"""The transformation pipeline.

    Master Data  ->  the universe of valid combinations (which products exist
                     in which cities) and the authoritative labels/prices.
    New Data     ->  the transactions actually collected.
    Historical   ->  the output template, and the donor pool used to invent
                     rows for combinations New Data never reported.

Pipeline stages:

    1. dedupe      collapse repeated transactions to one row per key + date
    2. grid        master combinations x reporting dates = every row we owe
    3. join        attach New Data; rows that stay empty are the gaps
    4. donors      pick a Historical row for each gap, via a fallback chain
    5. resolve     build each output column (real rows and gap rows separately)
    6. derive      user-defined computed columns over the assembled output
    7. identity    assign or blank out IDs
    8. validate    check the result against Master Data rules
"""
from __future__ import annotations

import datetime as _dt
import re

import numpy as np
import pandas as pd

from . import excel_io, formula, mapping, standardise

NS = ("new", "master", "donor", "new_donor", "grid", "out")

# Separator for composite match keys - a control character so it can never
# collide with real data values.
SEP = ""


# ==========================================================================
# helpers
# ==========================================================================
def _key_series(df: pd.DataFrame, cols: list[str], case_insensitive: bool = True) -> pd.Series:
    """Join several columns into one comparable key string."""
    if not cols:
        return pd.Series([""] * len(df), index=df.index)
    parts = []
    for c in cols:
        s = df[c] if c in df.columns else pd.Series([None] * len(df), index=df.index)
        if pd.api.types.is_datetime64_any_dtype(s):
            t = pd.to_datetime(s, errors="coerce").dt.strftime("%Y-%m-%d")
        elif pd.api.types.is_numeric_dtype(s):
            t = s.map(lambda v: "" if pd.isna(v) else (
                str(int(v)) if float(v).is_integer() else str(float(v))))
        else:
            t = s.astype(object).map(lambda v: "" if v is None or pd.isna(v) else str(v).strip())
        t = t.astype(str)
        if case_insensitive:
            t = t.str.upper()
        parts.append(t)
    out = parts[0]
    for p in parts[1:]:
        out = out + SEP + p
    return out


def _norm_dates(s: pd.Series) -> pd.Series:
    """Coerce a column to date-only timestamps, tolerating Excel serials."""
    if pd.api.types.is_numeric_dtype(s):
        vals = pd.to_numeric(s, errors="coerce")
        valid = vals.dropna()
        if len(valid) and valid.between(20000, 80000).mean() > 0.8:
            return excel_io.excel_serial_to_datetime(vals).dt.normalize()
    return pd.to_datetime(s, errors="coerce").dt.normalize()


AGGS = {
    "first": "first", "last": "last", "sum": "sum", "mean": "mean",
    "max": "max", "min": "min", "median": "median",
}


def dedupe(df: pd.DataFrame, keys: list[str], default: str = "first",
           overrides: dict[str, str] | None = None) -> tuple[pd.DataFrame, int]:
    """Collapse rows sharing the same key. Returns (frame, rows_removed)."""
    overrides = overrides or {}
    present = [k for k in keys if k in df.columns]
    if not present or default == "none":
        return df, 0
    dup_count = int(df.duplicated(present, keep="first").sum())
    if dup_count == 0:
        return df, 0

    spec: dict[str, str] = {}
    for c in df.columns:
        if c in present:
            continue
        how = overrides.get(c, default)
        if how in ("sum", "mean", "median") and not pd.api.types.is_numeric_dtype(df[c]):
            how = "first"
        spec[c] = AGGS.get(how, "first")
    if not spec:
        return df.drop_duplicates(present, keep="first").reset_index(drop=True), dup_count

    grouped = df.groupby(present, as_index=False, sort=False, dropna=False).agg(spec)
    return grouped[list(df.columns)].reset_index(drop=True), dup_count


# ==========================================================================
# donor selection
# ==========================================================================
def pick_donors(gaps: pd.DataFrame, hist: pd.DataFrame, chain: list[list[dict]],
                hist_date_col: str | None, strategy: str = "last") -> tuple[pd.DataFrame, pd.Series]:
    """Attach a Historical donor row to every gap row.

    `chain` is an ordered list of key-sets; each key-set is a list of
    {"grid": <grid column>, "hist": <historical column>} pairs. The first
    key-set that finds a donor wins, so callers can degrade from
    region+product down to product-only.

    Returns (donor frame aligned to gaps.index, Series naming the matched level).
    """
    donor = pd.DataFrame(index=gaps.index, columns=hist.columns, dtype=object)
    level = pd.Series([None] * len(gaps), index=gaps.index, dtype=object)
    if hist.empty or not chain:
        return donor, level

    h = hist.copy()
    if hist_date_col and hist_date_col in h.columns:
        h["__d"] = _norm_dates(h[hist_date_col])
    else:
        h["__d"] = pd.NaT

    unresolved = pd.Index(gaps.index)
    for depth, keyset in enumerate(chain):
        if len(unresolved) == 0:
            break
        pairs = [p for p in keyset
                 if p.get("grid") in gaps.columns and p.get("hist") in h.columns]
        if not pairs:
            continue
        g_cols = [p["grid"] for p in pairs]
        h_cols = [p["hist"] for p in pairs]

        sub = gaps.loc[unresolved]
        gk = _key_series(sub, g_cols)
        hk = _key_series(h, h_cols)

        if strategy in ("nearest", "previous") and h["__d"].notna().any() and "__target_date" in gaps.columns:
            chosen = _asof_donor(sub, gk, h, hk, strategy)
        else:
            chosen = _static_donor(gk, h, hk, strategy)

        hit = chosen.dropna()
        if len(hit):
            rows = h.loc[hit.to_numpy()]
            rows.index = hit.index
            for c in hist.columns:
                donor.loc[hit.index, c] = rows[c]
            label = "+".join(g_cols)
            level.loc[hit.index] = f"{label}" if depth == 0 else f"{label} (fallback {depth})"
            unresolved = unresolved.difference(hit.index)

    return donor, level


