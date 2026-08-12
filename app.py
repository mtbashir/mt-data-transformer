"""Data Transformer - a local web app for turning transactional New Data into a
gap-filled output file shaped by Master Data and a Historical template.

Run:  python app.py     then open http://127.0.0.1:5000
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import traceback
import datetime as _dt

import pandas as pd
from flask import (Flask, jsonify, make_response, request, send_file, session,
                   send_from_directory)
from werkzeug.exceptions import HTTPException

from engine import ai, excel_io, formula, mapping, standardise, transform

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# The folder the app lives in - where the working files are kept. Override with
# DT_DATA_DIR to point the quick-load list somewhere else.
DATA_DIR = os.path.realpath(os.environ.get("DT_DATA_DIR") or os.path.dirname(BASE_DIR))
WORK_ROOT = os.path.join(tempfile.gettempdir(), "data_transformer")
os.makedirs(WORK_ROOT, exist_ok=True)

MAX_UPLOAD_MB = 200
ROLES = ("new", "master", "historical")

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.secret_key = os.environ.get("DT_SECRET_KEY") or secrets.token_hex(16)

# session id -> {"dir":..., "files": {role: {...}}, "frames": {role: DataFrame}, "config": {...}}
STORE: dict[str, dict] = {}
LOCK = threading.Lock()


# ==========================================================================
# session plumbing
# ==========================================================================
def _sid() -> str:
    if "sid" not in session:
        session["sid"] = secrets.token_hex(12)
    return session["sid"]


def _state() -> dict:
    sid = _sid()
    with LOCK:
        st = STORE.get(sid)
        if st is None:
            st = {"dir": os.path.join(WORK_ROOT, sid), "files": {}, "frames": {},
                  "config": {}, "outputs": {}}
            os.makedirs(st["dir"], exist_ok=True)
            STORE[sid] = st
    return st


def _frame(role: str) -> pd.DataFrame | None:
    return _state()["frames"].get(role)


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": str(msg)}), code


def _safe_name(name: str) -> str:
    name = os.path.basename(str(name or "file"))
    return re.sub(r"[^A-Za-z0-9._ \-()]+", "_", name)[:120] or "file"


@app.errorhandler(Exception)
def _on_error(e):
    if isinstance(e, tuple):
        return e
    # Let routing/HTTP errors (404 for /favicon.ico, and so on) stay themselves
    # instead of being reported as server crashes.
    if isinstance(e, HTTPException):
        return jsonify({"ok": False, "error": e.description}), e.code
    app.logger.error("Unhandled: %s\n%s", e, traceback.format_exc())
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


# ==========================================================================
# static
# ==========================================================================
def _asset_version() -> str:
    """Newest timestamp across the front-end files, used to bust the cache."""
    stamps = []
    for f in ("styles.css", "app.js", "index.html"):
        p = os.path.join(app.static_folder, f)
        if os.path.exists(p):
            stamps.append(os.path.getmtime(p))
    return str(int(max(stamps))) if stamps else "0"


@app.route("/")
def index():
    """Serve the page with versioned asset links.

    Browsers hold on to styles.css and app.js hard enough that a fix can look
    like it did nothing. Stamping the URLs means a changed file is always
    fetched, with no need for anyone to know about Ctrl+F5.
    """
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    v = _asset_version()
    html = html.replace("/static/styles.css", f"/static/styles.css?v={v}")
    html = html.replace("/static/app.js", f"/static/app.js?v={v}")
    html = html.replace("{{BUILD}}", v)
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.after_request
def _no_store_static(resp):
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/api/build")
def build():
    return jsonify({"ok": True, "build": _asset_version()})


# ==========================================================================
# step 1 - upload
# ==========================================================================
@app.post("/api/upload")
def upload():
    role = (request.form.get("role") or "").strip().lower()
    if role not in ROLES:
        return _err(f"Unknown role '{role}'.")
    if "file" not in request.files:
        return _err("No file was sent.")
    f = request.files["file"]
    if not f.filename:
        return _err("No file was selected.")
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in excel_io.ALLOWED_EXTS:
        return _err(f"Unsupported file type '{ext}'. "
                    f"Use {', '.join(sorted(excel_io.ALLOWED_EXTS))}.")

    st = _state()
    dest = os.path.join(st["dir"], f"{role}{ext}")
    f.save(dest)

    try:
        sheets = excel_io.list_sheets(dest)
    except Exception as e:
        return _err(f"Could not open the workbook: {e}")

    sheet = sheets[0] if sheets else None
    # For master files the interesting sheet is often not the first one.
    if role == "master" and len(sheets) > 1:
        best = max(sheets, key=lambda s: _sheet_score(dest, s))
        sheet = best

    return _load_sheet(role, dest, f.filename, sheet, None)


def _sheet_score(path: str, sheet: str) -> int:
    """Rough size heuristic - the widest/longest sheet is usually the data."""
    try:
        raw = excel_io._read_raw(path, sheet, nrows=30)
        return int(raw.notna().sum().sum())
    except Exception:
        return 0


def _load_sheet(role: str, path: str, original: str, sheet, header_row):
    st = _state()
    try:
        if header_row is None:
            header_row = excel_io.detect_header_row(path, sheet)
        df = excel_io.read_table(path, sheet, header_row)
    except Exception as e:
        return _err(f"Could not read sheet '{sheet}': {e}")
    if df.empty:
        return _err(f"Sheet '{sheet}' has no data rows below the header.")

    st["frames"][role] = df
    st["files"][role] = {
        "path": path, "name": original, "sheet": sheet,
        "header_row": int(header_row),
        "sheets": excel_io.list_sheets(path),
    }
    return jsonify({
        "ok": True, "role": role, "name": original, "sheet": sheet,
        "header_row": int(header_row), "sheets": st["files"][role]["sheets"],
        "preview": excel_io.preview(df),
    })


@app.get("/api/local-files")
def local_files():
    """Spreadsheets sitting next to the app, so the file dialog can be skipped.

    The Windows file picker hides everything when its filter does not match, and
    on a mapped Google Drive that leaves the user staring at an empty folder.
    Offering the folder's own files avoids the dialog altogether.
    """
    found = []
    for name in sorted(os.listdir(DATA_DIR)):
        path = os.path.join(DATA_DIR, name)
        if not os.path.isfile(path) or name.startswith("~$"):
            continue
        if excel_io.ext_of(name) not in excel_io.ALLOWED_EXTS:
            continue
        found.append({
            "name": name,
            "role": _guess_role(name),
            "size_kb": round(os.path.getsize(path) / 1024, 1),
        })
    return jsonify({"ok": True, "folder": DATA_DIR, "files": found})


def _guess_role(name: str) -> str | None:
    n = name.lower()
    if "master" in n or n.startswith("md"):
        return "master"
    if "hist" in n or "template" in n:
        return "historical"
    if "new" in n or "raw" in n or "input" in n:
        return "new"
    return None


# Set DT_LOCKED on shared/hosted instances to forbid touching files outside the
# app's own data folder. Left unset for the normal local desktop use, where
# appending to any workbook on your own machine is the whole point.
LOCKED = bool(os.environ.get("DT_LOCKED"))


def _resolve_target(name: str) -> str:
    """Absolute path to an append target the user named.

    A bare name resolves inside DATA_DIR. A full path is honoured as-is on a
    local install; when DT_LOCKED is set it is pulled back to its basename in
    DATA_DIR, so a hosted instance can never read or write elsewhere.
    """
    if os.path.isabs(name) and not LOCKED:
        return os.path.realpath(name)
    return os.path.realpath(os.path.join(DATA_DIR, os.path.basename(name)))


@app.get("/api/target-sheets")
def target_sheets():
    """Sheet names of a candidate append target, so step 5 can offer them."""
    name = request.args.get("name") or ""
    path = _resolve_target(name)
    if not os.path.isfile(path):
        return _err(f"Cannot find '{name}'.")
    ext = excel_io.ext_of(path)
    writable = ext in (".xlsx", ".xlsm", ".xltx")
    try:
        sheets = excel_io.list_sheets(path)
    except Exception as e:  # noqa: BLE001 - report unreadable file, don't crash
        return _err(f"Cannot read '{name}': {e}")
    return jsonify({"ok": True, "sheets": sheets, "writable": writable, "ext": ext})


@app.post("/api/load-local")
def load_local():
    """Load one of the files reported by /api/local-files."""
    data = request.get_json(force=True) or {}
    role = (data.get("role") or "").lower()
    name = data.get("name") or ""
    if role not in ROLES:
        return _err(f"Unknown role '{role}'.")

    # Resolve inside DATA_DIR only - never let a crafted name escape the folder.
    path = os.path.realpath(os.path.join(DATA_DIR, os.path.basename(name)))
    if os.path.dirname(path) != os.path.realpath(DATA_DIR) or not os.path.isfile(path):
        return _err("That file is not in the app's folder.")
    if excel_io.ext_of(path) not in excel_io.ALLOWED_EXTS:
        return _err(f"Unsupported file type '{excel_io.ext_of(path)}'.")

    st = _state()
    dest = os.path.join(st["dir"], f"{role}{excel_io.ext_of(path)}")
    shutil.copyfile(path, dest)
    try:
        sheets = excel_io.list_sheets(dest)
    except Exception as e:
        return _err(f"Could not open the workbook: {e}")
    sheet = sheets[0] if sheets else None
    if role == "master" and len(sheets) > 1:
        sheet = max(sheets, key=lambda s: _sheet_score(dest, s))
    return _load_sheet(role, dest, os.path.basename(path), sheet, None)


@app.post("/api/sheet")
def change_sheet():
    data = request.get_json(force=True) or {}
    role = (data.get("role") or "").lower()
    st = _state()
    if role not in st["files"]:
        return _err("Upload that file first.")
    info = st["files"][role]
    sheet = data.get("sheet", info["sheet"])
    header_row = data.get("header_row")
    if header_row is not None:
        try:
            header_row = int(header_row)
        except (TypeError, ValueError):
            header_row = None
    return _load_sheet(role, info["path"], info["name"], sheet, header_row)


@app.post("/api/reset")
def reset():
    sid = _sid()
    with LOCK:
        st = STORE.pop(sid, None)
    if st:
        shutil.rmtree(st["dir"], ignore_errors=True)
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/state")
def state():
    st = _state()
    files = {r: {k: v for k, v in info.items() if k != "path"}
             for r, info in st["files"].items()}
    return jsonify({
        "ok": True, "files": files,
        "columns": {r: [str(c) for c in df.columns] for r, df in st["frames"].items()},
        "ready": all(r in st["frames"] for r in ROLES),
    })


# ==========================================================================
# step 2/3/4 - configuration helpers
# ==========================================================================
@app.get("/api/functions")
def functions():
    return jsonify({"ok": True, "functions": [
        {"signature": s, "help": h} for s, h in formula.FUNC_HELP]})


@app.post("/api/validate-formula")
def validate_formula():
    data = request.get_json(force=True) or {}
    expr = data.get("expr", "")
    available = set(data.get("available") or [])
    if not available:
        available = set(_all_refs())
    problems = formula.validate(expr, available)
    return jsonify({"ok": not problems, "problems": problems,
                    "refs": formula.extract_refs(expr)})


def _all_refs() -> list[str]:
    st = _state()
    refs: list[str] = []
    for ns, role in (("new", "new"), ("master", "master"), ("donor", "historical")):
        df = st["frames"].get(role)
        if df is not None:
            refs += [f"{ns}.{c}" for c in df.columns]
    for c in (st["config"].get("output_columns") or []):
        if c.get("name"):
            refs.append(f"out.{c['name']}")
    return refs


@app.get("/api/refs")
def refs():
    return jsonify({"ok": True, "refs": _all_refs()})


ROLES_ALLOWED = ("dimension", "measure", "date", "ignore")


def _detect_role(s: pd.Series) -> str:
    """Classify a column as a dimension, a measure, or a date.

    Values decide, not the declared dtype: reading .xlsb hands back numeric
    columns as objects, which is how price columns were once mistaken for
    labels and offered up for 'standardisation'.
    """
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    if pd.api.types.is_bool_dtype(s):
        return "dimension"
    if mapping._numeric_like(s):
        return "measure"
    return "dimension"


def _detect_roles(df: pd.DataFrame) -> dict[str, str]:
    return {str(c): _detect_role(df[c]) for c in df.columns}


def _roles_for(role_cfg: dict | None, which: str, df: pd.DataFrame) -> dict[str, str]:
    """User's roles for one file, falling back to detection for anything unset."""
    detected = _detect_roles(df)
    chosen = ((role_cfg or {}).get(which) or {})
    for c, r in chosen.items():
        if c in detected and r in ROLES_ALLOWED:
            detected[c] = r
    return detected


