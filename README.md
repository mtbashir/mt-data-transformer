# Data Transformer

A local web app that turns a transactional **New Data** file into a complete,
gap-filled output file, using **Master Data** for the valid universe and
standard labels, and **Historical Data** as both the output template and the
donor pool for missing rows.

Run it **locally** (your files never leave your machine) or **host it online**
so anyone can try it in a browser.

## Try it online

Deploy your own copy to a free host. Whichever you pick, the hosted app is
pre-loaded with the fictional [`sample_data/`](sample_data/), so visitors can
click **Load all three** and it works with zero setup.

**No credit card required:**

- **Hugging Face Spaces** — create a **Docker** Space and push this repo to it.
  It builds the [`Dockerfile`](Dockerfile) automatically. Set the Space's
  *app port* to `7860`.
- **PythonAnywhere** — free "Beginner" account. In a Bash console:
  `git clone https://github.com/mtbashir/mt-data-transformer.git`, make a
  virtualenv, `pip install -r requirements.txt`, then add a **Manual /
  Flask** web app whose WSGI file does `import app; application = app.app` and
  sets `os.environ["DT_DATA_DIR"] = ".../sample_data"`.

**Card required (but free tier):**

- **Render** — **New + → Blueprint → connect this repo → Apply**. It reads
  [`render.yaml`](render.yaml). Render asks for a card to verify the account
  even though the free tier isn't charged.

Any Docker host (Koyeb, Fly.io, Railway, …) can run the [`Dockerfile`](Dockerfile)
directly — it binds to `$PORT` if the host sets one, else `7860`.

The hosted instance is a **shared demo**: it keeps each visitor's uploads in
memory only and writes nothing permanent. Don't put confidential data into a
public instance — for real data, run it locally (below).

## Running it locally

Requires **Python 3.11+**. First time, from this `webapp` folder:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

Then start it (or, on Windows, double-click **`Start Data Transformer.bat`**):

```bash
python app.py
```

Then open <http://127.0.0.1:5000>. Accepts `.xlsx`, `.xlsb`, `.xlsm`, `.xls`
and `.csv`.

### Where it looks for files

By default the "Files in this folder" list reads the folder **above** `webapp`.
Point it elsewhere with an environment variable:

```bash
# Windows (PowerShell):  $env:DT_DATA_DIR = "C:\path\to\your\data"
# macOS / Linux:         export DT_DATA_DIR=/path/to/your/data
```

Nothing is uploaded anywhere - it runs on your machine and writes .xlsx files
to a temp working folder. **No data files are included in this repository.**

### Optional environment variables

| Variable | Purpose |
| --- | --- |
| `DT_DATA_DIR` | Folder the quick-load list reads (default: parent of `webapp`). |
| `DT_SMTP_PASSWORD` | SMTP password for the headless pipeline's email alerts. |

The Step 3 **Prompt (AI)** feature asks for an Anthropic API key **in the app**;
it is held in memory only and never written to disk or into a saved profile.

## The five steps

**1. Upload** the three files. Two ways:

- **Files in this folder** (top of the page) lists the spreadsheets sitting in
  the data folder (see *Where it looks for files* above; the bundled
  `sample_data/` is used on the hosted demo), already labelled New / Master /
  Historical. **Load all three** takes them in one click, with no file dialog.
  This is the easy path, and the reliable one on a mapped network drive.
- Or drag a file onto a card, or click to browse.

The sheet and header row are detected automatically (a workbook whose header is
on row 2 is picked up). Correct either one if a preview looks wrong.

The file picker deliberately has **no file-type filter**. With one, Windows
greys out every file on a mapped network drive, so the dialog opens but nothing
can be selected. The file type is checked after you choose instead.

**2. Standardise against Master Data.** Master Data is the authority on names;
this step converts New Data's spellings onto it, so only Master Data attributes
carry forward.