def _static_donor(gk: pd.Series, h: pd.DataFrame, hk: pd.Series, strategy: str) -> pd.Series:
    """One donor per key, independent of the target date."""
    tmp = pd.DataFrame({"k": hk, "d": h["__d"]}, index=h.index)
    if strategy == "random":
        pick = tmp.groupby("k").apply(
            lambda g: g.sample(1, random_state=None).index[0], include_groups=False)
    elif strategy == "first":
        order = tmp.sort_values("d", na_position="first")
        pick = order.groupby("k").apply(lambda g: g.index[0], include_groups=False)
    else:  # "last" - the most recent historical row for that key
        order = tmp.sort_values("d", na_position="first")
        pick = order.groupby("k").apply(lambda g: g.index[-1], include_groups=False)
    lookup = pick.to_dict()
    return gk.map(lookup)


def _asof_donor(sub: pd.DataFrame, gk: pd.Series, h: pd.DataFrame, hk: pd.Series,
                strategy: str) -> pd.Series:
    """Date-aware donor: nearest, or most recent on/before the target date."""
    left = pd.DataFrame({
        "__k": gk.to_numpy(),
        "__d": pd.to_datetime(sub["__target_date"]).to_numpy(),
        "__orig": sub.index.to_numpy(),
    }).dropna(subset=["__d"]).sort_values("__d")
    right = pd.DataFrame({
        "__k": hk.to_numpy(),
        "__d": h["__d"].to_numpy(),
        "__hidx": h.index.to_numpy(),
    }).dropna(subset=["__d"]).sort_values("__d")
    if left.empty or right.empty:
        return pd.Series([np.nan] * len(sub), index=sub.index)

    merged = pd.merge_asof(
        left, right, on="__d", by="__k",
        direction="nearest" if strategy == "nearest" else "backward",
        allow_exact_matches=True,
    )
    out = pd.Series([np.nan] * len(sub), index=sub.index, dtype=object)
    ok = merged.dropna(subset=["__hidx"])
    out.loc[ok["__orig"].to_numpy()] = ok["__hidx"].to_numpy()
    # Rows whose date was unusable still deserve a fallback donor.
    if out.isna().any():
        fallback = _static_donor(gk[out.isna()], h, hk, "last")
        out.loc[fallback.index] = fallback
    return out


# ==========================================================================
# column resolution
# ==========================================================================
class Context:
    """Namespaced access to every frame that can feed an output column."""

    def __init__(self, index: pd.Index):
        self.index = index
        self.frames: dict[str, pd.DataFrame] = {}
        self.out = pd.DataFrame(index=index)

    def add(self, ns: str, df: pd.DataFrame):
        self.frames[ns] = df

    def available(self) -> set[str]:
        names: set[str] = set()
        for ns, df in self.frames.items():
            for c in df.columns:
                names.add(f"{ns}.{c}")
        for c in self.out.columns:
            names.add(f"out.{c}")
            names.add(str(c))
        return names

    def resolve(self, ref: str):
        ref = str(ref).strip()
        if "." in ref:
            ns, _, col = ref.partition(".")
            ns = ns.strip().lower()
            if ns in self.frames and col in self.frames[ns].columns:
                return self.frames[ns][col].reindex(self.index)
            if ns == "out" and col in self.out.columns:
                return self.out[col]
        # Bare name: prefer an already-built output column, then new/master/donor.
        if ref in self.out.columns:
            return self.out[ref]
        for ns in ("new", "master", "donor", "new_donor", "grid"):
            df = self.frames.get(ns)
            if df is not None and ref in df.columns:
                return df[ref].reindex(self.index)
        raise formula.FormulaError(f"Unknown column reference [{ref}]")


def resolve_spec(spec: dict | None, ctx: Context) -> pd.Series:
    """Turn one column-source specification into a Series."""
    idx = ctx.index
    blank = pd.Series([None] * len(idx), index=idx, dtype=object)
    if not spec:
        return blank
    kind = str(spec.get("type", "blank")).lower()

    if kind == "blank":
        return blank
    if kind in ("const", "constant"):
        return pd.Series([spec.get("value")] * len(idx), index=idx)
    if kind == "formula":
        return formula.evaluate(spec.get("expr", ""), ctx.resolve, idx)
    if kind in NS:
        col = spec.get("column")
        df = ctx.frames.get(kind) if kind != "out" else ctx.out
        if df is None or col not in df.columns:
            return blank
        return df[col].reindex(idx) if kind != "out" else df[col]
    if kind == "coalesce":
        out = blank
        for sub in spec.get("sources", []):
            s = resolve_spec(sub, ctx)
            out = out.where(pd.notna(out), s)
        return out
    return blank