@app.get("/api/column-roles")
def column_roles():
    """Detected role plus samples for every column, for the step 2 table."""
    st = _state()
    if not st["frames"]:
        return _err("Upload your files first.")
    out = {}
    for role, df in st["frames"].items():
        cols = []
        for c in df.columns:
            s = df[c]
            cols.append({
                "name": str(c),
                "detected": _detect_role(s),
                "unique": int(s.nunique(dropna=True)),
                "samples": [excel_io.json_safe(v) for v in s.dropna().head(3).tolist()],
            })
        out[role] = cols
    return jsonify({"ok": True, "files": out})


def _master_sheets() -> dict:
    """Every sheet of the Master Data workbook, for crosswalk detection."""
    st = _state()
    info = st["files"].get("master")
    if not info:
        return {}
    out = {}
    try:
        for sh in excel_io.list_sheets(info["path"]):
            try:
                out[sh] = excel_io.read_table(info["path"], sh)
            except Exception:
                continue
    except Exception:
        pass
    return out


@app.post("/api/standardise")
def standardise_report():
    """Compare New Data values against Master Data for each mapped dimension."""
    data = request.get_json(force=True) or {}
    st = _state()
    if not all(r in st["frames"] for r in ROLES):
        return _err("Upload all three files first.")
    dims = data.get("dims") or (st["config"].get("grid", {}) or {}).get("master_dims", [])
    if not dims:
        return _err("Map at least one dimension first.")

    new_df, master_df = st["frames"]["new"], st["frames"]["master"]
    role_cfg = data.get("column_roles") or st["config"].get("column_roles")
    new_roles = _roles_for(role_cfg, "new", new_df)
    master_roles = _roles_for(role_cfg, "master", master_df)

    # Only dimensions are standardised. A measure paired here is reported as
    # skipped rather than silently rewritten.
    checkable, skipped = [], []
    for d in dims:
        n, m = d.get("new"), d.get("master")
        if new_roles.get(n) == "dimension" and master_roles.get(m) == "dimension":
            checkable.append(d)
        elif n and m:
            bad = n if new_roles.get(n) != "dimension" else m
            skipped.append({"new_column": n, "master_column": m,
                            "reason": f"'{bad}' is set to "
                                      f"{new_roles.get(n) if bad == n else master_roles.get(m)}"
                                      " - only dimensions are standardised"})

    crosswalks = standardise.collect_crosswalks(_master_sheets(), master_df)
    report = standardise.build_report(
        new_df, master_df, checkable, crosswalks, data.get("decisions") or {}, new_roles)
    return jsonify({"ok": True, "dimensions": report, "skipped": skipped,
                    "crosswalk_columns": sorted(crosswalks.keys())})