- **Column roles** - every column in each file is classified as a
  **dimension** (a label, standardised here), a **measure** (a number, never
  rewritten), a **date**, or **ignored**. Classification is by value, not by
  the declared type, because reading `.xlsb` hands back numbers as text.
  Change any that are wrong - a numeric code such as `PKG` is a dimension if
  you say so, and marking a column a measure removes it from standardisation
  immediately. *Reset to detected* undoes your changes for that file.
- **Which columns hold the same attribute** - pair each Master Data attribute
  with the New Data column holding it (BRAND with BRAND, and so on). Only
  dimensions are offered, and only paired attributes are checked. Standardising
  a price of 1005 onto 1000 because the digits look alike would corrupt it, so
  measures are blocked in the browser, in the API, and again in the engine when
  a run starts - a saved profile cannot reintroduce it.
- **Value check** - every distinct New Data value is compared with Master Data
  and anything unrecognised is listed with the number of rows affected, plus a
  proposed decision:
  - **Convert to a Master Data value** - `ORANGE FIZZ` becomes `ORANGE`.
    Suggestions come from cross-walks in the Master Data workbook first (that
    is how `DIET COLA` becomes `COLA`), then from name similarity.
  - **Add to Master Data** - for something genuinely new, such as `GINGER ALE`.
    Only the city/product combinations actually seen in New Data are added, and
    they are listed on a *Master Data Additions* sheet so you can paste them
    into the real Master Data file.
  - **Leave out of the output** - the rows stay excluded.

Nothing is applied until you continue, and every suggestion can be overridden.

**3. Output columns.** What one row is, and what each column contains.

- **Output grid** - the dimensions that define one output row (e.g. city +
  product). Every Master Data combination gets a row for every reporting date;
  this is what creates the gaps.
- **Repeated transactions** - New Data has several transactions per
  city/product/date. Choose whether to keep the first, sum them, average them,
  and so on.
- **Output columns** - the column list comes from Historical Data, and each row
  is filled in with four choices:

  | Column | Meaning |
  |---|---|
  | **Source** | Which file the value comes from: New Data, Master Data, or Historical Data |
  | **Operation** | How it is filled: take the column, grid/date, formula, prompt, fixed value, or blank |
  | **What** | The column, the formula, or the fixed value |
  | **Preview** | What the column produces for the first rows of real data |

  **+ Add column** creates a new output column - same four choices, and it can
  be removed again. **Refresh preview** recalculates the Preview column after
  you change anything.

  **Column order** is the order of the rows in this table, and it is the column
  order in the generated file. Drag a row by the handle on its left, or use the
  up/down arrows in the Order column. **Reset order** puts everything back into
  the Historical Data layout, with any columns you added at the end. Because a
  formula may only use `[out.X]` for a column built earlier, moving a column
  above one it depends on is flagged straight away rather than failing later at
  Generate.

  With **Operation = Formula**, start typing a column name and a list appears of
  every matching column across all three files, labelled with which file it came
  from; pick one and it is inserted as `[new.NTP/6]`. So `UNIT TP` is
  `ROUND([new.NTP/6] / [new.PKG], 2)`.

  With **Operation = Prompt**, describe the column in plain English
  ("NTP/6 divided by PKG") and Claude writes the formula, which then appears in
  the same editable box so you can check and adjust it. The first time you use
  it the app asks for a Claude API key from console.anthropic.com. The key is
  held in the running app's memory only - never written to disk, never saved
  into a mapping profile, and never logged. Everything else in the app works
  without a key.

A formula set on an output column applies **only to rows that came from New
Data**; gap-filled rows use their step 4 rule. For a calculation that should
apply to every row, add it as a computed column referencing `[out.*]`.

**4. Gap-fill rules.**

- *Where to copy from* - **both** New Data and Historical Data are searched for
  a donor. A product missing in one city on a reporting date is usually present
  in another city on that same date, so New Data is often the better source:
  its figures are current, while Historical Data can be months old. This setting
  picks which pool wins when both have a match. Per column you can force one or
  the other ("Copy from New Data" / "Copy from Historical"), or reference either
  in a formula as `[new_donor.NTP]` and `[donor.NTP]`.