# ==========================================================================
# validation
# ==========================================================================
MAX_ISSUES_PER_RULE = 500


def run_validations(out: pd.DataFrame, ctx: Context, rules: list[dict]) -> pd.DataFrame:
    """Check the output frame; returns a tidy frame of violations."""
    issues: list[dict] = []
    for rule in rules or []:
        col = rule.get("column")
        kind = str(rule.get("type", "")).lower()
        if not col or col not in out.columns:
            continue
        s = out[col]
        bad = pd.Series(False, index=out.index)
        detail = ""

        if kind in ("non_negative", "not_negative"):
            bad = pd.to_numeric(s, errors="coerce") < 0
            detail = "value is negative"
        elif kind == "positive":
            bad = pd.to_numeric(s, errors="coerce") <= 0
            detail = "value is not greater than zero"
        elif kind == "not_blank":
            bad = s.isna() | (s.astype(str).str.strip() == "")
            detail = "value is blank"
        elif kind == "min":
            lim = float(rule.get("value", 0))
            bad = pd.to_numeric(s, errors="coerce") < lim
            detail = f"below minimum {lim}"
        elif kind == "max":
            lim = float(rule.get("value", 0))
            bad = pd.to_numeric(s, errors="coerce") > lim
            detail = f"above maximum {lim}"
        elif kind == "between":
            lo, hi = float(rule.get("min", 0)), float(rule.get("max", 0))
            v = pd.to_numeric(s, errors="coerce")
            bad = (v < lo) | (v > hi)
            detail = f"outside {lo}-{hi}"
        elif kind in ("range_pct", "within_pct"):
            pct = float(rule.get("pct", 10))
            try:
                ref = ctx.resolve(rule.get("reference", ""))
            except formula.FormulaError:
                continue
            v = pd.to_numeric(s, errors="coerce")
            r = pd.to_numeric(ref, errors="coerce")
            lo, hi = r * (1 - pct / 100.0), r * (1 + pct / 100.0)
            bad = r.notna() & v.notna() & ((v < lo) | (v > hi))
            detail = f"more than {pct}% away from {rule.get('reference')}"
        elif kind == "in_master":
            try:
                ref = ctx.resolve(rule.get("reference", ""))
            except formula.FormulaError:
                continue
            allowed = set(ref.dropna().astype(str).str.strip().str.upper())
            bad = s.notna() & ~s.astype(str).str.strip().str.upper().isin(allowed)
            detail = "value not present in Master Data"
        elif kind == "unique":
            bad = s.duplicated(keep=False) & s.notna()
            detail = "duplicate value"
        else:
            continue

        bad = bad.fillna(False)
        if not bad.any():
            continue
        hits = out.index[bad]
        total = len(hits)
        positions = out.index.get_indexer(hits)
        for n, (i, pos) in enumerate(zip(hits[:MAX_ISSUES_PER_RULE],
                                         positions[:MAX_ISSUES_PER_RULE])):
            issues.append({
                "Row": int(pos) + 2,  # +2 = 1-based, past the header row
                "Column": col,
                "Rule": kind,
                "Value": excel_io.json_safe(out.at[i, col]),
                "Problem": detail,
            })
        if total > MAX_ISSUES_PER_RULE:
            issues.append({
                "Row": None, "Column": col, "Rule": kind, "Value": None,
                "Problem": f"... and {total - MAX_ISSUES_PER_RULE:,} more row(s) "
                           f"breaching this rule ({total:,} in total)",
            })
    return pd.DataFrame(issues, columns=["Row", "Column", "Rule", "Value", "Problem"])


