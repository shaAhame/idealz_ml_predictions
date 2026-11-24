"""Leakage-free feature engineering for daily sales panels.

Rules that keep this leakage-free (and are enforced by tests/test_features.py):
  * Every target-derived feature at date t uses only y at dates < t
    (lags are shift(k>=1); rolling stats run on shift(1)).
  * Calendar/holiday features depend only on the date itself (known in advance).
  * Features are computed per series (groupby), so series never leak into each other.
  * The same function is used in training, backtesting, batch forecasting and the
    API, so there is no train/serve skew. Only the last `lookback_days()` of history
    are needed to reproduce the features of the next day exactly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from functools import lru_cache

import pandas as pd

from ml.data_loader import SERIES_KEYS

CATEGORICAL_FEATURES: list[str] = list(SERIES_KEYS)
CALENDAR_FEATURES: list[str] = [
    "dow", "dom", "month", "week_of_year", "day_of_year", "is_weekend", "is_month_start", "is_month_end",
]
HOLIDAY_FEATURES: list[str] = ["is_holiday", "holiday_tomorrow", "holiday_yesterday"]


@dataclass(frozen=True)
class FeatureConfig:
    target: str = "revenue"
    lags: tuple[int, ...] = (1, 2, 3, 7, 14, 21, 28)
    rolling_windows: tuple[int, ...] = (7, 14, 28)
    holiday_country: str | None = "LK"
    weekend_days: tuple[int, ...] = (5, 6)  # Sat, Sun (Monday=0)
    extra: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if min(self.lags) < 1:
            raise ValueError("lags must be >= 1 (lag 0 would leak the target)")

    def lookback_days(self) -> int:
        """History length needed to compute every feature for the next day."""
        return max(*self.lags, max(self.rolling_windows) + 1) + 1

    def feature_columns(self) -> list[str]:
        cols = list(CATEGORICAL_FEATURES)
        cols += [f"lag_{k}" for k in self.lags]
        for w in self.rolling_windows:
            cols += [f"roll_mean_{w}", f"roll_std_{w}"]
        cols += ["same_dow_mean_4w", "zero_share_28"]
        cols += CALENDAR_FEATURES
        if self.holiday_country:
            cols += HOLIDAY_FEATURES
        return cols

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> FeatureConfig:
        d = dict(d)
        d["lags"] = tuple(d["lags"])  # type: ignore[arg-type]
        d["rolling_windows"] = tuple(d["rolling_windows"])  # type: ignore[arg-type]
        d["weekend_days"] = tuple(d.get("weekend_days", (5, 6)))  # type: ignore[arg-type]
        return cls(**d)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# Panel construction
# ----------------------------------------------------------------------------
def build_panel(
    daily: pd.DataFrame,
    target: str,
    end_date: dt.date,
    *,
    start_date: dt.date | None = None,
) -> pd.DataFrame:
    """Dense daily panel [sale_date, *SERIES_KEYS, y].

    Each series spans from its first sale to `end_date`; days without sales are 0
    (a shop day with no sales of a category is a real zero, not missing data).
    Negative daily totals (net returns) are clipped to 0 for the target.

    `start_date` declares that `daily` is a WINDOW of a longer history (serving
    loads only recent days): every series then starts at `start_date`, so a
    sparse series gets the same leading zeros it had in training instead of
    NaN lags (train/serve consistency).
    """
    end = pd.Timestamp(end_date)
    df = daily.loc[daily["sale_date"] <= end, ["sale_date", *SERIES_KEYS, target]]
    if start_date is not None:
        df = df.loc[df["sale_date"] >= pd.Timestamp(start_date)]
    if df.empty:
        return pd.DataFrame({"sale_date": pd.Series(dtype="datetime64[ns]"),
                             **{k: pd.Series(dtype=object) for k in SERIES_KEYS}, "y": pd.Series(dtype=float)})

    firsts = df.groupby(SERIES_KEYS, as_index=False)["sale_date"].min()
    if start_date is not None:
        firsts["sale_date"] = pd.Timestamp(start_date)
    grids = [
        pd.DataFrame({"sale_date": pd.date_range(row.sale_date, end, freq="D"),
                      **{k: getattr(row, k) for k in SERIES_KEYS}})
        for row in firsts.itertuples(index=False)
    ]
    grid = pd.concat(grids, ignore_index=True)
    grid["sale_date"] = grid["sale_date"].astype("datetime64[ns]")
    panel = grid.merge(df.rename(columns={target: "y"}), on=["sale_date", *SERIES_KEYS], how="left")
    panel["y"] = panel["y"].fillna(0.0).clip(lower=0.0).astype(float)
    return panel.sort_values([*SERIES_KEYS, "sale_date"]).reset_index(drop=True)


# ----------------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _holiday_dates(country: str, years: tuple[int, ...]) -> frozenset[dt.date]:
    import holidays  # local import keeps module import cheap

    return frozenset(holidays.country_holidays(country, years=list(years)).keys())


def _calendar(dates: pd.Series, cfg: FeatureConfig) -> pd.DataFrame:
    out = pd.DataFrame(index=dates.index)
    out["dow"] = dates.dt.day_of_week.astype("int16")
    out["dom"] = dates.dt.day.astype("int16")
    out["month"] = dates.dt.month.astype("int16")
    out["week_of_year"] = dates.dt.isocalendar().week.astype("int16").to_numpy()
    out["day_of_year"] = dates.dt.day_of_year.astype("int16")
    out["is_weekend"] = dates.dt.day_of_week.isin(cfg.weekend_days).astype("int8")
    out["is_month_start"] = dates.dt.is_month_start.astype("int8")
    out["is_month_end"] = dates.dt.is_month_end.astype("int8")
    if cfg.holiday_country:
        years = tuple(range(int(dates.dt.year.min()) - 1, int(dates.dt.year.max()) + 2))
        hol = _holiday_dates(cfg.holiday_country, years)
        d = dates.dt.date
        one = dt.timedelta(days=1)
        out["is_holiday"] = d.map(lambda x: x in hol).astype("int8")
        out["holiday_tomorrow"] = d.map(lambda x: (x + one) in hol).astype("int8")
        out["holiday_yesterday"] = d.map(lambda x: (x - one) in hol).astype("int8")
    return out


def add_features(panel: pd.DataFrame, cfg: FeatureConfig) -> pd.DataFrame:
    """Return `panel` (same index) plus all feature columns. `y` may be NaN for
    future rows; features of a row never read that row's own `y`."""
    if panel.empty:
        return panel.assign(**{c: pd.Series(dtype=float) for c in cfg.feature_columns() if c not in panel})
    df = panel.sort_values([*SERIES_KEYS, "sale_date"])
    keys = [df[k] for k in SERIES_KEYS]
    y = df["y"].astype(float)
    g = y.groupby(keys, sort=False)

    feats: dict[str, pd.Series] = {}
    for k in cfg.lags:
        feats[f"lag_{k}"] = g.shift(k)

    shifted = g.shift(1)
    gs = shifted.groupby(keys, sort=False)
    for w in cfg.rolling_windows:
        minp = max(2, w // 2)
        feats[f"roll_mean_{w}"] = gs.transform(lambda s, w=w, m=minp: s.rolling(w, min_periods=m).mean())
        feats[f"roll_std_{w}"] = gs.transform(lambda s, w=w, m=minp: s.rolling(w, min_periods=m).std())

    same_dow = pd.concat([g.shift(k) for k in (7, 14, 21, 28)], axis=1)
    feats["same_dow_mean_4w"] = same_dow.mean(axis=1, skipna=True)
    feats["zero_share_28"] = gs.transform(
        lambda s: s.eq(0).astype(float).where(s.notna()).rolling(28, min_periods=7).mean())

    out = pd.concat([df, pd.DataFrame(feats, index=df.index), _calendar(df["sale_date"], cfg)], axis=1)
    return out.loc[panel.index]


def make_xy(features: pd.DataFrame, cfg: FeatureConfig) -> tuple[pd.DataFrame, pd.Series]:
    cols = cfg.feature_columns()
    return features.loc[:, cols], features["y"]


def lookback_start(start: dt.date | pd.Timestamp, cfg: FeatureConfig) -> pd.Timestamp:
    return pd.Timestamp(start) - pd.Timedelta(days=cfg.lookback_days())
