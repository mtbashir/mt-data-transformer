"""Generate the bundled sample dataset for a fictional company, Acme Beverages.

Everything here is invented - no real company, product, price or location. Run
this to regenerate the three .xlsx files next to it:

    python generate_sample_data.py

The data is shaped to demonstrate the app's core job - filling gaps:

* Master Data is the full universe: every CITY x SKU that should exist.
* New Data is this period's transactions, but deliberately MISSING some of that
  universe (a whole product absent, and one product missing in one city), and it
  spells one brand differently ("ORANGE FIZZ" vs the master's "ORANGE") so the
  Standardise step has something to do.
* Historical Data is a prior period in the output shape - the template and the
  donor pool the gaps are filled from.
"""
from __future__ import annotations

import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

# --- the fictional universe -------------------------------------------------
COMPANY = "ACME"
CITIES = [  # (CITY code, REGION)
    ("SPR", "NORTH"),   # Springfield
    ("RVT", "NORTH"),   # Rivertown
    ("LKS", "SOUTH"),   # Lakeside
    ("HLC", "SOUTH"),   # Hillcrest
]
# (SKU STANDARD NAME, BRAND, CATEGORY, SIZE, PKG-per-case, case price, unit sell price)
PRODUCTS = [
    ("ACME COLA 500ML",   "COLA",   "CSD",   "500ML",  24, 480.0, 25.0),
    ("ACME COLA 1000ML",  "COLA",   "CSD",   "1000ML", 12, 540.0, 55.0),
    ("ACME LEMON 500ML",  "LEMON",  "CSD",   "500ML",  24, 470.0, 25.0),
    ("ACME ORANGE 500ML", "ORANGE", "CSD",   "500ML",  24, 470.0, 25.0),
    ("ACME SODA 1000ML",  "SODA",   "CSD",   "1000ML", 12, 500.0, 52.0),  # absent from New Data
    ("ACME WATER 1500ML", "WATER",  "WATER", "1500ML",  6, 210.0, 45.0),
]
NEW_DATES = [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02")]
HIST_DATE = pd.Timestamp("2023-12-31")


def master_data() -> pd.DataFrame:
    rows = []
    for city, region in CITIES:
        for name, brand, cat, size, pkg, case_tp, usp in PRODUCTS:
            rows.append({
                "MASTER LIST": f"{city}{name}",
                "REGION": region, "CITY": city, "CATEGORY": cat,
                "COMPANY": COMPANY, "BRAND": brand, "SKU STANDARD NAME": name,
                "SIZE": size, "PKG": pkg,
                "NTP REGULAR": case_tp,
                "UTP": round(case_tp / pkg, 2),
                "USP": usp,
            })
    return pd.DataFrame(rows)


def new_data() -> pd.DataFrame:
    """Transactions for this period, with intentional gaps."""
    rows, rid = [], 1001
    for date in NEW_DATES:
        for city, region in CITIES:
            for name, brand, cat, size, pkg, case_tp, usp in PRODUCTS:
                if name == "ACME SODA 1000ML":
                    continue                      # whole product missing -> gap
                if name == "ACME ORANGE 500ML" and city == "HLC":
                    continue                      # one product missing in one city -> gap
                shown_brand = "ORANGE FIZZ" if brand == "ORANGE" else brand  # needs standardising
                tp = case_tp + (5 if date == NEW_DATES[1] else 0)            # small change over time
                rows.append({
                    "ID": rid, "REGION": region, "CITY": city, "CATEGORY": cat,
                    "COMPANY": COMPANY, "BRAND": shown_brand, "SKU STANDARD NAME": name,
                    "SIZE": size, "PKG": pkg,
                    "TP": tp, "NTP": round(tp * 0.98, 2), "CONSUMER PRICE": usp,
                    "Date": date,
                })
                rid += 1
    return pd.DataFrame(rows)


def historical_data() -> pd.DataFrame:
    """A prior period in the output shape - template + donor pool for gaps."""
    rows, sn = [], 1
    for city, region in CITIES:
        for name, brand, cat, size, pkg, case_tp, usp in PRODUCTS:
            unit_tp = round(case_tp / pkg, 2)
            rows.append({
                "SN": sn, "UNIQUE ID": f"{city}{name}", "Date": HIST_DATE,
                "REGION": region, "CITY": city, "CATEGORY": cat,
                "COMPANY": COMPANY, "BRAND": brand, "SKU STANDARD NAME": name,
                "SIZE": size, "PKG": pkg,
                "NTP PER CASE": case_tp, "CONSUMER PRICE": usp,
                "UNIT TP": unit_tp, "UNIT SP": usp,
                "RET MARGIN": round(usp - unit_tp, 2),
                "RET MARGIN%": round((usp - unit_tp) / usp, 4) if usp else 0,
                "SOURCE": "ACTUAL",
            })
            sn += 1
    return pd.DataFrame(rows)


def main() -> None:
    for fname, df in [
        ("New Data.xlsx", new_data()),
        ("Master Data.xlsx", master_data()),
        ("Historical Data.xlsx", historical_data()),
    ]:
        path = os.path.join(HERE, fname)
        with pd.ExcelWriter(path, engine="openpyxl") as xl:
            df.to_excel(xl, index=False, sheet_name="Sheet1")
        print(f"wrote {fname}: {df.shape[0]} rows x {df.shape[1]} cols")


if __name__ == "__main__":
    main()