# ==========================================================================
# main entry point
# ==========================================================================
def build_output(new_df: pd.DataFrame, master_df: pd.DataFrame, hist_df: pd.DataFrame,
                 cfg: dict) -> dict:
    """Run the whole pipeline and return frames plus a run report."""
    report: dict = {"warnings": [], "steps": []}

    grid_cfg = cfg.get("grid", {}) or {}
    gap_cfg = cfg.get("gapfill", {}) or {}
    out_cols: list[dict] = cfg.get("output_columns", []) or []
    derived: list[dict] = cfg.get("derived_columns", []) or []

    new_df = new_df.copy()
    master_dims: list[dict] = grid_cfg.get("master_dims", []) or []
    if not master_dims:
        raise ValueError("Select at least one Master Data dimension for the output grid.")

    # --- standardise New Data labels against Master Data (step 2) ----------
    # Runs before anything else: matching, the grid and gap detection all key
    # off these values, so they must be the Master Data spelling first.
    std_info = {}
    decisions = cfg.get("standardise_decisions") or {}
    # Standardisation covers every attribute the user paired in step 2, which is
    # usually wider than the handful of dimensions that form the grid.
    attr_map = cfg.get("attribute_map") or [
        {"new": d.get("new"), "master": d.get("master")} for d in master_dims]
    # Roles are enforced here as well as in the UI, so a saved profile can never
    # reintroduce standardisation of a measure column.
    roles = (cfg.get("column_roles") or {}).get("new") or {}
    if roles:
        blocked = [a for a in attr_map
                   if a.get("new") and roles.get(a["new"], "dimension") != "dimension"]
        if blocked:
            attr_map = [a for a in attr_map if a not in blocked]
            report["warnings"].append(
                "Skipped standardising " +
                ", ".join(f"'{a['new']}'" for a in blocked) +
                " - not marked as a dimension in step 2.")
    if decisions:
        new_df, master_df, std_info = standardise.apply_decisions(
            new_df, master_df, attr_map, decisions)
        renamed = std_info.get("rename_row_count", 0)
        if renamed:
            report["steps"].append(
                f"Standardised {renamed:,} New Data row(s) onto Master Data labels "
                f"({len(std_info['renamed'])} value rule(s)).")
        if std_info.get("additions_frame") is not None:
            report["steps"].append(
                f"Added {len(std_info['additions_frame']):,} new combination(s) to "
                "Master Data from values you chose to keep.")
        if std_info.get("excluded"):
            report["warnings"].append(
                f"{len(std_info['excluded'])} value(s) were left unmatched on purpose; "
                "their rows are not in the output.")

    # Legacy value maps, kept so older saved profiles still load.
    for vm in cfg.get("value_maps", []) or []:
        col, pairs = vm.get("column"), vm.get("pairs") or {}
        if col in new_df.columns and pairs:
            new_df[col] = mapping.apply_value_map(
                new_df[col], {str(k).strip().upper(): v for k, v in pairs.items()},
                keep_unmatched=vm.get("keep_unmatched", True))

    # --- dates ------------------------------------------------------------
    new_date_col = grid_cfg.get("new_date_column")

    if new_date_col and new_date_col in new_df.columns:
        new_df["__date"] = _norm_dates(new_df[new_date_col])
    else:
        new_df["__date"] = pd.NaT

    date_mode = grid_cfg.get("date_mode", "from_new")
    if date_mode == "explicit" and grid_cfg.get("dates"):
        dates = pd.to_datetime(pd.Series(grid_cfg["dates"]), errors="coerce").dropna().unique()
    elif date_mode == "range" and grid_cfg.get("date_from") and grid_cfg.get("date_to"):
        dates = pd.date_range(grid_cfg["date_from"], grid_cfg["date_to"], freq="D").to_numpy()
    else:
        dates = np.sort(new_df["__date"].dropna().unique())
    dates = pd.to_datetime(pd.Series(dates)).dt.normalize().unique()
    if len(dates) == 0:
        raise ValueError("No reporting dates found. Pick the date column in New Data.")

    # --- dedupe New Data ---------------------------------------------------
    dedupe_cfg = grid_cfg.get("dedupe", {}) or {}
    strategy = dedupe_cfg.get("strategy", "first")
    new_keys = [d["new"] for d in master_dims if d.get("new") and d["new"] in new_df.columns]
    new_df, removed = dedupe(new_df, new_keys + ["__date"], strategy,
                             dedupe_cfg.get("overrides", {}))
    if removed:
        report["steps"].append(
            f"Collapsed {removed:,} duplicate transaction rows using '{strategy}'.")

    # --- the grid: master combinations x dates -----------------------------
    m_cols = [d["master"] for d in master_dims if d.get("master") in master_df.columns]
    if not m_cols:
        raise ValueError("The selected Master Data dimension columns were not found.")
    combos = master_df.dropna(subset=m_cols, how="all").drop_duplicates(m_cols).reset_index(drop=True)
    if grid_cfg.get("master_filter"):
        combos = _apply_filter(combos, grid_cfg["master_filter"], report)

    grid = combos.merge(pd.DataFrame({"__target_date": dates}), how="cross")
    report["steps"].append(
        f"Built a grid of {len(combos):,} Master Data combinations x {len(dates)} date(s) "
        f"= {len(grid):,} required rows.")

    # A preview only needs the first handful of rows; everything downstream is
    # per-row, so trimming here makes the live column preview fast.
    limit = cfg.get("preview_limit")
    if limit:
        grid = grid.head(int(limit)).reset_index(drop=True)
        report["preview"] = True
        report["steps"].append(f"Preview mode: only the first {len(grid):,} row(s) built.")

    # --- attach New Data ---------------------------------------------------
    # Both sides must key on the same dimensions, so only the dimensions that
    # actually resolved to a New Data column can take part in the match.
    matched_dims = [d for d in master_dims
                    if d.get("new") and d["new"] in new_df.columns
                    and d.get("master") in master_df.columns]
    if not matched_dims:
        raise ValueError(
            "No grid dimension is mapped to a New Data column, so New Data "
            "cannot be matched to the grid. Set the New Data column for at "
            "least one dimension in step 2.")
    if len(matched_dims) != len(m_cols):
        report["warnings"].append(
            "Some Master Data dimensions have no matching New Data column; "
            "matching used only the columns that were mapped.")

    new_key_cols = [d["new"] for d in matched_dims]
    grid_key_cols = [d["master"] for d in matched_dims]

    new_keys = _key_series(new_df, new_key_cols) + SEP + \
        new_df["__date"].dt.strftime("%Y-%m-%d")
    grid_keys = _key_series(grid, grid_key_cols) + SEP + \
        grid["__target_date"].dt.strftime("%Y-%m-%d")

    # Helper columns must not leak into the output namespace.
    new_cols = [c for c in new_df.columns if not str(c).startswith("__")]
    firsts = ~new_keys.duplicated(keep="first")
    lookup = pd.Series(np.arange(int(firsts.sum())), index=new_keys[firsts].to_numpy())
    new_rows = new_df.loc[firsts, new_cols].reset_index(drop=True)

    matched_pos = grid_keys.map(lookup)
    is_real = matched_pos.notna()

    aligned_new = pd.DataFrame(index=grid.index, columns=new_cols, dtype=object)
    if is_real.any():
        take = new_rows.iloc[matched_pos[is_real].astype(int).to_numpy()]
        take.index = grid.index[is_real]
        for c in new_cols:
            aligned_new.loc[take.index, c] = take[c]

    # With gap filling switched off the grid collapses to just what New Data
    # reported, rather than emitting empty rows for every expected combination.
    if not gap_cfg.get("enabled", True):
        keep = grid.index[is_real]
        grid = grid.loc[keep].reset_index(drop=True)
        aligned_new = aligned_new.loc[keep].reset_index(drop=True)
        grid_keys = grid_keys.loc[keep].reset_index(drop=True)
        is_real = pd.Series(True, index=grid.index)
        report["steps"].append(
            "Gap filling is off - only combinations present in New Data are output.")

    n_real, n_gap = int(is_real.sum()), int((~is_real).sum())
    orphan = ~new_keys.isin(set(grid_keys))
    unused = int(orphan.sum())
    # Keep the rejects so the user can see which products/cities Master Data is
    # missing, rather than just being told a number went missing.
    excluded = new_df.loc[orphan, new_cols].copy() if unused else \
        pd.DataFrame(columns=new_cols)
    report["steps"].append(
        f"Matched {n_real:,} rows from New Data; {n_gap:,} rows are gaps to fill.")
    if unused:
        report["warnings"].append(
            f"{unused:,} New Data row(s) did not match any Master Data combination "
            "and were excluded. Check the dimension mapping and value mapping.")

    # --- master attributes for every grid row ------------------------------
    master_full = master_df.drop_duplicates(m_cols).set_index(_key_series(
        master_df.drop_duplicates(m_cols), m_cols))
    grid_mkey = _key_series(grid, m_cols)
    pos = grid_mkey.map(pd.Series(np.arange(len(master_full)), index=master_full.index))
    aligned_master = pd.DataFrame(index=grid.index, columns=master_df.columns, dtype=object)
    ok = pos.notna()
    if ok.any():
        take = master_full.iloc[pos[ok].astype(int).to_numpy()]
        take.index = grid.index[ok]
        for c in master_df.columns:
            aligned_master.loc[take.index, c] = take[c]

    # --- donors for the gap rows -------------------------------------------
    # Two independent donor pools. Historical Data is the usual source, but New
    # Data is often the better one: a product missing in one city on a reporting
    # date is usually present in another city on that same date, and those
    # figures are current rather than months old.
    donor = pd.DataFrame(index=grid.index, columns=hist_df.columns, dtype=object)
    donor_new = pd.DataFrame(index=grid.index, columns=new_cols, dtype=object)
    donor_level = pd.Series([None] * len(grid), index=grid.index, dtype=object)
    gap_idx = grid.index[~is_real]
    strategy = gap_cfg.get("donor_strategy", "last")

    if gap_cfg.get("enabled", True) and len(gap_idx):
        gaps = grid.loc[gap_idx]

        if not hist_df.empty:
            chain = gap_cfg.get("match_chain") or _default_chain(master_dims, hist_df, "hist")
            d, lvl = pick_donors(gaps, hist_df, chain,
                                 gap_cfg.get("hist_date_column"), strategy)
            donor.loc[gap_idx] = d
            donor_level.loc[gap_idx] = lvl
            found = int(lvl.notna().sum())
            report["steps"].append(
                f"Found Historical donors for {found:,} of {len(gap_idx):,} gap rows "
                f"(strategy: {strategy}).")

        # The New Data pool is searched on the same dimensions. The date is
        # deliberately NOT part of the match - the whole point is to borrow a
        # figure from another row for the date we are filling.
        new_pool = new_df.loc[:, new_cols]
        if not new_pool.empty:
            chain_new = gap_cfg.get("match_chain_new") or \
                _default_chain(master_dims, new_pool, "new")
            dn, lvln = pick_donors(gaps, new_pool, chain_new,
                                   new_date_col if new_date_col in new_pool.columns else None,
                                   strategy)
            donor_new.loc[gap_idx] = dn
            found_new = int(lvln.notna().sum())
            report["steps"].append(
                f"Found New Data donors for {found_new:,} of {len(gap_idx):,} gap rows.")
            # Record whichever pool the default fill will actually use.
            prefer = gap_cfg.get("prefer", "historical")
            if prefer == "new":
                donor_level.loc[gap_idx] = lvln.where(lvln.notna(),
                                                      donor_level.loc[gap_idx])

        have = donor_level.loc[gap_idx].notna()
        if not have.all():
            report["warnings"].append(
                f"{int((~have).sum()):,} gap row(s) matched neither pool. "
                "They fall back to Master Data values or stay blank.")

    # --- resolve output columns --------------------------------------------
    ctx = Context(grid.index)
    ctx.add("new", aligned_new)
    ctx.add("master", aligned_master)
    ctx.add("donor", donor)
    ctx.add("new_donor", donor_new)
    # The grid namespace is also published under the Historical template's own
    # names, so a template column called 'DATE' or 'City' still resolves to the
    # row's true date/dimension without the user mapping it by hand.
    grid_ns = {"DATE": grid["__target_date"], **{c: grid[c] for c in m_cols}}
    hist_date = gap_cfg.get("hist_date_column")
    if hist_date and hist_date not in grid_ns:
        grid_ns[hist_date] = grid["__target_date"]
    for d in master_dims:
        alias, src = d.get("hist"), d.get("master")
        if alias and src in grid.columns and alias not in grid_ns:
            grid_ns[alias] = grid[src]
    ctx.add("grid", pd.DataFrame(grid_ns, index=grid.index))

    fill_rules = {r.get("column"): r for r in (gap_cfg.get("rules", []) or [])}

    # The date and the grid dimensions are what a filled row *is* - they come
    # from New Data's reporting range and the Master Data combination, never
    # from whichever donor supplied the measures. A rule pointing at a donor
    # here would stamp the donor's own date on the row, so drop it and say so.
    grid_owned = set(grid_ns.keys())
    hijacked = [c for c, r in fill_rules.items()
                if c in grid_owned and str(r.get("type")) in ("donor", "new_donor")]
    for c in hijacked:
        fill_rules.pop(c, None)
    if hijacked:
        report["warnings"].append(
            "Ignored a donor-based fill rule on " + ", ".join(f"'{c}'" for c in hijacked) +
            " - dates and dimensions always come from the reporting grid, "
            "so filled rows keep New Data's date range.")

    order: list[str] = [s["name"] for s in out_cols if s.get("name")]

    # Compute in dependency order, not top-to-bottom. A column's position in the
    # file is the user's layout choice and has nothing to do with what its
    # formula needs, so UNIQUE ID can sit first and still be built from CITY and
    # BRAND further down. Only a genuine circular reference is an error.
    for spec in _resolution_order(out_cols):
        name = spec.get("name")
        if not name:
            continue
        # Name the column and the step, so a bad formula is findable among the
        # two dozen on screen instead of just 'invalid syntax'.
        try:
            real_vals = resolve_spec(spec.get("source"), ctx)
        except formula.FormulaError as e:
            raise ValueError(
                f"Step 2, source formula for output column '{name}': {e}") from e
        rule = fill_rules.get(name) or spec.get("fill")
        src = spec.get("source") or {}
        # A formula is a definition of the column, not a description of one kind
        # of row: "RET MARGIN = UNIT SP - UNIT TP" is true of every row. So it
        # computes for filled rows too, unless step 4 explicitly overrides it.
        # Without this a formula column comes out populated on filled rows (from
        # the donor) and blank on the real ones, which reads as broken.
        formula_all_rows = (src.get("type") == "formula"
                            and src.get("all_rows", True) and not rule)
        try:
            if formula_all_rows:
                # Compute it for the filled rows as well, then fall back to the
                # normal donor value wherever the formula could not produce one
                # - typically because it reads [new.*], which a filled row has
                # nothing in. So [out.*] formulas apply everywhere, and [new.*]
                # ones still leave filled rows with a sensible donor figure.
                gap_vals = real_vals.where(
                    pd.notna(real_vals),
                    _default_fill(spec, ctx, gap_cfg.get("prefer", "historical")))
            else:
                gap_vals = resolve_spec(rule, ctx) if rule else _default_fill(
                    spec, ctx, gap_cfg.get("prefer", "historical"))
        except formula.FormulaError as e:
            raise ValueError(
                f"Step 4, gap-fill rule for column '{name}': {e}") from e
        combined = pd.Series(
            np.where(is_real.to_numpy(), real_vals.to_numpy(object), gap_vals.to_numpy(object)),
            index=grid.index, dtype=object)
        ctx.out[name] = combined

    # --- derived columns ----------------------------------------------------
    for spec in derived:
        name = spec.get("name")
        if not name:
            continue
        try:
            ctx.out[name] = formula.evaluate(spec.get("expr", ""), ctx.resolve, ctx.index)
        except formula.FormulaError as e:
            raise ValueError(f"Step 3, computed column '{name}': {e}") from e
        if name not in order:
            pos_at = spec.get("position")
            if isinstance(pos_at, int) and 0 <= pos_at <= len(order):
                order.insert(pos_at, name)
            else:
                order.append(name)

    out = ctx.out[order].copy() if order else ctx.out.copy()

    # --- identity ------------------------------------------------------------
    id_cfg = gap_cfg.get("id_policy") or {}
    id_col = id_cfg.get("column")
    if id_col in out.columns:
        # A rule the user wrote for this column in step 4 is a deliberate
        # instruction; the ID policy must not silently overwrite it.
        if id_col in fill_rules:
            report["steps"].append(
                f"'{id_col}' uses its step 4 rule, so the ID policy was not applied.")
        else:
            out[id_col] = _apply_id_policy(
                out[id_col], is_real, id_cfg, aligned_new, donor)

    # --- provenance ----------------------------------------------------------
    flag_col = cfg.get("flag_column", "SOURCE")
    if cfg.get("add_flag", True):
        out[flag_col] = np.where(is_real.to_numpy(), "ACTUAL", "FILLED")
        if donor_level.notna().any():
            out["FILL BASIS"] = donor_level.where(donor_level.notna(), None)

    out = _finalise_types(out)

    # --- validation -----------------------------------------------------------
    issues = run_validations(out, ctx, cfg.get("validations", []))

    # Per-column blank counts, split by row kind. "Why is this cell empty?" is
    # the question this tool gets asked most, and the actual/filled split
    # usually answers it on its own: blank only on filled rows points at the
    # gap-fill rules, blank only on real rows points at the step 3 source.
    # Each column's step-3 source, so a blank can name the real cause rather
    # than always blaming step 4. A formula that reads [new.*] is empty on gap
    # rows because no New Data row sits behind them - that is a step-3 problem
    # even though the emptiness only shows on filled rows.
    src_by_name = {s.get("name"): (s.get("source") or {})
                   for s in out_cols if s.get("name")}

    def _blank_cause(col, only_actual, only_filled):
        src = src_by_name.get(col, {})
        stype = src.get("type")
        if stype == "formula":
            reads_new = any(r.startswith("new.")
                            for r in formula.extract_refs(src.get("expr", "")))
            if only_filled and reads_new:
                return ("its step 3 formula reads from New Data, which is empty "
                        "on gap-filled rows - reference [out.*] instead")
            if only_filled:
                return ("its step 3 formula, whose inputs are empty on gap-filled "
                        "rows - give those inputs a step 4 rule")
            return "its step 3 formula returning nothing for these rows"
        if only_filled:
            return "its step 4 gap-fill rule"
        if only_actual:
            return "its Source/Operation in step 3"
        return "no source, or a formula returning nothing"

    blanks = []
    for c in out.columns:
        s = out[c]
        empty = s.isna() | (s.astype("object").map(
            lambda v: isinstance(v, str) and v.strip() == ""))
        n = int(empty.sum())
        if not n:
            continue
        entry = {"column": str(c), "blank": n, "pct": round(100.0 * n / max(len(out), 1), 1)}
        if flag_col in out.columns:
            kind = out[flag_col]
            a = int((empty & kind.eq("ACTUAL")).sum())
            f = int((empty & kind.eq("FILLED")).sum())
            entry["actual"] = a
            entry["filled"] = f
            entry["cause"] = _blank_cause(str(c), f == 0 and a > 0, a == 0 and f > 0)
        blanks.append(entry)
    report["blank_columns"] = sorted(blanks, key=lambda x: -x["blank"])

    report["row_count"] = int(len(out))
    report["actual_rows"] = n_real
    report["filled_rows"] = n_gap
    report["excluded_new_rows"] = unused
    report["issue_count"] = int(len(issues))
    if len(issues):
        report["warnings"].append(
            f"{len(issues):,} validation issue(s) found - see the Validation Issues sheet.")

    return {"output": out, "issues": issues, "excluded": excluded, "report": report,
            "master_additions": std_info.get("additions_frame"), "ctx": ctx}