- *Which row to copy* - most recent date, nearest date, last date before the
  missing one, earliest, or random.
- *Matching order* - a missing row is matched on all dimensions first; if a pool
  has nothing, looser matches are tried in turn. The output records which level
  was used in the `FILL BASIS` column.
- *How each measure is filled* - by default the value comes from the preferred
  pool, falling back to the other one and then to Master Data. Presets cover the
  common variations, e.g. **±1 unit** produces
  `COALESCE([donor.NTP], [new_donor.NTP]) * RANDBETWEEN(99,101) / 100`.

  **The date and the dimensions are never taken from a donor.** A filled row
  exists because Master Data says this city/product should be reported on this
  date, so it keeps *its own* date from New Data's reporting range and its own
  city/product - only the measures are borrowed. Those columns are shown locked
  in the table, and a donor-based rule aimed at one of them is ignored with a
  warning rather than silently stamping the donor's date on the row.
- *IDs on filled rows* - leave blank, generate new unique IDs, reuse the
  donor's ID, or set a fixed value.
- *Validation* - e.g. quantities cannot be negative, prices must be within 10%
  of the Master Data reference. Breaches are reported, never blocked.

**5. Generate.** Preview first, then download. Use **Save profile** in the
header to keep your configuration and **Load profile** to reuse it next month.

## The output workbook

| Sheet | Contents |
|---|---|
| `Output Data` | Every Master Data combination × every date. `SOURCE` marks each row `ACTUAL` or `FILLED`; `FILL BASIS` records how a filled row was matched. |
| `Validation Issues` | Rule breaches, capped at 500 rows per rule with the true total noted. |
| `Excluded New Rows` | New Data rows with no matching Master Data combination. These are missing from Master Data and are worth fixing there. |
| `Master Data Additions` | Combinations added because you chose "Add to Master Data" in step 2. Paste them into the real Master Data file to make them permanent. |
| `Run Summary` | Files used, row counts, and every step and warning from the run. |

## Formula language

Reference columns in square brackets, with a namespace prefix:

| Prefix | Meaning |
|---|---|
| `[new.NTP]` | the New Data row (blank on filled rows) |
| `[master.UTP]` | the matched Master Data row |
| `[donor.NTP]` | the Historical Data donor row (filled rows only) |
| `[grid.DATE]` | the row's own date and dimensions |
| `[out.BRAND]` | an output column already built above |

Operators `+ - * / % **`, comparisons, and `AND`/`OR`. `+` joins text when
either side is text. Excel habits are accepted: a leading `=` is ignored, `&`
joins text, and `<>` means "not equal" - so `=[NTP]*2` and `[NTP]*2` are the
same formula.

Functions: `CONCAT` `IF` `IFERROR` `COALESCE` `RANDBETWEEN` `RAND` `ROUND`
`ROUNDUP` `ROUNDDOWN` `INT` `ABS` `SUM` `AVERAGE` `MIN` `MAX` `UPPER` `LOWER`
`PROPER` `TRIM` `LEN` `LEFT` `RIGHT` `PAD` `TEXT` `VALUE` `YEAR` `MONTH` `DAY`
`ISBLANK` `NOT`.

Examples:

```
CONCAT([out.CITY], [out.CATEGORY], [out.COMPANY], [out.SKU STANDARD NAME])
ROUND([donor.NTP] * RANDBETWEEN(99,101) / 100, 2)
[out.UNIT SP] - [out.UNIT TP]
IF([out.QUANTITY] > 0, [out.NET AMOUNT] / [out.QUANTITY], 0)
```

Formulas are parsed and evaluated in a sandbox - only the functions above can
run.

## Layout

```
webapp/
  app.py                 Flask server and API
  engine/
    excel_io.py          reading/writing xlsx, xlsb, csv; header detection
    mapping.py           column and value-mapping suggestions
    formula.py           the formula language
    transform.py         grid, matching, donors, gap filling, validation
  static/                the browser UI
```