@app.get("/api/suggest")
def suggest():
    """Propose a complete starting configuration from the three files."""
    st = _state()
    if not all(r in st["frames"] for r in ROLES):
        return _err("Upload all three files first.")
    new_df, master_df, hist_df = (st["frames"]["new"], st["frames"]["master"],
                                  st["frames"]["historical"])

    template_cols = [str(c) for c in hist_df.columns]
    from_new = mapping.suggest_column_map(template_cols, new_df, hist_df)
    from_master = mapping.suggest_column_map(template_cols, master_df, hist_df)

    # Dimensions: text columns shared across master and the template.
    dims = _suggest_dims(new_df, master_df, hist_df)
    date_new = _guess_date_column(new_df)
    date_hist = _guess_date_column(hist_df)

    output_columns = []
    for c in template_cols:
        if c in from_new:
            src = {"type": "new", "column": from_new[c]["column"]}
            why = f"New Data - {from_new[c]['why']}"
        elif c in from_master:
            src = {"type": "master", "column": from_master[c]["column"]}
            why = f"Master Data - {from_master[c]['why']}"
        elif date_hist and c == date_hist:
            src = {"type": "grid", "column": "DATE"}
            why = "Reporting date"
        else:
            src = {"type": "blank"}
            why = "No confident match - choose a source"
        output_columns.append({"name": c, "source": src, "suggestion": why,
                               "dtype": excel_io.dtype_label(hist_df[c])})

    # Dimension columns should come from Master Data so labels are standardised.
    dim_masters = {d["master"] for d in dims}
    for oc in output_columns:
        if oc["name"] in dim_masters and oc["source"]["type"] != "master":
            oc["source"] = {"type": "master", "column": oc["name"]}
            oc["suggestion"] = "Master Data - standardised label"

    measures = [c for c in template_cols
                if pd.api.types.is_numeric_dtype(hist_df[c]) and c not in dim_masters]

    cfg = {
        "grid": {
            "master_dims": dims,
            "new_date_column": date_new,
            "date_mode": "from_new",
            "dedupe": {"strategy": "first", "overrides": {}},
        },
        "output_columns": output_columns,
        "derived_columns": [],
        "column_roles": {r: _detect_roles(df) for r, df in st["frames"].items()},
        "attribute_map": _suggest_attribute_map(new_df, master_df),
        "standardise_decisions": {},
        "gapfill": {
            "enabled": True,
            "donor_strategy": "last",
            "hist_date_column": date_hist,
            "prefer": "historical",
            "match_chain": transform._default_chain(dims, hist_df, "hist"),
            "match_chain_new": transform._default_chain(dims, new_df, "new"),
            "rules": [],
            # Fill IDs by default. Blanking them marks a row as invented, but a
            # half-empty ID column reads as a bug, and a generated ID cannot
            # collide with a real one.
            "id_policy": {"column": _guess_id_column(hist_df), "mode": "unique",
                          "prefix": "", "start": None},
        },
        "validations": _suggest_validations(hist_df, master_df, measures),
        "add_flag": True,
        "flag_column": "SOURCE",
    }
    st["config"] = cfg
    return jsonify({
        "ok": True, "config": cfg,
        "template_columns": template_cols,
        "measures": measures,
        "columns": {r: [str(c) for c in df.columns] for r, df in st["frames"].items()},
        "stats": _grid_stats(new_df, master_df, dims, date_new),
    })