def _resolution_order(out_cols: list[dict]) -> list[dict]:
    """Output columns sorted so a formula's [out.*] dependencies come first.

    Declaration order is kept between columns that do not depend on each other,
    so the result is stable and predictable. A circular reference is reported by
    name rather than silently producing blanks.
    """
    by_name = {s["name"]: s for s in out_cols if s.get("name")}
    deps: dict[str, list[str]] = {}
    for name, spec in by_name.items():
        expr = (spec.get("source") or {}).get("expr") or ""
        refs = []
        for ref in formula.extract_refs(expr):
            if ref.startswith("out."):
                target = ref[4:]
                if target in by_name and target != name:
                    refs.append(target)
        deps[name] = refs

    ordered: list[dict] = []
    done: set[str] = set()
    visiting: list[str] = []

    def visit(name: str):
        if name in done:
            return
        if name in visiting:
            cycle = " -> ".join(visiting[visiting.index(name):] + [name])
            raise ValueError(
                f"Circular reference between output columns: {cycle}. "
                "One of these formulas has to stop referring back.")
        visiting.append(name)
        for dep in deps[name]:
            visit(dep)
        visiting.pop()
        done.add(name)
        ordered.append(by_name[name])

    for spec in out_cols:
        if spec.get("name"):
            visit(spec["name"])
    return ordered


