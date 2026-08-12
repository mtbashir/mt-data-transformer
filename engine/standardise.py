"""Standardising New Data attribute values against Master Data.

New Data and Master Data describe the same things with different labels:
New Data says 'ORANGE FIZZ' where Master Data says 'ORANGE', and 'DIET COLA'
where Master Data says 'COLA'. Left alone, those rows match nothing and
silently drop out of the output.

This module reports every value that Master Data does not recognise, proposes a
standard for it, and applies the user's decision. Three decisions are possible:

    map     - this value IS that Master Data value; rewrite it
    add     - this is genuinely new; extend Master Data so it becomes valid
    exclude - leave it unmatched (its rows stay out of the output)
"""
from __future__ import annotations

import pandas as pd

from . import mapping


def distinct(s: pd.Series) -> list[str]:
    v = s.dropna().astype(str).str.strip()
    return [x for x in v.unique().tolist() if x != ""]


def _norm(v: str) -> str:
    return str(v).strip().upper()


def _similarity(value: str, candidate: str) -> float:
    """Similarity tuned for label standardisation.

    Plain string distance rates '250ml' closer to '2250ML' than to '250ML CAN',
    because one character differs either way. Whole-token agreement is what
    actually matters for these labels, so a candidate that extends the value at
    a word boundary - or simply starts the same way - is rewarded.
    """
    base = mapping.score_names(value, candidate)
    nv, nc = mapping.norm(value), mapping.norm(candidate)
    if not nv or not nc:
        return base
    if nc.startswith(nv + " ") or nv.startswith(nc + " "):
        base += 0.18
    tv, tc = nv.split(), nc.split()
    if tv and tc and tv[0] == tc[0]:
        base += 0.08
    return min(base, 1.0)


def suggest_standard(value: str, master_values: list[str],
                     crosswalk: dict[str, str] | None = None,
                     threshold: float = 0.72) -> dict:
    """Propose what an unrecognised value should become.

    A crosswalk found in the Master Data workbook wins, because it is an
    explicit statement by whoever maintains the data. Otherwise fall back to
    name similarity, and if nothing is close enough, propose adding it.
    """
    if crosswalk:
        hit = crosswalk.get(_norm(value))
        if hit and _norm(hit) in {_norm(m) for m in master_values}:
            return {"action": "map", "to": hit, "confidence": 1.0,
                    "why": "listed in the Master Data workbook"}

    best, best_score = None, 0.0
    for m in master_values:
        score = _similarity(value, m)
        if score > best_score:
            best, best_score = m, score

    if best is not None and best_score >= threshold:
        return {"action": "map", "to": best, "confidence": round(best_score, 3),
                "why": f"{int(best_score * 100)}% similar to '{best}'"}
    return {"action": "add", "to": None, "confidence": round(best_score, 3),
            "why": "nothing in Master Data resembles this"}


def build_report(new_df: pd.DataFrame, master_df: pd.DataFrame,
                 dims: list[dict], crosswalks: dict[str, dict] | None = None,
                 decisions: dict | None = None,
                 roles: dict[str, str] | None = None) -> list[dict]:
    """One entry per mapped dimension, listing matched and unmatched values.

    `roles` carries the user's step 2 classification. A column they explicitly
    marked as a dimension is checked even if it looks numeric - their call
    overrides the guard below.
    """
    crosswalks = crosswalks or {}
    decisions = decisions or {}
    roles = roles or {}
    out: list[dict] = []

    for dim in dims:
        n_col, m_col = dim.get("new"), dim.get("master")
        if not n_col or not m_col:
            continue
        if n_col not in new_df.columns or m_col not in master_df.columns:
            continue
        # Refuse to standardise measures. Prices and quantities are not labels;
        # 'converting' 1005 to 1000 because the digits look similar would
        # quietly corrupt the numbers. An explicit 'dimension' role overrides
        # this, so a numeric-looking code such as PKG can still be standardised.
        declared = roles.get(n_col)
        if declared != "dimension" and (
                mapping._numeric_like(new_df[n_col])
                or mapping._numeric_like(master_df[m_col])):
            out.append({
                "new_column": n_col, "master_column": m_col,
                "new_value_count": 0, "master_value_count": 0,
                "matched": 0, "unmatched": [], "master_values": [],
                "skipped": "numeric column - measures are never standardised",
            })
            continue

        new_vals = distinct(new_df[n_col])
        master_vals = distinct(master_df[m_col])
        master_lookup = {_norm(m) for m in master_vals}
        counts = new_df[n_col].astype(str).str.strip().value_counts()
        prior = decisions.get(n_col, {})

        unmatched = []
        for v in sorted(new_vals):
            if _norm(v) in master_lookup:
                continue
            chosen = prior.get(v)
            if not chosen:
                chosen = suggest_standard(v, master_vals, crosswalks.get(m_col))
            unmatched.append({
                "value": v,
                "rows": int(counts.get(v, 0)),
                "action": chosen.get("action", "map"),
                "to": chosen.get("to"),
                "why": chosen.get("why", ""),
                "confidence": chosen.get("confidence", 0),
            })

        out.append({
            "new_column": n_col,
            "master_column": m_col,
            "new_value_count": len(new_vals),
            "master_value_count": len(master_vals),
            "matched": len(new_vals) - len(unmatched),
            "unmatched": unmatched,
            "master_values": master_vals,
        })
    return out