def _suggest_dims(new_df, master_df, hist_df) -> list[dict]:
    """Columns that define one output row: city, product, and friends.

    A dimension is only useful if its Master Data values actually appear in New
    Data - otherwise the join finds nothing and every row looks like a gap. So
    candidates are confirmed by value overlap, not just by name.
    """
    # Value sets for the two pools we search against, built once.
    _new_sets = mapping.value_sets(new_df)
    _hist_sets = mapping.value_sets(hist_df)

    candidates = []
    for c in master_df.columns:
        s = master_df[c]
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
            continue
        n = s.nunique(dropna=True)
        if n < 2 or n > max(400, len(master_df) * 0.5):
            continue
        candidates.append(str(c))

    preferred = ["city", "sku standard name", "sku", "product", "region"]
    scored: list[dict] = []
    for c in candidates:
        base = mapping.norm(c)
        rank = next((i for i, p in enumerate(preferred) if p == base), None)
        if rank is None:
            rank = next((i + 10 for i, p in enumerate(preferred) if p in base), None)
        if rank is None:
            continue
        new_match, new_ov = _best_value_match(master_df[c], new_df, _new_sets)
        if not new_match or new_ov < 0.5:
            continue  # a dimension we cannot join on is worse than no dimension
        hist_match, _ = _best_value_match(master_df[c], hist_df, _hist_sets)
        scored.append({"master": c, "new": new_match, "hist": hist_match,
                       "rank": rank, "overlap": new_ov})

    scored.sort(key=lambda d: (d["rank"], -d["overlap"]))

    # Drop dimensions that restate one another (REGION and CITY are the same
    # code here) and dimensions that reuse an already-taken New Data column.
    chosen: list[dict] = []
    for d in scored:
        dup = False
        for k in chosen:
            if d["new"] == k["new"]:
                dup = True
                break
            try:
                same = (master_df[d["master"]].astype(str).str.strip().str.upper()
                        == master_df[k["master"]].astype(str).str.strip().str.upper()).mean()
                if same > 0.95:
                    dup = True
                    break
            except Exception:
                pass
        if not dup:
            chosen.append(d)

    return [{"master": d["master"], "new": d["new"], "hist": d["hist"]} for d in chosen[:3]]


