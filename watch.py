"""Folder watcher - runs the pipeline as soon as a new New Data file appears.

Two ways to run it:

  * Task Scheduler (recommended): schedule "watch.py --once" every few minutes.
    Each run checks for a new/changed New Data file and processes it if found.
    Nothing stays resident, so it survives reboots.

  * Continuous: "python watch.py" loops, polling every --interval seconds.

A file counts as 'new' when its (size, mtime) differs from the last one
processed, recorded in <data_dir>/.agent_state.json. Before running, the file's
size must hold steady for a few seconds, so a half-copied / still-syncing file
is left alone until it settles.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] watch: {msg}", flush=True)


def load_cfg(path):
    path = path or os.path.join(BASE_DIR, "pipeline_config.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), path


def data_dir_of(cfg):
    return os.path.realpath(cfg.get("data_dir") or os.path.dirname(BASE_DIR))


def _excluded_names(cfg):
    """Files that must never be treated as incoming New Data."""
    files = cfg.get("files") or {}
    names = set()
    for r in ("master", "historical"):
        if files.get(r):
            names.add(os.path.basename(files[r]).lower())
    db = (cfg.get("database") or {}).get("path")
    if db:
        names.add(os.path.basename(db).lower())
    return names


def find_candidate(cfg, data_dir):
    """The New Data file to consider: a fixed name if configured, else the most
    recently modified file matching the watch glob (excluding master/DB/output)."""
    files = cfg.get("files") or {}
    watch = cfg.get("watch") or {}
    excl = _excluded_names(cfg)

    fixed = files.get("new")
    if fixed:
        p = os.path.join(data_dir, fixed)
        return p if os.path.isfile(p) else None

    pat = watch.get("new_glob") or "*[Nn]ew*.xls*"
    cands = []
    for p in glob.glob(os.path.join(data_dir, pat)):
        b = os.path.basename(p).lower()
        if b in excl or b.startswith("~$") or b.startswith("output_data_"):
            continue
        if os.path.isfile(p):
            cands.append(p)
    return max(cands, key=os.path.getmtime) if cands else None


def signature(path):
    st = os.stat(path)
    return {"path": os.path.abspath(path), "name": os.path.basename(path),
            "size": st.st_size, "mtime": int(st.st_mtime)}


def _state_path(data_dir):
    return os.path.join(data_dir, ".agent_state.json")


def read_state(data_dir):
    p = _state_path(data_dir)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def write_state(data_dir, state):
    with open(_state_path(data_dir), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def is_stable(path, seconds):
    try:
        s1 = os.path.getsize(path)
    except OSError:
        return False
    time.sleep(max(0, seconds))
    try:
        s2 = os.path.getsize(path)
    except OSError:
        return False
    return s1 == s2


def same_sig(a, b):
    return bool(a and b and a.get("path") == b.get("path")
               and a.get("size") == b.get("size") and a.get("mtime") == b.get("mtime"))


def run_once(cfg, cfg_path, dry=False) -> bool:
    data_dir = data_dir_of(cfg)
    watch = cfg.get("watch") or {}
    cand = find_candidate(cfg, data_dir)
    if not cand:
        log("no New Data file found - waiting.")
        return False
    sig = signature(cand)
    state = read_state(data_dir)
    if same_sig(sig, state.get("last")):
        log(f"unchanged since last run: {sig['name']} - nothing to do.")
        return False
    if not is_stable(cand, int(watch.get("stability_seconds", 5))):
        log(f"{sig['name']} is still changing (syncing?) - will retry next cycle.")
        return False

    log(f"new data detected: {sig['name']} - running pipeline...")
    cmd = [sys.executable, os.path.join(BASE_DIR, "run_pipeline.py"),
           "--config", cfg_path, "--new-file", cand]
    if dry:
        cmd.append("--dry-run")
    rc = subprocess.call(cmd, cwd=BASE_DIR)
    log(f"pipeline exit code {rc}.")

    # Record as processed regardless of exit code: the runner already alerted on
    # any problem, and re-running the identical bytes would only repeat it. A real
    # fix means re-saving the file, which changes its signature and re-triggers.
    state["last"] = {**sig, "processed_at": _dt.datetime.now().isoformat(), "exit_code": rc}
    write_state(data_dir, state)
    if watch.get("archive_processed"):
        _archive(cand, data_dir, watch)
    return True


def _archive(path, data_dir, watch):
    dest_dir = os.path.join(data_dir, watch.get("archive_dir", "processed"))
    os.makedirs(dest_dir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(path)
    try:
        os.replace(path, os.path.join(dest_dir, f"{stamp}__{base}"))
        log(f"archived {base} -> {watch.get('archive_dir', 'processed')}/")
    except OSError as e:
        log(f"could not archive {base}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Watch a folder and run the pipeline on new data.")
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true", help="check once and exit (for Task Scheduler)")
    ap.add_argument("--interval", type=int, default=None, help="loop poll interval seconds")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg, cfg_path = load_cfg(args.config)
    interval = args.interval or int((cfg.get("watch") or {}).get("interval_seconds", 300))
    if args.once:
        run_once(cfg, cfg_path, args.dry_run)
        return 0
    log(f"watching '{data_dir_of(cfg)}' every {interval}s. Ctrl+C to stop.")
    try:
        while True:
            try:
                run_once(cfg, cfg_path, args.dry_run)
            except Exception as e:
                log(f"error: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
