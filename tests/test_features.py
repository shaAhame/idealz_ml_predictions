from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from ml.features import FeatureConfig, add_features, build_panel, lookback_start


def _panel(n_days: int = 80, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    frames = [pd.DataFrame({"sale_date": dates, "location": loc, "category": cat,
                            "y": rng.gamma(2.0, 100.0, n_days).round(2)})
              for loc, cat in [("A", "X"), ("A", "Y"), ("B", "X")]]
    return pd.concat(frames, ignore_index=True)


CFG = FeatureConfig(holiday_country="LK")


def test_lag_zero_is_rejected() -> None:
    with pytest.raises(ValueError):
        FeatureConfig(lags=(0, 1))


def test_features_do_not_read_current_or_future_target() -> None:
    """Changing y at date t (and later) must not change any feature at dates <= t."""
    panel = _panel()
    base = add_features(panel, CFG)
    t = pd.Timestamp("2026-02-15")
    perturbed = panel.copy()
    perturbed.loc[perturbed["sale_date"] >= t, "y"] *= 1000
    after = add_features(perturbed, CFG)
    cols = CFG.feature_columns()
    mask = base["sale_date"] <= t
    pd.testing.assert_frame_equal(base.loc[mask, cols], after.loc[mask, cols])
    # sanity: features strictly after t DO change
    assert not base.loc[base["sale_date"] == t + pd.Timedelta(days=1), "lag_1"].equals(
        after.loc[after["sale_date"] == t + pd.Timedelta(days=1), "lag_1"])


def test_series_do_not_leak_into_each_other() -> None:
    panel = _panel()
    base = add_features(panel, CFG)
    other = panel.copy()
    other.loc[(other["location"] == "B"), "y"] = 0.0
    after = add_features(other, CFG)
    m = base["location"] == "A"
    pd.testing.assert_frame_equal(base.loc[m, CFG.feature_columns()], after.loc[m, CFG.feature_columns()])


def test_lag_values_are_correct() -> None:
    panel = _panel()
    f = add_features(panel, CFG)
    s = f[(f["location"] == "A") & (f["category"] == "X")].sort_values("sale_date")
    assert s["lag_7"].iloc[10] == s["y"].iloc[3]
    assert np.isclose(s["roll_mean_7"].iloc[10], s["y"].iloc[3:10].mean())
    assert np.isclose(s["same_dow_mean_4w"].iloc[30], s["y"].iloc[[23, 16, 9, 2]].mean())


def test_truncated_history_reproduces_training_features() -> None:
    """Serving only loads `lookback_days()` of history: next-day features must equal
    the features computed on the full history (no train/serve skew)."""
    panel = _panel(120)
    day = pd.Timestamp("2026-04-20")
    full = add_features(panel.loc[panel["sale_date"] <= day], CFG)
    window = panel.loc[(panel["sale_date"] <= day) & (panel["sale_date"] >= lookback_start(day, CFG))]
    trunc = add_features(window, CFG)
    cols = CFG.feature_columns()
    a = full.loc[full["sale_date"] == day, cols].reset_index(drop=True)
    b = trunc.loc[trunc["sale_date"] == day, cols].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_build_panel_zero_fills_and_clips() -> None:
    daily = pd.DataFrame({
        "sale_date": pd.to_datetime(["2026-01-01", "2026-01-04", "2026-01-02"]),
        "location": ["A", "A", "B"], "category": ["X", "X", "X"], "revenue": [10.0, -5.0, 3.0],
    })
    p = build_panel(daily, "revenue", dt.date(2026, 1, 5))
    a = p[p["location"] == "A"]
    assert a["sale_date"].min() == pd.Timestamp("2026-01-01") and len(a) == 5
    assert a["y"].tolist() == [10.0, 0.0, 0.0, 0.0, 0.0]  # zero-fill + negative clipped
    assert p[p["location"] == "B"]["sale_date"].min() == pd.Timestamp("2026-01-02")
    # window mode: every series starts at the window start
    w = build_panel(daily, "revenue", dt.date(2026, 1, 5), start_date=dt.date(2026, 1, 1))
    assert w.groupby("location")["sale_date"].min().eq(pd.Timestamp("2026-01-01")).all()


def test_holiday_features_sri_lanka() -> None:
    panel = pd.DataFrame({"sale_date": pd.date_range("2026-04-12", "2026-04-16"),
                          "location": "A", "category": "X", "y": 1.0})
    f = add_features(panel, CFG).set_index("sale_date")
    assert f.loc["2026-04-13", "is_holiday"] == 1  # Sinhala and Tamil New Year eve
    assert f.loc["2026-04-12", "holiday_tomorrow"] == 1
