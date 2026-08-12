"""Quality-control gating for the headless pipeline.

The web app's `transform.run_validations` *reports* rule breaches but never acts
on them. For an unattended agent we need to *act*: rows that break a rule marked
`blocking` are pulled out of the load set into a quarantine frame, and only the
clean rows go forward to the database. Non-blocking ("warn") rules are still
listed so nothing is hidden.

A "rule" is the same dict the app already produces, plus one extra flag:

    {"column": "NTP", "type": "range_pct", "reference": "master.NTP REGULAR",
     "pct": 10, "enabled": True, "blocking": True}

Supported types (identical semantics to the app):
    not_blank, non_negative, positive, min, max, between,
    range_pct / within_pct, in_master, unique.

`resolve(ref)` is any callable that turns a reference like "master.NTP REGULAR"
into a Series aligned to `out` - in the pipeline this is the engine Context's
`.resolve`. A blocking rule whose reference cannot be resolved is reported as
*unusable* (never silently treated as passed) so the runner can stop instead of
shipping unchecked data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAX_ISSUES_PER_RULE = 500


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def rule_mask(out: pd.DataFrame, resolve, rule: dict) -> tuple[pd.Series, str]:
    """Return (bad_mask, human_detail) for one rule over `out`.

    bad_mask is a boolean Series aligned to out.index; True = the row breaches
    the rule. An unknown/unusable rule yields an all-False mask.
    """
    false = pd.Series(False, index=out.index)
    col = rule.get("column")
    kind = str(rule.get("type", "")).lower()
    if not col or col not in out.columns:
        return false, ""
    s = out[col]

    if kind in ("non_negative", "not_negative"):
        return (_num(s) < 0).fillna(False), "value is negative"
    if kind == "positive":
        return (_num(s) <= 0).fillna(False), "value is not greater than zero"
    if kind == "not_blank":
        return (s.isna() | (s.astype(str).str.strip() == "")), "value is blank"
    if kind == "min":
        lim = float(rule.get("value", 0))
        return (_num(s) < lim).fillna(False), f"below minimum {lim}"
    if kind == "max":
        lim = float(rule.get("value", 0))
        return (_num(s) > lim).fillna(False), f"above maximum {lim}"
    if kind == "between":
        lo, hi = float(rule.get("min", 0)), float(rule.get("max", 0))
        v = _num(s)
        return ((v < lo) | (v > hi)).fillna(False), f"outside {lo}-{hi}"
    if kind in ("range_pct", "within_pct"):
        pct = float(rule.get("pct", 10))
        ref = _try_resolve(resolve, rule.get("reference", ""))
        if ref is None:
            return false, ""
        v, r = _num(s), _num(ref)
        lo, hi = r * (1 - pct / 100.0), r * (1 + pct / 100.0)
        bad = r.notna() & v.notna() & ((v < lo) | (v > hi))
        return bad.fillna(False), f"more than {pct}% away from {rule.get('reference')}"
    if kind == "in_master":
        ref = _try_resolve(resolve, rule.get("reference", ""))
        if ref is None:
            return false, ""
        allowed = set(ref.dropna().astype(str).str.strip().str.upper())
        bad = s.notna() & ~s.astype(str).str.strip().str.upper().isin(allowed)
        return bad.fillna(False), "value not present in Master Data"
    if kind == "unique":
        return (s.duplicated(keep=False) & s.notna()).fillna(False), "duplicate value"
    return false, ""


def _try_resolve(resolve, ref: str):
    if not ref or resolve is None:
        return None
    try:
        return resolve(ref)
    except Exception:
        return None


def rule_status(out: pd.DataFrame, resolve, rule: dict) -> str:
    """Can this rule actually be evaluated? -> 'ok' | 'missing_column' |
    'unresolved_reference'.

    A blocking rule that cannot be evaluated must never be treated as 'passed' -
    that would let bad data through silently. The runner uses this to fail loudly
    instead.
    """
    col = rule.get("column")
    kind = str(rule.get("type", "")).lower()
    if not col or col not in out.columns:
        return "missing_column"
    if kind in ("range_pct", "within_pct", "in_master"):
        if _try_resolve(resolve, rule.get("reference", "")) is None:
            return "unresolved_reference"
    return "ok"


def evaluate(out: pd.DataFrame, resolve, rules: list[dict]) -> pd.DataFrame:
    """A tidy frame of every breach, for the 'Validation Issues' sheet."""
    rows: list[dict] = []
    for rule in rules or []:
        if not rule.get("enabled", True):
            continue
        bad, detail = rule_mask(out, resolve, rule)
        if not bad.any():
            continue
        hits = out.index[bad]
        positions = out.index.get_indexer(hits)
        for i, pos in zip(hits[:MAX_ISSUES_PER_RULE], positions[:MAX_ISSUES_PER_RULE]):
            rows.append({
                "Row": int(pos) + 2,           # 1-based, past the header
                "Column": rule.get("column"),
                "Rule": str(rule.get("type", "")).lower(),
                "Blocking": bool(rule.get("blocking")),
                "Value": _cell(out.at[i, rule["column"]]),
                "Problem": detail,
            })
        if len(hits) > MAX_ISSUES_PER_RULE:
            rows.append({
                "Row": None, "Column": rule.get("column"),
                "Rule": str(rule.get("type", "")).lower(),
                "Blocking": bool(rule.get("blocking")), "Value": None,
                "Problem": f"... and {len(hits) - MAX_ISSUES_PER_RULE:,} more row(s) "
                           f"breaching this rule ({len(hits):,} in total)",
            })
    return pd.DataFrame(rows, columns=["Row", "Column", "Rule", "Blocking", "Value", "Problem"])


def split(out: pd.DataFrame, resolve, rules: list[dict]) -> dict:
    """Divide `out` into clean and quarantined rows.

    A row is quarantined if it breaches any *enabled, blocking* rule. Each
    quarantined row carries a QC_REASON column naming the rule(s) it broke.
    Warn-level breaches are reported in `issues` but do not hold a row back.
    Blocking rules that cannot be evaluated are listed in summary['unusable_blocking'].
    """
    blocking = [r for r in (rules or [])
                if r.get("enabled", True) and r.get("blocking")]
    bad_any = pd.Series(False, index=out.index)
    reasons = pd.Series("", index=out.index, dtype=object)
    unusable: list[dict] = []

    for rule in blocking:
        status = rule_status(out, resolve, rule)
        if status != "ok":
            # A blocking rule we cannot evaluate is recorded, never assumed to pass.
            unusable.append({"column": rule.get("column"),
                             "type": str(rule.get("type", "")).lower(),
                             "reason": status})
            continue
        bad, detail = rule_mask(out, resolve, rule)
        if not bad.any():
            continue
        bad_any = bad_any | bad
        tag = f"{rule.get('column')}: {detail}"
        reasons.loc[bad] = reasons.loc[bad].map(
            lambda cur: f"{cur} | {tag}" if cur else tag)

    clean = out.loc[~bad_any].reset_index(drop=True)
    quarantine = out.loc[bad_any].copy()
    if len(quarantine):
        quarantine.insert(0, "QC_REASON", reasons.loc[bad_any].to_numpy())
        quarantine = quarantine.reset_index(drop=True)

    issues = evaluate(out, resolve, rules)
    summary = {
        "total_rows": int(len(out)),
        "clean_rows": int(len(clean)),
        "quarantined_rows": int(len(quarantine)),
        "blocking_rules": [f"{r.get('column')} {r.get('type')}" for r in blocking],
        "unusable_blocking": unusable,
        "issue_count": int(len(issues)),
        "blocking_issue_count": int(issues["Blocking"].sum()) if len(issues) else 0,
    }
    return {"clean": clean, "quarantine": quarantine, "issues": issues, "summary": summary}


def _cell(value):
    """A plain, Excel-friendly scalar for the issues sheet."""
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        f = float(value)
        return None if (np.isnan(f) or np.isinf(f)) else f
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    return value if isinstance(value, (int, float, str, bool)) else str(value)