def _default_fill(spec: dict, ctx: Context, prefer: str = "historical") -> pd.Series:
    """Value for a gap row when the user set no explicit rule.

    The grid comes first and is never overridden: a filled row exists precisely
    because Master Data says this city/product/date should be present, so its
    dimensions and date must describe *that* row - not the donor's. Taking the
    donor's DATE or CITY here would silently duplicate the donor's identity and
    defeat the whole point of filling the gap.

    After that: the donor's value for the same column, then Master Data, then
    blank.
    """
    name = spec.get("name")
    blank = pd.Series([None] * len(ctx.index), index=ctx.index, dtype=object)

    grid_df = ctx.frames.get("grid")
    if grid_df is not None and name in grid_df.columns:
        return grid_df[name].reindex(ctx.index)

    # Both donor pools, preferred one first, then Master Data.
    order = ["new_donor", "donor"] if prefer == "new" else ["donor", "new_donor"]
    base = blank
    for ns in order + ["master"]:
        df = ctx.frames.get(ns)
        if df is not None and name in df.columns:
            base = base.where(pd.notna(base), df[name].reindex(ctx.index))
    return base


def _default_chain(master_dims: list[dict], pool_df: pd.DataFrame,
                   side: str = "hist") -> list[list[dict]]:
    """Full dimension match first, then progressively drop the leading ones.

    `side` selects which mapping on each dimension names the pool's column -
    'hist' for the Historical pool, 'new' for the New Data pool.
    """
    pairs = [{"grid": d["master"], "hist": d.get(side)}
             for d in master_dims if d.get(side) and d[side] in pool_df.columns]
    chain = []
    for cut in range(len(pairs)):
        sub = pairs[cut:]
        if sub:
            chain.append(sub)
    return chain or []


