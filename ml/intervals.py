"""80% forecast ranges from walk-forward backtest errors (empirical / split-conformal).

Errors are measured as log ratios, log1p(actual) - log1p(forecast), so one set of
quantiles works for big and small groups and for zeros. They are computed for
every filter combination the dashboard offers (all, one location, one category,
one location x category) and for the aggregations people plan with:

    d1_7, d8_14   a single day, 1-7 / 8-14 days ahead
    d7, d14       the total of the next 7 / 14 days

A range is then  [expm1(log1p(f) + lo), expm1(log1p(f) + hi)]  clipped at 0.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ml.data_loader import SERIES_KEYS

MIN_POINTS = 5
WILDCARD = "*"


def combo_key(location: str | None, category: str | None) -> str:
    return f"{location or WILDCARD}|{category or WILDCARD}"


def _quantiles(err: pd.Series, lo: float, hi: float) -> dict[str, float] | None:
    err = err.dropna()
    if len(err) < MIN_POINTS:
        return None
    return {"lo": round(float(np.quantile(err, lo)), 4), "hi": round(float(np.quantile(err, hi)), 4), "n": len(err)}


def _log_err(df: pd.DataFrame) -> pd.Series:
    return np.log1p(df["y_true"].clip(lower=0)) - np.log1p(df["y_pred"].clip(lower=0))


def compute_intervals(bt: pd.DataFrame, lo: float = 0.10, hi: float = 0.90) -> dict[str, Any]:
    """`bt` = walk-forward backtest rows [origin, sale_date, *SERIES_KEYS, step, y_true, y_pred]."""
    loc_col, cat_col = SERIES_KEYS
    locs = sorted(bt[loc_col].unique())
    cats = sorted(bt[cat_col].unique())
    combos = [(None, None), *[(lc, None) for lc in locs], *[(None, c) for c in cats],
              *[(lc, c) for lc in locs for c in cats]]
    out: dict[str, Any] = {"levels": [lo, hi], "combos": {}}
    for lc, c in combos:
        sub = bt
        if lc is not None:
            sub = sub.loc[sub[loc_col] == lc]
        if c is not None:
            sub = sub.loc[sub[cat_col] == c]
        if sub.empty:
            continue
        n_series = sub[SERIES_KEYS].drop_duplicates().shape[0]
        daily = sub.groupby(["origin", "sale_date", "step"], as_index=False)[["y_true", "y_pred"]].sum()
        entry: dict[str, Any] = {
            "d1_7": _quantiles(_log_err(daily.loc[daily["step"] <= 7]), lo, hi),
            "d8_14": _quantiles(_log_err(daily.loc[daily["step"] >= 8]), lo, hi),
        }
        for name, days in (("d7", 7), ("d14", 14)):
            win = sub.loc[sub["step"] <= days]
            tot = win.groupby("origin").agg(y_true=("y_true", "sum"), y_pred=("y_pred", "sum"), rows=("step", "size"))
            tot = tot.loc[tot["rows"] == days * n_series]  # complete windows only
            entry[name] = _quantiles(_log_err(tot), lo, hi)
        out["combos"][combo_key(lc, c)] = entry
    return out


def apply_range(point: float, q: dict[str, float] | None) -> tuple[float | None, float | None]:
    if not q or point is None or not math.isfinite(point):
        return None, None
    base = math.log1p(max(point, 0.0))
    return max(0.0, math.expm1(base + q["lo"])), max(0.0, math.expm1(base + q["hi"]))


def coverage(bt: pd.DataFrame, intervals: dict[str, Any], key: str = "d1_7") -> float | None:
    """Share of per-series daily actuals inside their range (in-sample sanity check)."""
    hits = []
    for r in bt.loc[bt["step"] <= 7].itertuples(index=False):
        q = intervals["combos"].get(combo_key(getattr(r, SERIES_KEYS[0]), getattr(r, SERIES_KEYS[1])), {}).get(key)
        low, high = apply_range(r.y_pred, q)
        if low is not None:
            hits.append(low <= r.y_true <= high)
    return float(np.mean(hits)) if hits else None
