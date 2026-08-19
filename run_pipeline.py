"""Headless pipeline runner - the 'agent'.

Runs the same transformation the Data Transformer web app runs, but with no UI:
it resolves the three input files, applies a saved mapping profile, standardises
labels + gap-fills, runs QC as a *gate* (rows breaking a blocking rule are
quarantined, the rest are loaded), appends the clean rows to the Excel database
file, and writes logs + an alert whenever anything needs a human.

Usually launched by watch.py when a new file lands. Can also be run directly.

    python run_pipeline.py --config <cfg>                 # normal run
    python run_pipeline.py --config <cfg> --new-file X.xlsx   # use this New Data file
    python run_pipeline.py --config <cfg> --dry-run       # skip the DB load

Exit codes:  0 = success (even if some rows were quarantined / excluded)
             1 = the run failed and nothing was loaded
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys
import traceback

import pandas as pd

from engine import excel_io, transform, qc, load

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROLES = ("new", "master", "historical")


class Log:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str):
        line = f"[{_dt.datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines)


def load_config(path):
    path = path or os.environ.get("DT_PIPELINE_CONFIG") or os.path.join(BASE_DIR, "pipeline_config.json")
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    return cfg, path


def _guess_role(name: str):
    n = name.lower()
    if "master" in n or n.startswith("md"):
        return "master"
    if "hist" in n or "template" in n or "db final" in n or "sales db" in n:
        return "historical"
    if "new" in n or "raw" in n or "input" in n:
        return "new"
    return None


def resolve_files(data_dir: str, cfg: dict, log: Log, new_file: str | None = None) -> dict:
    named = (cfg.get("files") or {})
    found: dict[str, str] = {}

    # An explicit --new-file wins for the 'new' role.
    if new_file:
        p = new_file if os.path.isabs(new_file) else os.path.join(data_dir, new_file)
        if not os.path.isfile(p):
            raise FileNotFoundError(f"--new-file not found: {p}")
        found["new"] = p

    for role in ROLES:
        if role in found:
            continue
        fn = named.get(role)
        if fn:
            p = fn if os.path.isabs(fn) else os.path.join(data_dir, fn)
            if not os.path.isfile(p):
                raise FileNotFoundError(f"{role}: '{fn}' not found in {data_dir}")
            found[role] = p

    if len(found) < len(ROLES):
        for name in sorted(os.listdir(data_dir)):
            p = os.path.join(data_dir, name)
            if not os.path.isfile(p) or name.startswith("~$"):
                continue
            if excel_io.ext_of(name) not in excel_io.ALLOWED_EXTS:
                continue
            role = _guess_role(name)
            if role and role not in found:
                found[role] = p

    missing = [r for r in ROLES if r not in found]
    if missing:
        raise FileNotFoundError(
            f"Could not find file(s) for role(s): {', '.join(missing)}. "
            "Set them explicitly in the config -> files.")
    for role in ROLES:
        log(f"{role:<10} -> {os.path.basename(found[role])}")
    return found


def _sheet_score(path: str, sheet: str) -> int:
    try:
        raw = excel_io._read_raw(path, sheet, nrows=30)
        return int(raw.notna().sum().sum())
    except Exception:
        return 0


# Sheets this app writes alongside the data. A previous output workbook is the
# most natural thing to hand back as the template, so these must never be
# mistaken for the data itself - 'Quarantined Rows' is wider than 'Output Data'
# and would otherwise win a "most cells wins" contest.
OUTPUT_SHEET = "output data"
AUX_SHEETS = {"quarantined rows", "validation issues", "excluded new rows",
              "master additions", "master data additions", "run summary"}


def resolve_sheet(role: str, path: str, cfg: dict) -> str | None:
    """Pick the sheet to read for a role, mirroring the web app."""
    override = (cfg.get("sheets") or {}).get(role) or {}
    if override.get("sheet"):
        return override["sheet"]
    sheets = excel_io.list_sheets(path)
    sheet = sheets[0] if sheets else None
    if role in ("master", "historical") and len(sheets) > 1:
        # A workbook this app produced: take its data sheet by name.
        exact = [s for s in sheets if str(s).strip().casefold() == OUTPUT_SHEET]
        if exact:
            return exact[0]
        real = [s for s in sheets if str(s).strip().casefold() not in AUX_SHEETS]
        sheet = max(real or sheets, key=lambda s: _sheet_score(path, s))
    return sheet


def read_role(role: str, path: str, cfg: dict, log: Log):
    override = (cfg.get("sheets") or {}).get(role) or {}
    sheet = resolve_sheet(role, path, cfg)
    df = excel_io.read_table(path, sheet, override.get("header_row"))
    log(f"{role:<10} sheet '{sheet}' -> {len(df):,} rows x {df.shape[1]} cols")
    return df, sheet


def assemble_rules(profile: dict, cfg: dict) -> list[dict]:
    qc_cfg = cfg.get("qc") or {}
    rules = [dict(r) for r in (profile.get("validations") or []) if r.get("enabled", True)]
    rules += [dict(r) for r in (qc_cfg.get("extra_validations") or [])]
    policy = qc_cfg.get("blocking", "all")
    warn_only = {(w.get("column"), str(w.get("type", "")).lower())
                 for w in (qc_cfg.get("warn_only") or [])}
    for r in rules:
        key = (r.get("column"), str(r.get("type", "")).lower())
        if policy == "none":
            r["blocking"] = False
        elif policy == "all":
            r["blocking"] = key not in warn_only
        elif isinstance(policy, list):
            want = {(b.get("column"), str(b.get("type", "")).lower()) for b in policy}
            r["blocking"] = key in want and key not in warn_only
        else:
            r["blocking"] = bool(r.get("blocking"))
    return rules


def write_alert(logs_dir: str, subject: str, body: str, cfg: dict, log: Log):
    os.makedirs(logs_dir, exist_ok=True)
    alert_path = os.path.join(logs_dir, f"ALERT_{_dt.datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(alert_path, "w", encoding="utf-8") as fh:
        fh.write(subject + "\n\n" + body)
    log(f"ALERT written: {alert_path}")
    email = ((cfg.get("alert") or {}).get("email") or {})
    if email.get("enabled"):
        try:
            _send_email(email, subject, body)
            log(f"Alert emailed to {email.get('to')}")
        except Exception as e:
            log(f"Email alert failed ({e}); the alert file was still written.")


def _send_email(email: dict, subject: str, body: str):
    import smtplib
    from email.message import EmailMessage
    pw = os.environ.get(email.get("use_env_password", "DT_SMTP_PASSWORD"), "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email.get("from") or email.get("user")
    msg["To"] = email.get("to")
    msg.set_content(body)
    with smtplib.SMTP(email["host"], int(email.get("port", 587)), timeout=30) as s:
        s.starttls()
        if email.get("user"):
            s.login(email["user"], pw)
        s.send_message(msg)


def save_status(logs_dir: str, status: dict):
    os.makedirs(logs_dir, exist_ok=True)
    with open(os.path.join(logs_dir, "last_run.json"), "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, default=str)


def save_log(logs_dir: str, log: Log) -> str:
    os.makedirs(logs_dir, exist_ok=True)
    p = os.path.join(logs_dir, f"run_{_dt.datetime.now():%Y%m%d_%H%M%S}.log")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(log.text())
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Headless Data Transformer pipeline.")
    ap.add_argument("--config", default=None)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--new-file", default=None, help="path to the New Data file to process")
    ap.add_argument("--dry-run", action="store_true", help="skip the database load")
    args = ap.parse_args(argv)

    log = Log()
    cfg, cfg_path = load_config(args.config)
    log(f"Config: {cfg_path}")
    data_dir = os.path.realpath(args.data_dir or cfg.get("data_dir") or os.path.dirname(BASE_DIR))
    logs_dir = cfg.get("logs_dir") or os.path.join(data_dir, "logs")
    started = _dt.datetime.now()
    log(f"Data folder: {data_dir}")
    status = {"started": started.isoformat(), "status": "running", "data_dir": data_dir}

    try:
        # 1. profile ---------------------------------------------------------
        prof_cfg = cfg.get("profile", "mapping_profile.json")
        profile_path = prof_cfg if os.path.isabs(prof_cfg) else os.path.join(BASE_DIR, prof_cfg)
        if not os.path.isfile(profile_path):
            # The web app's "Save profile" lets you name the file anything, so an
            # exact-name mismatch is the most common reason this agent fails.
            # Fall back to the folder's own profile when there is no ambiguity.
            found = sorted(glob.glob(os.path.join(BASE_DIR, "mapping_profile*.json")))
            if len(found) == 1:
                profile_path = found[0]
                log(f"Config named '{os.path.basename(prof_cfg)}', which is missing; "
                    f"using the only profile present: {os.path.basename(profile_path)}")
            elif len(found) > 1:
                names = ", ".join(os.path.basename(f) for f in found)
                raise FileNotFoundError(
                    f"No mapping profile at {profile_path}, and several candidates "
                    f"exist ({names}). Set 'profile' in the config to the one to use.")
            else:
                raise FileNotFoundError(
                    f"No mapping profile at {profile_path}, and no mapping_profile*.json "
                    f"in {BASE_DIR}. Save one from the web app ('Save profile') and "
                    "point the config's 'profile' at it.")
        with open(profile_path, encoding="utf-8") as fh:
            profile = json.load(fh)
        log(f"Profile: {os.path.basename(profile_path)}")

        # 2. inputs ----------------------------------------------------------
        files = resolve_files(data_dir, cfg, log, args.new_file)
        frames, sheets_used = {}, {}
        for r in ROLES:
            frames[r], sheets_used[r] = read_role(r, files[r], cfg, log)

        # 3. transform (validations handled by the gate, not the engine) -----
        run_cfg = dict(profile)
        run_cfg["value_maps"] = [m for m in (profile.get("value_maps") or []) if m.get("enabled")]
        run_cfg["validations"] = []
        log("Running transformation...")
        res = transform.build_output(frames["new"], frames["master"], frames["historical"], run_cfg)
        out, ctx = res["output"], res["ctx"]
        report = res.get("report", {})
        for step in report.get("steps", []):
            log("  " + step)
        for w in report.get("warnings", []):
            log("  WARNING: " + w)

        excluded = res.get("excluded")
        excluded_n = int(len(excluded)) if excluded is not None else int(report.get("excluded_new_rows", 0) or 0)
        additions = res.get("master_additions")
        additions_n = int(len(additions)) if additions is not None else 0

        # 4. QC gate ---------------------------------------------------------
        rules = assemble_rules(profile, cfg)
        log(f"QC: {len(rules)} rule(s), {sum(1 for r in rules if r.get('blocking'))} blocking.")
        gate = qc.split(out, ctx.resolve, rules)
        clean, quar, issues, qsum = gate["clean"], gate["quarantine"], gate["issues"], gate["summary"]
        log(f"QC result: {qsum['clean_rows']:,} clean, {qsum['quarantined_rows']:,} quarantined, "
            f"{excluded_n:,} excluded (unmatched), {qsum['issue_count']:,} issue(s).")

        # 5. write the output workbook --------------------------------------
        out_dir = cfg.get("output_dir") or data_dir
        os.makedirs(out_dir, exist_ok=True)
        stamp = started.strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(out_dir, f"Output_Data_{stamp}.xlsx")
        sheets = {"Output Data": out}
        if len(quar):
            sheets["Quarantined Rows"] = quar
        if len(issues):
            sheets["Validation Issues"] = issues
        if excluded is not None and len(excluded):
            sheets["Excluded New Rows"] = excluded
        if additions is not None and len(additions):
            sheets["Master Additions"] = additions
        sheets["Run Summary"] = _summary_frame(report, qsum, files, started, excluded_n, additions_n)
        excel_io.write_excel(sheets, out_path)
        log(f"Output workbook: {out_path}")

        # 5b. refuse to load if a blocking rule could not be evaluated -------
        unusable = qsum.get("unusable_blocking") or []
        on_unevaluable = str((cfg.get("qc") or {}).get("on_unevaluable", "fail")).lower()
        if unusable:
            desc = "; ".join(f"{u['column']} {u['type']} ({u['reason']})" for u in unusable)
            if on_unevaluable == "fail":
                raise RuntimeError(
                    "Blocking QC rule(s) could not be evaluated, so the load was refused "
                    f"to avoid shipping unchecked data: {desc}. Fix the rule's "
                    "column/reference, or set qc.on_unevaluable='warn' to override.")
            log("WARNING: unevaluable blocking rule(s) ignored per config: " + desc)

        # 6. load the clean rows --------------------------------------------
        db = cfg.get("database") or {}
        db_path = db.get("path") or files["historical"]   # default: grow the historical/DB file
        db_sheet = db.get("sheet") or sheets_used.get("historical") or "Output Data"
        load_summary = None
        if args.dry_run:
            log("Dry run - skipping the database load.")
        elif len(clean) == 0:
            log("Nothing clean to load - every row was quarantined.")
        else:
            load_summary = load.append_idempotent(
                db_path, db_sheet, clean, db.get("key_columns"),
                db.get("backup_dir") or os.path.join(data_dir, "backups"))
            if not load_summary["keys"]:
                log("WARNING: no key columns matched the output; the load was append-only "
                    "(NOT idempotent). Set database.key_columns to the output's real columns.")
            log(f"Loaded into {os.path.basename(db_path)} [{load_summary['sheet']}]: "
                f"{load_summary['rows_after']:,} total rows "
                f"(replaced {load_summary['rows_replaced']:,}).")

        # 7. status + alerts -------------------------------------------------
        needs_attention = qsum["quarantined_rows"] or excluded_n
        status.update({
            "status": "completed_with_attention" if needs_attention else "completed",
            "finished": _dt.datetime.now().isoformat(),
            "new_file": os.path.basename(files["new"]),
            "rows_total": qsum["total_rows"], "rows_clean": qsum["clean_rows"],
            "rows_quarantined": qsum["quarantined_rows"], "rows_excluded": excluded_n,
            "master_additions": additions_n, "issues": qsum["issue_count"],
            "output": out_path, "loaded": None if args.dry_run else (load_summary or "nothing"),
        })
        log_path = save_log(logs_dir, log)
        status["log"] = log_path
        save_status(logs_dir, status)

        if needs_attention:
            body = (
                f"New Data file: {os.path.basename(files['new'])}\n"
                f"Loaded (clean): {qsum['clean_rows']:,}\n"
                f"Quarantined (failed a QC rule): {qsum['quarantined_rows']:,}\n"
                f"Excluded (label not recognised in Master Data): {excluded_n:,}\n"
                + (f"New combinations added to Master (your 'add' decisions): {additions_n:,}\n" if additions_n else "")
                + "\nQuarantined rows failed a rule you defined - see 'Quarantined Rows' / "
                "'Validation Issues'.\nExcluded rows carry a label the profile hasn't seen - "
                "open the web app, standardise them in step 2, re-save the profile, then re-drop "
                "the file.\n\n"
                f"Details:\n  {out_path}\nLog:\n  {log_path}\n")
            write_alert(logs_dir,
                        f"[Data Agent] {qsum['quarantined_rows']} quarantined, {excluded_n} excluded "
                        f"({started:%Y-%m-%d %H:%M})", body, cfg, log)
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        log("RUN FAILED: " + str(e))
        status.update({"status": "failed", "error": str(e), "finished": _dt.datetime.now().isoformat()})
        try:
            log_path = save_log(logs_dir, log)
            status["log"] = log_path
            save_status(logs_dir, status)
            write_alert(logs_dir, f"[Data Agent] RUN FAILED ({started:%Y-%m-%d %H:%M})",
                        str(e) + "\n\n" + tb + "\n\nLog:\n" + log_path, cfg, log)
        except Exception:
            print(tb)
        return 1


def _summary_frame(report, qsum, files, started, excluded_n, additions_n) -> pd.DataFrame:
    rows = [("Started", started.strftime("%Y-%m-%d %H:%M:%S"))]
    for role in ROLES:
        rows.append((f"{role.title()} file", os.path.basename(files[role])))
    rows += [
        ("Total output rows", report.get("row_count")),
        ("Rows from New Data", report.get("actual_rows")),
        ("Gap-filled rows", report.get("filled_rows")),
        ("Clean rows loaded", qsum["clean_rows"]),
        ("Rows quarantined", qsum["quarantined_rows"]),
        ("Rows excluded (unmatched labels)", excluded_n),
        ("Master additions", additions_n),
        ("Blocking rules", "; ".join(qsum["blocking_rules"]) or "(none)"),
    ]
    rows += [("Step", s) for s in report.get("steps", [])]
    rows += [("Warning", w) for w in report.get("warnings", [])]
    return pd.DataFrame(rows, columns=["Item", "Detail"])


if __name__ == "__main__":
    sys.exit(main())