def _apply_id_policy(s: pd.Series, is_real: pd.Series, cfg: dict,
                     new_df: pd.DataFrame, donor: pd.DataFrame) -> pd.Series:
    mode = str(cfg.get("mode", "blank")).lower()
    gap = ~is_real
    out = s.copy()
    if mode == "blank":
        out.loc[gap] = None
    elif mode == "donor":
        pass  # already carries the donor value through the normal fill path
    elif mode in ("unique", "sequence"):
        existing = pd.to_numeric(s[is_real], errors="coerce").dropna()
        start = int(cfg.get("start") or (existing.max() + 1 if len(existing) else 1))
        n = int(gap.sum())
        prefix = str(cfg.get("prefix") or "")
        nums = np.arange(start, start + n)
        out.loc[gap] = [f"{prefix}{v}" if prefix else int(v) for v in nums]
    elif mode == "constant":
        out.loc[gap] = cfg.get("value")
    return out


def _apply_filter(df: pd.DataFrame, flt: dict, report: dict) -> pd.DataFrame:
    """Optional narrowing of the master universe, e.g. only ACTIVE CITY = 1."""
    col, op, val = flt.get("column"), str(flt.get("op", "eq")), flt.get("value")
    if not col or col not in df.columns:
        return df
    s = df[col]
    before = len(df)
    if op in ("eq", "="):
        out = df[s.astype(str).str.strip().str.upper() == str(val).strip().upper()]
    elif op in ("ne", "!="):
        out = df[s.astype(str).str.strip().str.upper() != str(val).strip().upper()]
    elif op == "in":
        vals = {str(v).strip().upper() for v in (val or [])}
        out = df[s.astype(str).str.strip().str.upper().isin(vals)]
    elif op in ("gt", "ge", "lt", "le"):
        v = pd.to_numeric(s, errors="coerce")
        lim = float(val)
        out = df[{"gt": v > lim, "ge": v >= lim, "lt": v < lim, "le": v <= lim}[op]]
    else:
        return df
    report["steps"].append(
        f"Master Data filter {col} {op} {val}: kept {len(out):,} of {before:,} combinations.")
    return out.reset_index(drop=True)


def _finalise_types(out: pd.DataFrame) -> pd.DataFrame:
    """Recover real dtypes after the object-dtype assembly."""
    for c in out.columns:
        s = out[c]
        if s.isna().all():
            continue
        non_null = s.dropna()
        if non_null.map(lambda v: isinstance(v, (pd.Timestamp, _dt.datetime, _dt.date))).all():
            out[c] = pd.to_datetime(s, errors="coerce")
            continue
        converted = pd.to_numeric(s, errors="coerce")
        if converted.notna().sum() == s.notna().sum() and \
                not non_null.map(lambda v: isinstance(v, str) and v.strip() == "").any():
            # Whole numbers become a nullable integer type so IDs and pack sizes
            # land in Excel as 818791684 rather than 818791684.0.
            valid = converted.dropna()
            if len(valid) and np.isfinite(valid).all() and (valid % 1 == 0).all() \
                    and valid.abs().max() < 2 ** 63 - 1:
                try:
                    out[c] = converted.astype("Int64")
                    continue
                except (TypeError, ValueError, OverflowError):
                    pass
            out[c] = converted
    return out
