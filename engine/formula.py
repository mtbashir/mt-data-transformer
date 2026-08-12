"""A small, safe, Excel-flavoured formula language evaluated over DataFrames.

Columns are referenced with square brackets: [NTP] * 1.05
Namespaced references reach the different row sources:
    [new.NTP]     value from the New Data row
    [master.UTP]  value from the matched Master Data row
    [donor.NTP]   value from the Historical Data donor row (gap rows)
    [out.BRAND]   a column already built on the output row

Evaluation is vectorised: every expression returns a pandas Series aligned to
the frame. Only a whitelisted set of AST nodes and functions can run, so user
supplied text can never execute arbitrary Python.
"""
from __future__ import annotations

import ast
import re

import numpy as np
import pandas as pd

COLUMN_REF = re.compile(r"\[([^\[\]]+)\]")


class FormulaError(ValueError):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _series(x, index: pd.Index) -> pd.Series:
    if isinstance(x, pd.Series):
        return x.reindex(index) if not x.index.equals(index) else x
    return pd.Series([x] * len(index), index=index)


def _num(x, index: pd.Index) -> pd.Series:
    return pd.to_numeric(_series(x, index), errors="coerce")


def _text(x, index: pd.Index) -> pd.Series:
    s = _series(x, index)
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d").fillna("")
    out = s.astype(object).where(s.notna(), "")

    def one(v):
        if v == "" or v is None:
            return ""
        if isinstance(v, float) and float(v).is_integer():
            return str(int(v))
        return str(v)

    return out.map(one).astype(str)


# --------------------------------------------------------------------------
# function library
# --------------------------------------------------------------------------
def _fn_concat(idx, *args):
    if not args:
        return pd.Series([""] * len(idx), index=idx)
    out = _text(args[0], idx)
    for a in args[1:]:
        out = out + _text(a, idx)
    return out


def _fn_if(idx, cond, a, b):
    c = _series(cond, idx).fillna(False).astype(bool)
    return pd.Series(np.where(c, _series(a, idx), _series(b, idx)), index=idx)


def _fn_randbetween(idx, lo, hi):
    lo_s = _num(lo, idx).fillna(0)
    hi_s = _num(hi, idx).fillna(0)
    rng = np.random.default_rng()
    draws = rng.random(len(idx))
    lo_v, hi_v = lo_s.to_numpy(float), hi_s.to_numpy(float)
    span = np.floor(hi_v) - np.ceil(lo_v) + 1
    span = np.where(span < 1, 1, span)
    return pd.Series(np.ceil(lo_v) + np.floor(draws * span), index=idx)


def _fn_rand(idx):
    return pd.Series(np.random.default_rng().random(len(idx)), index=idx)


def _fn_round(idx, x, digits=0):
    d = _num(digits, idx).fillna(0).astype(int)
    v = _num(x, idx)
    factor = np.power(10.0, d.to_numpy(float))
    arr = v.to_numpy(float)
    with np.errstate(invalid="ignore"):
        r = np.round(arr * factor) / factor
    return pd.Series(r, index=idx)


def _fn_iferror(idx, value, fallback):
    v = _series(value, idx)
    return v.where(v.notna(), _series(fallback, idx))


def _fn_coalesce(idx, *args):
    if not args:
        raise FormulaError("COALESCE needs at least one argument")
    out = _series(args[0], idx)
    for a in args[1:]:
        out = out.where(out.notna(), _series(a, idx))
    return out


def _reduce_numeric(idx, args, ufunc):
    cols = [_num(a, idx) for a in args]
    if not cols:
        raise FormulaError("function needs at least one argument")
    frame = pd.concat(cols, axis=1)
    return getattr(frame, ufunc)(axis=1)


def _fn_text_pad(idx, x, width, char="0"):
    w = int(_num(width, idx).fillna(0).max() or 0)
    c = _text(char, idx).iloc[0] if len(idx) else "0"
    return _text(x, idx).str.rjust(w, c or "0")