def apply_decisions(new_df: pd.DataFrame, master_df: pd.DataFrame,
                    dims: list[dict], decisions: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Rewrite New Data values and extend Master Data per the user's choices.

    Returns (new_df, master_df, info) where `info` records what was done and
    which Master Data rows were invented, so both can be reported back.
    """
    new_df = new_df.copy()
    info = {"renamed": [], "added": [], "excluded": [], "additions_frame": None}
    if not decisions:
        return new_df, master_df, info

    # --- 1. rewrite values the user mapped onto a Master Data standard ------
    for dim in dims:
        n_col = dim.get("new")
        if not n_col or n_col not in new_df.columns:
            continue
        rules = decisions.get(n_col) or {}
        renames = {_norm(v): d.get("to") for v, d in rules.items()
                   if d.get("action") == "map" and d.get("to")}
        if not renames:
            continue
        before = new_df[n_col].astype(object)
        new_df[n_col] = mapping.apply_value_map(new_df[n_col], renames, keep_unmatched=True)
        changed = int((before.astype(str) != new_df[n_col].astype(str)).sum())
        for src, dst in renames.items():
            info["renamed"].append({"column": n_col, "from": src, "to": dst})
        info.setdefault("rename_row_count", 0)
        info["rename_row_count"] += changed

    # --- 2. extend Master Data with values the user chose to add ------------
    add_cols = {d.get("new"): d.get("master") for d in dims
                if d.get("new") and d.get("master")}
    wanted: list[tuple[str, str]] = []
    for n_col, rules in decisions.items():
        for value, d in (rules or {}).items():
            if d.get("action") == "add" and n_col in add_cols:
                wanted.append((n_col, value))
            elif d.get("action") == "exclude":
                info["excluded"].append({"column": n_col, "value": value})

    if wanted:
        mask = pd.Series(False, index=new_df.index)
        for n_col, value in wanted:
            if n_col in new_df.columns:
                mask |= new_df[n_col].astype(str).str.strip().str.upper() == _norm(value)
                info["added"].append({"column": n_col, "value": value})
        rows = new_df.loc[mask]
        if len(rows):
            # Only the combinations actually observed are added, so Master Data
            # is never told a product exists somewhere it was never reported.
            proj = {}
            for n_col, m_col in add_cols.items():
                if n_col in rows.columns:
                    proj[m_col] = rows[n_col].astype(str).str.strip()
            additions = pd.DataFrame(proj).drop_duplicates().reset_index(drop=True)
            if len(additions):
                for c in master_df.columns:
                    if c not in additions.columns:
                        additions[c] = pd.NA
                additions = additions[list(master_df.columns)]
                master_df = pd.concat([master_df, additions], ignore_index=True)
                info["additions_frame"] = additions
    return new_df, master_df, info


def collect_crosswalks(sheets: dict[str, pd.DataFrame],
                       master_df: pd.DataFrame) -> dict[str, dict[str, str]]:
    """Cross-walks in the Master Data workbook, keyed by target master column.

    A KEYS-style sheet pairs 'Diet Cola' with 'COLA'; if the right hand side
    lines up with a Master Data column, that pairing is authoritative for
    standardising into it.
    """
    out: dict[str, dict[str, str]] = {}
    master_sets = {c: {_norm(v) for v in distinct(master_df[c])}
                   for c in master_df.columns}
    for df in sheets.values():
        for pair in mapping.find_key_pairs(df):
            targets = {_norm(v) for v in pair["pairs"].values()}
            if not targets:
                continue
            for col, vals in master_sets.items():
                if not vals:
                    continue
                if len(targets & vals) / len(targets) >= 0.6:
                    bucket = out.setdefault(col, {})
                    for a, b in pair["pairs"].items():
                        bucket.setdefault(_norm(a), str(b).strip())
    return out
