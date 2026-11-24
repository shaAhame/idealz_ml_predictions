"""Multi-step forecasting shared by training (backtests), the batch job and the API.

Strategy: recursive (one global model, predict day t, feed the prediction back as
history for t+1). Only `cfg.lookback_days()` of history are carried, which keeps
API latency low and is exactly enough to reproduce the training features.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import numpy as np
import pandas as pd
from sklearn.pipeline import Pipeline

from ml.data_loader import SERIES_KEYS
from ml.features import FeatureConfig, add_features, lookback_start

ForecastFn = Callable[[pd.DataFrame, pd.Timestamp, int], pd.DataFrame]


def recursive_forecast(
    pipeline: Pipeline,
    cfg: FeatureConfig,
    history: pd.DataFrame,
    start: dt.date | pd.Timestamp,
    horizon: int,
) -> pd.DataFrame:
    """Forecast `horizon` days from `start` for every series in `history`.

    `history` is a dense panel [sale_date, *SERIES_KEYS, y] that must end on
    start - 1 day (build it with features.build_panel(end_date=start - 1)).
    Returns [sale_date, *SERIES_KEYS, step, y_pred].
    """
    start = pd.Timestamp(start)
    if history.empty:
        return pd.DataFrame(columns=["sale_date", *SERIES_KEYS, "step", "y_pred"])
    last = history["sale_date"].max()
    if last != start - pd.Timedelta(days=1):
        raise ValueError(f"history must end the day before start ({start.date()}), it ends {last.date()}")

    work = history.loc[history["sale_date"] >= lookback_start(start, cfg), ["sale_date", *SERIES_KEYS, "y"]]
    series = history[SERIES_KEYS].drop_duplicates().reset_index(drop=True)
    cols = cfg.feature_columns()
    outputs: list[pd.DataFrame] = []

    for step in range(horizon):
        day = start + pd.Timedelta(days=step)
        work = pd.concat([work, series.assign(sale_date=day, y=np.nan)], ignore_index=True)
        feats = add_features(work, cfg)
        rows = feats.index[feats["sale_date"] == day]
        preds = np.clip(pipeline.predict(feats.loc[rows, cols]), 0.0, None)
        work.loc[rows, "y"] = preds
        outputs.append(feats.loc[rows, ["sale_date", *SERIES_KEYS]].assign(step=step + 1, y_pred=preds))

    return pd.concat(outputs, ignore_index=True)


def seasonal_naive_forecast(history: pd.DataFrame, start: dt.date | pd.Timestamp, horizon: int) -> pd.DataFrame:
    """Baseline: repeat the last observed week (same weekday, 7 days earlier)."""
    start = pd.Timestamp(start)
    last_week = history.loc[history["sale_date"] >= start - pd.Timedelta(days=7)]
    out = []
    for step in range(horizon):
        day = start + pd.Timedelta(days=step)
        src = start - pd.Timedelta(days=7) + pd.Timedelta(days=step % 7)
        rows = last_week.loc[last_week["sale_date"] == src, [*SERIES_KEYS, "y"]]
        out.append(rows.assign(sale_date=day, step=step + 1).rename(columns={"y": "y_pred"}))
    known = history[SERIES_KEYS].drop_duplicates()
    res = pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=[*SERIES_KEYS, "y_pred"])
    # series that did not exist a week ago -> 0
    full = known.merge(pd.DataFrame({"sale_date": pd.date_range(start, periods=horizon, freq="D")}), how="cross")
    full["step"] = (full["sale_date"] - start).dt.days + 1
    res = full.merge(res, on=["sale_date", *SERIES_KEYS, "step"], how="left")
    res["y_pred"] = res["y_pred"].fillna(0.0)
    return res[["sale_date", *SERIES_KEYS, "step", "y_pred"]]


def rolling_origin_backtest(
    panel: pd.DataFrame,
    forecast_fn: ForecastFn,
    eval_start: dt.date | pd.Timestamp,
    eval_end: dt.date | pd.Timestamp,
    horizon: int,
    step_days: int,
) -> pd.DataFrame:
    """Simulate production: at each origin, forecast `horizon` days using only data
    before the origin. Returns [sale_date, *SERIES_KEYS, origin, step, y_true, y_pred]
    restricted to dates inside [eval_start, eval_end]."""
    eval_start, eval_end = pd.Timestamp(eval_start), pd.Timestamp(eval_end)
    results: list[pd.DataFrame] = []
    origin = eval_start
    while origin <= eval_end:
        history = panel.loc[panel["sale_date"] < origin]
        h = min(horizon, int((eval_end - origin).days) + 1)
        fc = forecast_fn(history, origin, h)
        actual = panel.loc[(panel["sale_date"] >= origin) & (panel["sale_date"] <= origin + pd.Timedelta(days=h - 1))]
        merged = fc.merge(actual.rename(columns={"y": "y_true"}), on=["sale_date", *SERIES_KEYS], how="inner")
        results.append(merged.assign(origin=origin))
        origin += pd.Timedelta(days=step_days)
    return pd.concat(results, ignore_index=True)