FUNCS = {
    "CONCAT": _fn_concat,
    "CONCATENATE": _fn_concat,
    "IF": _fn_if,
    "IFERROR": _fn_iferror,
    "COALESCE": _fn_coalesce,
    "RANDBETWEEN": _fn_randbetween,
    "RAND": _fn_rand,
    "ROUND": _fn_round,
    "ROUNDUP": lambda idx, x, d=0: pd.Series(
        np.ceil(_num(x, idx).to_numpy(float) * 10 ** int(_num(d, idx).fillna(0).max() or 0))
        / 10 ** int(_num(d, idx).fillna(0).max() or 0), index=idx),
    "ROUNDDOWN": lambda idx, x, d=0: pd.Series(
        np.floor(_num(x, idx).to_numpy(float) * 10 ** int(_num(d, idx).fillna(0).max() or 0))
        / 10 ** int(_num(d, idx).fillna(0).max() or 0), index=idx),
    "INT": lambda idx, x: pd.Series(np.floor(_num(x, idx).to_numpy(float)), index=idx),
    "ABS": lambda idx, x: _num(x, idx).abs(),
    "SUM": lambda idx, *a: _reduce_numeric(idx, a, "sum"),
    "AVERAGE": lambda idx, *a: _reduce_numeric(idx, a, "mean"),
    "MIN": lambda idx, *a: _reduce_numeric(idx, a, "min"),
    "MAX": lambda idx, *a: _reduce_numeric(idx, a, "max"),
    "UPPER": lambda idx, x: _text(x, idx).str.upper(),
    "LOWER": lambda idx, x: _text(x, idx).str.lower(),
    "PROPER": lambda idx, x: _text(x, idx).str.title(),
    "TRIM": lambda idx, x: _text(x, idx).str.strip(),
    "LEN": lambda idx, x: _text(x, idx).str.len(),
    "LEFT": lambda idx, x, n=1: _text(x, idx).str[: int(_num(n, idx).fillna(1).max() or 1)],
    "RIGHT": lambda idx, x, n=1: _text(x, idx).str[-int(_num(n, idx).fillna(1).max() or 1):],
    "TEXT": lambda idx, x, *_: _text(x, idx),
    "VALUE": lambda idx, x: _num(x, idx),
    "PAD": _fn_text_pad,
    "YEAR": lambda idx, x: pd.to_datetime(_series(x, idx), errors="coerce").dt.year,
    "MONTH": lambda idx, x: pd.to_datetime(_series(x, idx), errors="coerce").dt.month,
    "DAY": lambda idx, x: pd.to_datetime(_series(x, idx), errors="coerce").dt.day,
    "ISBLANK": lambda idx, x: _series(x, idx).isna(),
    "NOT": lambda idx, x: ~_series(x, idx).fillna(False).astype(bool),
}

FUNC_HELP = [
    ("CONCAT(a, b, ...)", "Join values into text"),
    ("IF(test, a, b)", "Conditional"),
    ("IFERROR(v, fallback)", "Use fallback when v is blank/invalid"),
    ("COALESCE(a, b, ...)", "First non-blank value"),
    ("RANDBETWEEN(lo, hi)", "Random whole number per row"),
    ("RAND()", "Random 0-1 per row"),
    ("ROUND(x, digits)", "Round (also ROUNDUP / ROUNDDOWN / INT)"),
    ("ABS(x)", "Absolute value"),
    ("SUM/AVERAGE/MIN/MAX(a, b, ...)", "Across the listed columns"),
    ("UPPER/LOWER/PROPER/TRIM(x)", "Text case and spacing"),
    ("LEFT/RIGHT(x, n)  LEN(x)", "Substring and length"),
    ("PAD(x, width, char)", "Pad on the left, e.g. PAD([ID], 6, '0')"),
    ("YEAR/MONTH/DAY(date)", "Date parts"),
    ("ISBLANK(x)  NOT(x)", "Tests"),
]


# --------------------------------------------------------------------------
# parsing / evaluation
# --------------------------------------------------------------------------
_BIN = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_CMP = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


def normalise(expr: str) -> str:
    """Tidy an expression the way a spreadsheet user is likely to have typed it.

    Excel formulas start with '='. Left in place that becomes '==' under the
    equality rewrite below, so `=[NTP]*2` failed to parse - the single most
    likely thing for someone coming from Excel to write.
    """
    s = str(expr or "").strip()
    while s.startswith("="):
        s = s[1:].lstrip()
    return s


def extract_refs(expr: str) -> list[str]:
    """Column references used by an expression, in order of appearance."""
    seen, out = set(), []
    for m in COLUMN_REF.finditer(normalise(expr)):
        name = m.group(1).strip()
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _tokenise(expr: str) -> tuple[str, dict[str, str]]:
    """Replace [Col Name] with safe identifiers so Python's ast can parse it."""
    mapping: dict[str, str] = {}

    def sub(m):
        name = m.group(1).strip()
        key = f"_c{len(mapping)}_"
        mapping[key] = name
        return key

    return COLUMN_REF.sub(sub, expr or ""), mapping