def _suggest_attribute_map(new_df, master_df, new_roles=None, master_roles=None) -> list[dict]:
    """Every dimension the two files share, for standardisation in step 2.

    Wider than the grid dimensions: BRAND and CATEGORY need standardising even
    though the output row is defined by city and product. Measures are excluded
    - rewriting a price of 1005 to 1000 because the digits look alike would
    corrupt the data.
    """
    new_roles = new_roles or _detect_roles(new_df)
    master_roles = master_roles or _detect_roles(master_df)

    def is_label(col: str, roles: dict, s: pd.Series) -> bool:
        if roles.get(str(col)) != "dimension":
            return False
        return 1 < s.nunique(dropna=True) <= 500

    # One value set per column, not one per pair (see mapping.value_sets).
    m_sets = mapping.value_sets(master_df)
    n_sets = mapping.value_sets(new_df)

    pairs: list[dict] = []
    used: set[str] = set()
    for m_col in master_df.columns:
        if not is_label(m_col, master_roles, master_df[m_col]):
            continue
        best, best_score = None, 0.0
        for n_col in new_df.columns:
            if n_col in used or not is_label(n_col, new_roles, new_df[n_col]):
                continue
            name = mapping.score_names(m_col, n_col)
            overlap = mapping.overlap_of(m_sets[str(m_col)], n_sets[str(n_col)])
            score = max(name, 0.5 * name + 0.7 * overlap)
            if score > best_score:
                best, best_score = str(n_col), score
        if best and best_score >= 0.7:
            pairs.append({"new": best, "master": str(m_col)})
            used.add(best)
    return pairs


def _best_value_match(ref: pd.Series, df: pd.DataFrame,
                      sets: dict | None = None) -> tuple[str | None, float]:
    """Column of `df` whose values best overlap `ref`, with the overlap score."""
    ref_set = mapping.value_set(ref)
    best, best_ov = None, 0.0
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            continue
        other = sets[str(c)] if sets is not None else mapping.value_set(df[c])
        ov = mapping.overlap_of(ref_set, other)
        if ov > best_ov:
            best, best_ov = str(c), ov
    return best, best_ov


def _best_match(col: str, df: pd.DataFrame) -> str | None:
    best, score = None, 0.0
    for c in df.columns:
        s = mapping.score_names(col, c)
        if s > score:
            best, score = str(c), s
    return best if score >= 0.7 else None


def _guess_date_column(df: pd.DataFrame) -> str | None:
    dated = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    named = [c for c in df.columns if re.search(r"date", str(c), re.I)]
    for pool in (set(dated) & set(named), dated, named):
        if pool:
            return str(sorted(pool, key=lambda c: list(df.columns).index(c))[0])
    return None


