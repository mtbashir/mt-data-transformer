"""Auto-suggestion of column mappings and value (label) mappings.

Two different problems are solved here:

* Column mapping - 'NTP' in New Data is the same field as 'NTP' in the output
  template, 'TP' maps to 'INV AMOUNT', and so on.
* Value mapping - the same real-world brand is written 'DIET COLA' in the
  transactional file but 'COLA' in Master Data. Master Data's KEYS sheet
  holds these cross-walks as side-by-side column pairs.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import pandas as pd

# Domain shorthand that string similarity alone would never connect.
SYNONYMS: list[tuple[set[str], set[str]]] = [
    ({"tp", "trade price", "inv amount", "invoice amount"}, {"tp", "inv amount"}),
    ({"ntp", "net trade price"}, {"ntp", "ntp regular"}),
    ({"sp", "consumer price", "retail price", "usp"}, {"consumer price", "usp", "unit sp"}),
    ({"utp", "unit tp", "per unit trade price"}, {"utp", "unit tp"}),
    ({"qty", "quantity"}, {"quantity"}),
    ({"disc", "discount"}, {"discount"}),
    ({"date", "visit date", "txn date"}, {"date"}),
    ({"id", "visit id", "transaction id"}, {"visit id", "id"}),
    ({"sku", "sku standard name", "product"}, {"sku standard name"}),
    ({"m.cat", "master cat", "master category"}, {"m.cat", "master cat"}),
    ({"cat", "category"}, {"category"}),
    ({"a/q", "aq", "amount/qty"}, {"a/q"}),
]


def norm(s) -> str:
    """Loose normalisation for name comparison."""
    s = str(s or "").lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _synonym_bonus(a: str, b: str) -> float:
    for left, right in SYNONYMS:
        if (a in left and b in right) or (a in right and b in left):
            return 0.45
    return 0.0


def score_names(a: str, b: str) -> float:
    """0-1 similarity between two column names."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    base = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        base = max(base, 0.35 + 0.65 * jaccard) if jaccard else base
    if na in nb or nb in na:
        base = max(base, 0.82)
    return min(1.0, base + _synonym_bonus(na, nb))


def _numeric_like(s: pd.Series) -> bool:
    """True for numeric columns, and for text columns that are really numbers."""
    if pd.api.types.is_numeric_dtype(s):
        return True
    if pd.api.types.is_datetime64_any_dtype(s):
        return False
    sample = s.dropna().head(500)
    if sample.empty:
        return True  # nothing to contradict it
    return pd.to_numeric(sample, errors="coerce").notna().mean() > 0.8


_SAMPLE = 4000


def value_set(s: pd.Series) -> frozenset:
    """Normalised sample of a column's values.

    `head` before `astype(str)`: converting a 250k-row column to strings and
    then keeping 4,000 of them pays for the whole column every time.
    """
    try:
        vals = s.dropna().head(_SAMPLE).astype(str)
        return frozenset({norm(v) for v in vals} - {""})
    except Exception:
        return frozenset()


def value_sets(df: pd.DataFrame, columns=None) -> dict:
    """Build every column's value set once, for loops that compare many pairs."""
    return {str(c): value_set(df[c]) for c in (columns if columns is not None else df.columns)}


def overlap_of(sa: frozenset, sb: frozenset) -> float:
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def value_overlap(a: pd.Series, b: pd.Series) -> float:
    """How much two columns' value sets overlap - strong evidence of a match.

    Convenience wrapper for one-off comparisons. In a loop over many pairs use
    `value_sets` + `overlap_of` instead: building the sets once per column turns
    an N x M problem into N + M.
    """
    return overlap_of(value_set(a), value_set(b))


_value_overlap = value_overlap  # backwards-compatible alias