def evaluate(expr: str, resolve, index: pd.Index) -> pd.Series:
    """Evaluate `expr`, using resolve(column_name) -> Series/scalar for refs."""
    cleaned = normalise(expr)
    if cleaned == "":
        return pd.Series([None] * len(index), index=index)

    py, refs = _tokenise(cleaned)
    # Excel-isms that differ from Python.
    py = re.sub(r"(?<![<>!=])=(?!=)", "==", py)
    py = py.replace("<>", "!=").replace("&", "+")

    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError as e:
        raise FormulaError(_syntax_hint(cleaned, e)) from e

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in refs:
                return resolve(refs[node.id])
            if node.id.upper() in ("TRUE", "FALSE"):
                return node.id.upper() == "TRUE"
            # Adjacent [A][B] tokenises into one identifier; report that in the
            # user's terms rather than leaking the internal placeholder name.
            if re.fullmatch(r"(_c\d+_){2,}", node.id):
                joined = " ".join(f"[{refs[m]}]" for m in
                                  re.findall(r"_c\d+_", node.id) if m in refs)
                raise FormulaError(
                    f"Columns {joined} sit side by side with nothing between "
                    "them. Join them with CONCAT(...) or an operator such as *.")
            raise FormulaError(f"Unknown name '{node.id}'. Wrap column names in [brackets].")
        if isinstance(node, ast.BinOp):
            op = _BIN.get(type(node.op))
            if op is None:
                raise FormulaError("Unsupported operator")
            left, right = walk(node.left), walk(node.right)
            # '+' concatenates when either side is text.
            if isinstance(node.op, ast.Add) and (_is_texty(left) or _is_texty(right)):
                return _text(left, index) + _text(right, index)
            return op(_num(left, index), _num(right, index))
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_num(walk(node.operand), index)
            if isinstance(node.op, ast.UAdd):
                return _num(walk(node.operand), index)
            if isinstance(node.op, ast.Not):
                return ~_series(walk(node.operand), index).fillna(False).astype(bool)
            raise FormulaError("Unsupported unary operator")
        if isinstance(node, ast.BoolOp):
            parts = [_series(walk(v), index).fillna(False).astype(bool) for v in node.values]
            out = parts[0]
            for p in parts[1:]:
                out = (out & p) if isinstance(node.op, ast.And) else (out | p)
            return out
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                raise FormulaError("Chained comparisons are not supported")
            op = _CMP.get(type(node.ops[0]))
            if op is None:
                raise FormulaError("Unsupported comparison")
            left, right = walk(node.left), walk(node.comparators[0])
            if _is_texty(left) or _is_texty(right):
                return op(_text(left, index), _text(right, index))
            return op(_num(left, index), _num(right, index))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise FormulaError("Unsupported function call")
            fname = node.func.id.upper()
            fn = FUNCS.get(fname)
            if fn is None:
                raise FormulaError(f"Unknown function '{node.func.id}'")
            if node.keywords:
                raise FormulaError("Named arguments are not supported")
            return fn(index, *[walk(a) for a in node.args])
        raise FormulaError("Unsupported expression element")

    result = walk(tree)
    return _series(result, index)


def _syntax_hint(expr: str, err: SyntaxError) -> str:
    """Turn Python's terse parser message into something actionable.

    'invalid syntax' on its own tells a spreadsheet user nothing, so name the
    likely cause where we can recognise it.
    """
    opens, closes = expr.count("("), expr.count(")")
    if opens != closes:
        missing = "closing" if opens > closes else "opening"
        return (f"Unbalanced brackets in \"{expr}\" - "
                f"{abs(opens - closes)} {missing} bracket(s) missing.")
    if re.search(r",\s*,", expr) or re.search(r"\(\s*,", expr):
        return f"Empty argument in \"{expr}\" - remove the extra comma."
    if re.search(r"[\+\-\*/&]\s*$", expr):
        return f"\"{expr}\" ends with an operator - something is missing after it."
    if re.search(r"\]\s*\[", expr):
        return (f"Two columns sit side by side in \"{expr}\" - "
                "join them with CONCAT(...) or an operator such as *.")
    return f"Cannot parse \"{expr}\": {err.msg}."


def _is_texty(v) -> bool:
    if isinstance(v, str):
        return True
    if isinstance(v, pd.Series):
        return not pd.api.types.is_numeric_dtype(v) and not pd.api.types.is_datetime64_any_dtype(v)
    return False


def validate(expr: str, available: set[str]) -> list[str]:
    """Return a list of human-readable problems with an expression."""
    problems: list[str] = []
    if not normalise(expr):
        return ["Formula is empty"]
    for ref in extract_refs(expr):
        if ref not in available:
            problems.append(f"Unknown column reference [{ref}]")
    try:
        idx = pd.RangeIndex(3)
        evaluate(expr, lambda name: pd.Series([1.0, 2.0, 3.0], index=idx), idx)
    except FormulaError as e:
        problems.append(str(e))
    except Exception:
        # Type-driven failures on dummy numbers are not conclusive; the real
        # run will surface them with proper data.
        pass
    return problems