def _guess_id_column(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        if re.search(r"\b(visit\s*id|transaction\s*id|^id$)\b", str(c), re.I):
            return str(c)
    for c in df.columns:
        if re.search(r"id", str(c), re.I) and pd.api.types.is_numeric_dtype(df[c]):
            return str(c)
    return None


def _text_values(s: pd.Series) -> set[str]:
    v = s.dropna().astype(str).str.strip().str.upper()
    return set(v[v != ""].unique())


def _suggest_value_maps(new_df, master_df, hist_df) -> list[dict]:
    """Find label cross-walks (e.g. brand naming) worth applying.

    A crosswalk is only proposed when applying it measurably increases how many
    New Data values are recognised by Master Data. Suggesting one on name/shape
    alone produced nonsense like renaming BRAND into COMPANY, so the test here
    is the outcome, not the appearance.
    """
    st = _state()
    master_path = st["files"]["master"]["path"]
    try:
        sheets = excel_io.list_sheets(master_path)
    except Exception:
        sheets = []

    # Text columns worth considering on each side.
    new_cols = [c for c in new_df.columns
                if not pd.api.types.is_numeric_dtype(new_df[c])
                and not pd.api.types.is_datetime64_any_dtype(new_df[c])
                and 1 < new_df[c].nunique(dropna=True) <= 500]
    master_cols = [c for c in master_df.columns
                   if not pd.api.types.is_numeric_dtype(master_df[c])
                   and not pd.api.types.is_datetime64_any_dtype(master_df[c])
                   and 1 < master_df[c].nunique(dropna=True) <= 500]
    new_vals = {c: _text_values(new_df[c]) for c in new_cols}
    master_vals = {c: _text_values(master_df[c]) for c in master_cols}

    # Each New Data column is compared only against the Master Data column that
    # represents the same concept. Without this, a crosswalk that rewrites brand
    # names into company names scores as an 'improvement' against the COMPANY
    # column, and gets suggested as a rename of BRAND.
    partner: dict[str, str] = {}
    for nc in new_cols:
        best, best_score = None, 0.0
        for mc in master_cols:
            score = mapping.score_names(nc, mc)
            if score > best_score:
                best, best_score = mc, score
        if best and best_score >= 0.7:
            partner[nc] = best

    found: list[dict] = []
    for sh in sheets:
        try:
            df = excel_io.read_table(master_path, sh)
        except Exception:
            continue
        for pair in mapping.find_key_pairs(df):
            lut = {str(k).strip().upper(): str(v).strip()
                   for k, v in pair["pairs"].items()}
            for nc in new_cols:
                mc = partner.get(nc)
                src, ref = new_vals[nc], master_vals.get(mc or "", set())
                if not src or not ref:
                    continue
                mapped = {lut.get(v, v).strip().upper() for v in src}
                before = len(src & ref) / len(src)
                after = len(mapped & ref) / len(mapped)
                gain = after - before
                if gain >= 0.15 and after >= 0.6:
                    found.append({
                        "column": nc, "target": mc, "sheet": sh,
                        "from_column": pair["from_column"],
                        "to_column": pair["to_column"],
                        "pairs": pair["pairs"],
                        "keep_unmatched": True, "enabled": False,
                        "covers": len(src & set(lut)),
                        "gain": round(gain, 3),
                        "note": f"raises New Data [{nc}] values recognised by "
                                f"Master Data [{mc}] from {before:.0%} to {after:.0%}",
                    })

    best: dict[str, dict] = {}
    for m in found:
        cur = best.get(m["column"])
        if cur is None or m["gain"] > cur["gain"]:
            best[m["column"]] = m
    return sorted(best.values(), key=lambda m: -m["gain"])


def _suggest_validations(hist_df, master_df, measures) -> list[dict]:
    rules: list[dict] = []
    for c in measures:
        base = mapping.norm(c)
        if re.search(r"qty|quantity|discount|amount|sale|sales|net", base):
            rules.append({"column": c, "type": "non_negative", "enabled": True})
    # Price sanity against Master Data, where a comparable column exists.
    price_pairs = [("NTP", "NTP REGULAR"), ("UNIT TP", "UTP"), ("UNIT SP", "USP"),
                   ("CONSUMER PRICE", "USP")]
    for out_col, m_col in price_pairs:
        if out_col in hist_df.columns and m_col in master_df.columns:
            rules.append({"column": out_col, "type": "range_pct",
                          "reference": f"master.{m_col}", "pct": 10, "enabled": True})
    return rules


def _grid_stats(new_df, master_df, dims, date_col) -> dict:
    try:
        m_cols = [d["master"] for d in dims if d.get("master") in master_df.columns]
        combos = master_df.drop_duplicates(m_cols) if m_cols else master_df
        dates = 0
        if date_col and date_col in new_df.columns:
            dates = int(transform._norm_dates(new_df[date_col]).dropna().nunique())
        return {"combinations": int(len(combos)), "dates": dates,
                "expected_rows": int(len(combos) * max(dates, 1)),
                "new_rows": int(len(new_df))}
    except Exception:
        return {}


# ==========================================================================
# step 5 - run
# ==========================================================================
def _run(cfg: dict, persist: bool = True) -> dict:
    st = _state()
    missing = [r for r in ROLES if r not in st["frames"]]
    if missing:
        raise ValueError(f"Missing file(s): {', '.join(missing)}")
    cfg = dict(cfg or {})
    cfg["value_maps"] = [m for m in (cfg.get("value_maps") or []) if m.get("enabled")]
    cfg["validations"] = [v for v in (cfg.get("validations") or [])
                          if v.get("enabled", True)]
    if persist:
        st["config"] = cfg
    return transform.build_output(
        st["frames"]["new"], st["frames"]["master"], st["frames"]["historical"], cfg)


@app.get("/api/columns-index")
def columns_index():
    """Every referenceable column, for the formula autocomplete."""
    st = _state()
    if not st["frames"]:
        return _err("Upload your files first.")
    out = []
    for ns, role, label in (("new", "new", "New Data"),
                            ("master", "master", "Master Data"),
                            ("donor", "historical", "Historical Data")):
        df = st["frames"].get(role)
        if df is None:
            continue
        for c in df.columns:
            out.append({"ref": f"{ns}.{c}", "column": str(c), "file": label,
                        "ns": ns, "dtype": excel_io.dtype_label(df[c])})
    out.append({"ref": "grid.DATE", "column": "DATE", "file": "Grid",
                "ns": "grid", "dtype": "date"})
    for c in (st["config"].get("output_columns") or []):
        if c.get("name"):
            out.append({"ref": f"out.{c['name']}", "column": c["name"],
                        "file": "Output (built above)", "ns": "out", "dtype": "any"})
    return jsonify({"ok": True, "columns": out})


@app.post("/api/column-preview")
def column_preview():
    """Values each output column produces, from the first rows of real data."""
    data = request.get_json(force=True) or {}
    cfg = dict(data.get("config") or {})
    cfg["preview_limit"] = int(data.get("limit") or 25)
    cfg["validations"] = []
    try:
        res = _run(cfg, persist=False)
    except (ValueError, formula.FormulaError) as e:
        return _err(str(e))

    out = res["output"]
    rows = min(len(out), int(data.get("rows") or 3))
    values = {}
    for col in out.columns:
        values[str(col)] = [excel_io.json_safe(v) for v in out[col].head(rows).tolist()]

    # Source-side values for the same rows, so a formula can be sanity-checked
    # against what actually fed it.
    ctx = res.get("ctx")
    inputs = {}
    if ctx is not None:
        for ns, df in ctx.frames.items():
            for c in df.columns:
                inputs[f"{ns}.{c}"] = [excel_io.json_safe(v)
                                       for v in df[c].head(rows).tolist()]
    return jsonify({"ok": True, "rows": rows, "values": values, "inputs": inputs,
                    "source_flags": [excel_io.json_safe(v) for v in
                                     out.get("SOURCE", pd.Series(dtype=object))
                                     .head(rows).tolist()]})


# ==========================================================================
# AI-assisted formulas (optional)
# ==========================================================================
@app.get("/api/ai/status")
def ai_status():
    st = _state()
    return jsonify({"ok": True, "installed": ai.available(),
                    "configured": bool(st.get("ai_key")),
                    "model": ai.MODEL})


@app.post("/api/ai/key")
def ai_key():
    """Store the user's API key in this session's memory only.

    Never written to disk, never included in an exported profile, never logged.
    """
    data = request.get_json(force=True) or {}
    key = (data.get("api_key") or "").strip()
    st = _state()
    if not key:
        st.pop("ai_key", None)
        return jsonify({"ok": True, "configured": False})
    try:
        info = ai.check_key(key)
    except ai.AIError as e:
        return _err(str(e))
    st["ai_key"] = key
    return jsonify({"ok": True, "configured": True, "model": info["model"]})


@app.post("/api/ai/formula")
def ai_formula():
    data = request.get_json(force=True) or {}
    st = _state()
    key = st.get("ai_key")
    if not key:
        return _err("Add your Claude API key first.", 428)

    cols = {}
    for ns, role in (("new", "new"), ("master", "master"), ("donor", "historical")):
        df = st["frames"].get(role)
        if df is not None:
            cols[ns] = [str(c) for c in df.columns]
    cols["out"] = [c["name"] for c in (data.get("output_columns") or []) if c.get("name")]

    try:
        result = ai.suggest_formula(
            key, data.get("prompt", ""), cols,
            target_column=data.get("target_column"),
            samples=data.get("samples"))
    except ai.AIError as e:
        return _err(str(e))

    problems = formula.validate(result["formula"], set(_all_refs()))
    result["problems"] = problems
    return jsonify({"ok": True, **result})


@app.post("/api/preview")
def preview_run():
    data = request.get_json(force=True) or {}
    try:
        res = _run(data.get("config") or {})
    except (ValueError, formula.FormulaError) as e:
        return _err(str(e))
    out = res["output"]
    filled = out[out.get("SOURCE", pd.Series(index=out.index)).eq("FILLED")] \
        if "SOURCE" in out.columns else out.iloc[0:0]
    excluded = res.get("excluded")
    return jsonify({
        "ok": True,
        "report": res["report"],
        "preview": excel_io.preview(out, 40),
        "filled_preview": excel_io.preview(filled, 15) if len(filled) else None,
        "issues": excel_io.preview(res["issues"], 25) if len(res["issues"]) else None,
        "excluded": excel_io.preview(excluded, 25)
        if excluded is not None and len(excluded) else None,
    })


@app.post("/api/generate")
def generate():
    data = request.get_json(force=True) or {}
    try:
        res = _run(data.get("config") or {})
    except (ValueError, formula.FormulaError) as e:
        return _err(str(e))

    st = _state()

    # Append mode: add the output rows to the bottom of an existing workbook the
    # user names here, instead of writing a fresh file. Everything else in that
    # workbook is preserved.
    if (data.get("output_mode") or "new") == "append":
        target = data.get("append_to")
        if not target:
            return _err("Choose the existing file to append to.")
        tgt_path = _resolve_target(target)
        if not os.path.isfile(tgt_path):
            return _err(f"Cannot find '{target}'.")
        skip_dates = data.get("skip_existing_dates", True)
        try:
            info = excel_io.append_to_excel(
                res["output"], tgt_path, data.get("append_sheet") or None,
                skip_existing_dates=skip_dates)
        except (ValueError, PermissionError) as e:
            # PermissionError is the usual one: the file is open in Excel.
            msg = str(e)
            if isinstance(e, PermissionError):
                msg = (f"Could not write '{os.path.basename(tgt_path)}'. "
                       "Close it in Excel and try again.")
            return _err(msg)
        token = secrets.token_hex(8)
        st["outputs"][token] = tgt_path
        return jsonify({"ok": True, "token": token,
                        "filename": os.path.basename(tgt_path),
                        "appended": info["appended"], "sheet": info["sheet"],
                        "skipped": info["skipped"],
                        "skipped_dates": info["skipped_dates"],
                        "date_column": info["date_column"],
                        "report": res["report"], "mode": "append",
                        "size_kb": round(os.path.getsize(tgt_path) / 1024, 1)})

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = _safe_name(data.get("filename") or f"Output_Data_{stamp}.xlsx")
    if not fname.lower().endswith(".xlsx"):
        fname += ".xlsx"
    path = os.path.join(st["dir"], fname)

    sheets = {"Output Data": res["output"]}
    if len(res["issues"]):
        sheets["Validation Issues"] = res["issues"]
    excluded = res.get("excluded")
    if excluded is not None and len(excluded):
        sheets["Excluded New Rows"] = excluded
    additions = res.get("master_additions")
    if additions is not None and len(additions):
        # So the user can paste these straight into the real Master Data file.
        sheets["Master Data Additions"] = additions
    sheets["Run Summary"] = _summary_frame(res["report"], st)
    excel_io.write_excel(sheets, path)

    token = secrets.token_hex(8)
    st["outputs"][token] = path
    return jsonify({"ok": True, "token": token, "filename": fname,
                    "report": res["report"], "mode": "new",
                    "size_kb": round(os.path.getsize(path) / 1024, 1)})


def _summary_frame(report: dict, st: dict) -> pd.DataFrame:
    rows = [("Generated", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))]
    for role in ROLES:
        info = st["files"].get(role)
        if info:
            rows.append((f"{role.title()} file",
                         f"{info['name']} [sheet: {info['sheet']}]"))
    rows += [
        ("Total output rows", report.get("row_count")),
        ("Rows from New Data", report.get("actual_rows")),
        ("Gap-filled rows", report.get("filled_rows")),
        ("New Data rows excluded", report.get("excluded_new_rows")),
        ("Validation issues", report.get("issue_count")),
    ]
    rows += [("Step", s) for s in report.get("steps", [])]
    rows += [("Warning", w) for w in report.get("warnings", [])]
    return pd.DataFrame(rows, columns=["Item", "Detail"])