def suggest_column_map(
    target_cols: list[str],
    source_df: pd.DataFrame,
    target_df: pd.DataFrame | None = None,
    threshold: float = 0.62,
) -> dict[str, dict]:
    """For each target column, propose the best source column.

    Name similarity is combined with value-set overlap when both frames are
    available, which is what correctly links differently-named key columns.
    """
    suggestions: dict[str, dict] = {}
    src_cols = list(source_df.columns)

    # Build each column's value set once. Doing it inside the nested loop meant
    # sampling the same column hundreds of times - on a 250k-row template that
    # was the difference between two minutes and a couple of seconds.
    src_sets = value_sets(source_df, src_cols)
    src_numeric = {str(s): _numeric_like(source_df[s]) for s in src_cols}
    tgt_sets = value_sets(target_df, [t for t in target_cols if t in target_df.columns]) \
        if target_df is not None else {}

    for t in target_cols:
        best, best_score, best_why, best_overlap = None, 0.0, "", 0.0
        # Label-like columns are judged on shared values as well as name, since
        # a name-only match there produces a join that silently finds nothing.
        checkable = target_df is not None and t in target_df.columns
        numeric_target = checkable and pd.api.types.is_numeric_dtype(target_df[t])
        labelish = checkable and not numeric_target \
            and not pd.api.types.is_datetime64_any_dtype(target_df[t])
        tgt_set = tgt_sets.get(str(t), frozenset())

        for s in src_cols:
            # A numeric measure must not be fed from a text column: 'RET MARGIN'
            # scores a deceptively high name similarity against 'REGION', and
            # would otherwise be filled with city codes.
            if numeric_target and not src_numeric[str(s)]:
                continue
            name_score = score_names(t, s)
            overlap = overlap_of(src_sets[str(s)], tgt_set) if checkable else 0.0
            combined = max(name_score, 0.55 * name_score + 0.75 * overlap)
            if combined > best_score:
                best, best_score, best_overlap = s, combined, overlap
                best_why = "exact name" if name_score >= 0.999 else (
                    f"{int(overlap * 100)}% shared values" if overlap > 0.5
                    else f"{int(name_score * 100)}% name match")

        if best is None or best_score < threshold:
            continue
        # Reject a text match that shares no actual values unless the names are
        # effectively identical - e.g. 'UNIQUE ID' vs 'ID'.
        if labelish and best_overlap < 0.15 and score_names(t, best) < 0.95:
            continue
        suggestions[t] = {"column": best, "score": round(best_score, 3), "why": best_why}
    return suggestions


def find_key_pairs(df: pd.DataFrame, min_rows: int = 4) -> list[dict]:
    """Detect side-by-side lookup blocks in a sheet like Master Data 'KEYS'.

    A pair of adjacent columns qualifies when both are mostly text, the left
    side is (near) unique, and the two are not identical - i.e. it reads as
    'this label' -> 'that label'.
    """
    pairs: list[dict] = []
    cols = list(df.columns)
    for i in range(len(cols) - 1):
        left, right = df[cols[i]], df[cols[i + 1]]
        block = pd.DataFrame({"l": left, "r": right}).dropna()
        if len(block) < min_rows:
            continue
        l_txt = block["l"].astype(str).str.strip()
        r_txt = block["r"].astype(str).str.strip()
        block = block[(l_txt != "") & (r_txt != "")]
        if len(block) < min_rows:
            continue
        l_txt, r_txt = l_txt.loc[block.index], r_txt.loc[block.index]
        uniq_ratio = l_txt.nunique() / len(l_txt)
        identical = (l_txt.str.lower() == r_txt.str.lower()).mean()
        if uniq_ratio < 0.9 or identical > 0.95:
            continue
        mapping = dict(zip(l_txt, r_txt))
        pairs.append({
            "from_column": str(cols[i]),
            "to_column": str(cols[i + 1]),
            "size": len(mapping),
            "pairs": mapping,
            "identical_ratio": round(float(identical), 3),
        })
    return pairs


def build_value_map(
    df: pd.DataFrame, from_col: str, to_col: str, case_insensitive: bool = True
) -> dict[str, str]:
    block = df[[from_col, to_col]].dropna()
    out: dict[str, str] = {}
    for a, b in zip(block[from_col].astype(str), block[to_col].astype(str)):
        key = a.strip()
        if case_insensitive:
            key = key.upper()
        if key and key not in out:
            out[key] = b.strip()
    return out


def apply_value_map(s: pd.Series, mapping: dict[str, str], case_insensitive: bool = True,
                    keep_unmatched: bool = True) -> pd.Series:
    if not mapping:
        return s
    txt = s.astype(object).where(s.notna(), None)

    def one(v):
        if v is None:
            return None
        key = str(v).strip()
        if case_insensitive:
            key = key.upper()
        if key in mapping:
            return mapping[key]
        return v if keep_unmatched else None

    return txt.map(one)


def unmatched_values(s: pd.Series, mapping: dict[str, str], case_insensitive: bool = True,
                     limit: int = 200) -> list[str]:
    """Distinct source values with no entry in the mapping - shown for review."""
    vals = s.dropna().astype(str).str.strip()
    vals = vals[vals != ""]
    seen, out = set(), []
    for v in vals.unique():
        key = v.upper() if case_insensitive else v
        if key not in mapping and v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) >= limit:
            break
    return out
