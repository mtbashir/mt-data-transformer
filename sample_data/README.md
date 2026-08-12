# Sample data (fictional)

Everything in this folder is **invented** — a made-up company, *Acme Beverages*,
with made-up products, prices and cities. No real business data.

| File | What it is |
| --- | --- |
| `New Data.xlsx` | This period's transactions (38 rows), with deliberate gaps. |
| `Master Data.xlsx` | The full valid universe: 4 cities × 6 products = 24 combos. |
| `Historical Data.xlsx` | A prior period in the output shape — template + donor pool. |

It's built to show the app's core job. Load all three and generate: the output
has **48 rows** (24 combos × 2 dates), of which **10 are gap-filled** — including
`ACME SODA 1000ML`, which is missing from New Data entirely and gets pulled from
Historical for every city and date. New Data also spells one brand `ORANGE FIZZ`
where Master says `ORANGE`, so the **Standardise** step has something to resolve.

Regenerate anytime with:

```bash
python generate_sample_data.py
```