@app.get("/api/download/<token>")
def download(token):
    st = _state()
    path = st["outputs"].get(token)
    if not path or not os.path.exists(path):
        return _err("That download has expired. Generate the file again.", 404)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# ==========================================================================
# saved configurations
# ==========================================================================
@app.post("/api/config/export")
def config_export():
    data = request.get_json(force=True) or {}
    st = _state()
    path = os.path.join(st["dir"], "mapping_profile.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data.get("config") or st["config"], fh, indent=2, default=str)
    return send_file(path, as_attachment=True, download_name="mapping_profile.json")


@app.post("/api/config/import")
def config_import():
    if "file" not in request.files:
        return _err("No file was sent.")
    try:
        cfg = json.load(request.files["file"])
    except Exception as e:
        return _err(f"Not a valid profile file: {e}")
    _state()["config"] = cfg
    return jsonify({"ok": True, "config": cfg})


def _free_port(preferred: int = 5000, tries: int = 20) -> int:
    """First usable port at or after `preferred`.

    Without this, a server left running from an earlier launch makes the next
    one die with 'address already in use' - which looks like the app is simply
    broken.
    """
    import socket

    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # No SO_REUSEADDR here: on Windows it lets bind() succeed on a port
            # another process is actively listening on, so the probe would hand
            # back a busy port - the exact thing it exists to avoid.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port between {preferred} and {preferred + tries}.")


if __name__ == "__main__":
    import threading
    import webbrowser

    port = int(os.environ.get("PORT") or _free_port())
    url = f"http://127.0.0.1:{port}"
    print("\n" + "=" * 58)
    print("  Data Transformer")
    print(f"  Open:  {url}")
    print(f"  Build: {_asset_version()}")
    print("  Close this window to stop the app.")
    print("=" * 58 + "\n")

    if os.environ.get("DT_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
