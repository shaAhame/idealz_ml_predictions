"""Generate a realistic synthetic sales export in the SAME format as the Google
Sheet (Date, Location, Category, Bill No., IMEI, Brand/Item, Model, Capacity,
Colour, Sales Rep, Revenue, Cost, Profit) for local development and CI.

    python scripts/make_sample_data.py --days 730 --out data/sample_sales.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays
import numpy as np
import pandas as pd

LOCATIONS = {"ISEVEN": 1.0, "ISEVEN CITY": 0.6}
REPS = {"ISEVEN": ["AADHIL", "ABDULLAH", "NIMAL"], "ISEVEN CITY": ["SAMAN", "FATHIMA"]}
CATEGORIES = {
    # name: (mean line items/day, (min, max) unit price LKR, margin range)
    "ACCESSORIES": (14.0, (500, 12000), (0.15, 0.85)),
    "PHONES": (3.0, (35000, 450000), (0.04, 0.10)),
    "SMART WATCHES": (1.0, (8000, 120000), (0.10, 0.25)),
    "REPAIRS": (4.0, (1500, 25000), (0.40, 0.75)),
}
ITEMS = {
    "ACCESSORIES": ["TEMPERED", "BACKCASE", "GREEN 4 IN 1", "CABLE 2M - APPLE", "ASPOR A315 10000mah POWER BANK",
                    "CHARGER 20W", "EARBUDS", "CAR HOLDER"],
    "PHONES": ["APPLE", "SAMSUNG", "XIAOMI", "OPPO", "VIVO"],
    "SMART WATCHES": ["APPLE WATCH", "GALAXY WATCH", "XIAOMI BAND"],
    "REPAIRS": ["DISPLAY REPLACEMENT", "BATTERY REPLACEMENT", "CHARGING PORT", "SOFTWARE"],
}
DOW_EFFECT = np.array([0.9, 0.85, 0.9, 0.95, 1.1, 1.3, 1.2])  # Mon..Sun


def generate(days: int, end: dt.date, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = end - dt.timedelta(days=days - 1)
    hol = holidays.country_holidays("LK", years=range(start.year, end.year + 1))
    rows: list[list[object]] = []
    bill = {loc: 1000 + i * 50000 for i, loc in enumerate(LOCATIONS)}
    for i in range(days):
        day = start + dt.timedelta(days=i)
        trend = 1.0 + 0.25 * i / max(days, 1)
        season = 1.0 + 0.25 * np.exp(-((day.timetuple().tm_yday - 350) % 365) / 20)  # December peak
        if day.month == 4 and 8 <= day.day <= 14:
            season *= 1.5  # Sinhala & Tamil New Year shopping
        h = 1.25 if day in hol else 1.0
        for loc, loc_scale in LOCATIONS.items():
            for cat, (mean, (lo, hi), (mlo, mhi)) in CATEGORIES.items():
                lam = mean * loc_scale * trend * season * h * DOW_EFFECT[day.weekday()]
                for _ in range(rng.poisson(lam)):
                    if rng.random() < 0.6:
                        bill[loc] += 1
                    price = float(np.round(np.exp(rng.uniform(np.log(lo), np.log(hi))), -2))
                    cost = float(np.round(price * (1 - rng.uniform(mlo, mhi)), -1))
                    item = rng.choice(ITEMS[cat])
                    imei = str(rng.integers(10**14, 10**15)) if cat == "PHONES" else ""
                    model = f"MODEL {rng.integers(10, 20)}" if cat == "PHONES" else ""
                    capacity = rng.choice(["128GB", "256GB"]) if cat == "PHONES" else ""
                    colour = rng.choice(["BLACK", "BLUE", "WHITE"]) if cat == "PHONES" else ""
                    rows.append([day.isoformat(), loc, cat, bill[loc], imei, item, model, capacity, colour,
                                 rng.choice(REPS[loc]), price, cost, price - cost])
    cols = ["Date", "Location", "Category", "Bill No.", "IMEI", "Brand/Item", "Model", "Capacity", "Colour",
            "Sales Rep", "Revenue", "Cost", "Profit"]
    return pd.DataFrame(rows, columns=cols)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--end", type=dt.date.fromisoformat,
                   default=dt.datetime.now(ZoneInfo("Asia/Colombo")).date() - dt.timedelta(days=1))
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", type=Path, default=Path("data/sample_sales.csv"))
    a = p.parse_args()
    df = generate(a.days, a.end, a.seed)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out, index=False)
    print(f"wrote {len(df):,} rows ({df['Date'].min()} .. {df['Date'].max()}) to {a.out}")


if __name__ == "__main__":
    main()
