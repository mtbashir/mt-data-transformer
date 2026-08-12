# Data Pipeline Agent — setup & operation

This turns the interactive **Data Transformer** web app into an unattended
**agent** that runs every day: it reads the three input files, applies your
saved mapping, gap-fills, runs your QC rules as a **gate** (bad rows are
quarantined, not loaded), and appends the clean rows to an Excel database file —
writing logs and an alert whenever something needs you.

It reuses the app's proven engine. The only new pieces are `run_pipeline.py`
(the runner), `engine/qc.py` (the QC gate) and `engine/load.py` (the load).

---

## One-time setup

### 1. Save a mapping profile (defines the transform + your QC rules)

The agent needs a reviewed configuration to run unattended.

1. Start the app: double-click **`Start Data Transformer.bat`**.
2. Click **Load all three** (New / Master / Historical).
3. Review the column mapping, the output grid, and — importantly — **Step 4
   (Gap-fill rules) / validations**. Add the QC rules you want enforced (see
   *Defining QC rules* below).
4. Click **Save profile** in the header. Save the file as **`mapping_profile.json`**
   inside this `webapp` folder (next to `run_pipeline.py`).

If the profile is missing, the agent stops with a clear message — it will never
guess your mapping.

### 2. Check `pipeline_config.json`

Open `pipeline_config.json` and confirm:

- **files** — the three input filenames (defaults match your folder).
- **database.path** — defaults to a **new** `sales_db_pipeline.xlsx` so nothing
  existing is touched. Once you've confirmed the sheet name and key columns,
  you can point this at your real sales database file.
- **database.key_columns** — the columns that identify one row (default
  `DATE, CITY, SKU STANDARD NAME`). These make re-runs *idempotent*: re-running a
  day replaces that day rather than duplicating it. They must match the real
  column names in your output.
- **qc.blocking** — `"all"` means every rule quarantines. List specific rules in
  **qc.warn_only** to downgrade them to report-only.

### 3. Test before scheduling

From a terminal in this folder:

```bash
python run_pipeline.py --dry-run     # runs everything EXCEPT the database load
```

Check `logs\last_run.json` and the new `Output_Data_*.xlsx` (look at the
**Quarantined Rows** and **Validation Issues** sheets). When it looks right:

```bash
python run_pipeline.py               # a real run - loads clean rows to the DB file
```

---

## Schedule it (Windows Task Scheduler)

If the input files live on a shared network drive, run the agent on a PC that
has that drive mapped.

1. Open **Task Scheduler** → **Create Task** (not "Basic").
2. **General**: name it "Data Pipeline". Select **Run only when user is logged
   on** (simplest, since it needs the `I:` drive). Tick **Run with highest
   privileges**.
3. **Triggers** → **New** → **Daily**, set your time (e.g. 6:00 AM).
4. **Actions** → **New** → **Start a program** → Program/script: browse to
   **`Run Pipeline.bat`** in this folder.
5. **Settings**: tick "Run task as soon as possible after a scheduled start is
   missed".
6. Save. Right-click the task → **Run** once to confirm it works, then check
   `logs\last_run.json`.

---

## How the QC gate works

- Each rule is **blocking** (default) or **warn-only** (`qc.warn_only`).
- A row that breaks any **blocking** rule is moved to **Quarantined Rows** with a
  `QC_REASON`, and is **not loaded**. All other rows load normally.
- **Warn-only** breaches are listed in **Validation Issues** but do not hold a
  row back.
- **Safe-fail:** if a blocking rule *cannot be evaluated* (e.g. its master
  reference or column is missing), the whole run **stops and loads nothing**
  (`qc.on_unevaluable: "fail"`) — so unchecked data never reaches the database.
- Any quarantine or failure writes an **`ALERT_*.txt`** in `logs\` (and emails
  you if you enable it in the config).

### Defining QC rules

Add these in the app's validations step (or in `qc.extra_validations`). Types:

| Rule type | Checks | Needs |
|---|---|---|
| `not_blank` | field is not empty | column |
| `non_negative` | value ≥ 0 | column |
| `positive` | value > 0 | column |
| `min` / `max` | value within a bound | `value` |
| `between` | value within a range | `min`, `max` |
| `range_pct` | within ±X% of a Master reference | `reference`, `pct` |
| `in_master` | value exists in Master Data | `reference` |
| `unique` | no duplicate values | column |

Example (NTP must be within 10% of the Master reference, and every SKU must
exist in Master Data):

```json
{ "column": "NTP", "type": "range_pct", "reference": "master.NTP REGULAR", "pct": 10 }
{ "column": "SKU STANDARD NAME", "type": "in_master", "reference": "master.SKU STANDARD NAME" }
```

---

## Where things go

| Location | Contents |
|---|---|
| `Output_Data_<timestamp>.xlsx` | Full output + Quarantined Rows + Validation Issues + Run Summary |
| `sales_db_pipeline.xlsx` | The accumulating Excel database (clean rows only) |
| `logs\last_run.json` | Machine-readable status of the most recent run |
| `logs\run_<timestamp>.log` | Full log of each run |
| `logs\ALERT_*.txt` | Written whenever rows are quarantined or a run fails |
| `backups\` | A timestamped copy of the DB file before every load |

---

## Notes & current limits

- **Load target is the Excel database file only** (as chosen). Loading on to
  MySQL (like `create_uat_tables.py`) can be added later as a post-QC step.
- **Pivots are recomputed in code** (the app's grid + dedupe + donor fill), so no
  Excel needs to be open.
- The agent never edits your input files; it only reads them and writes new
  output/DB/log files.
