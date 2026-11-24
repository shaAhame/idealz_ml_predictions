from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.features import FeatureConfig
from ml.forecasting import recursive_forecast, rolling_origin_backtest, seasonal_naive_forecast
from ml.metrics import regression_metrics


def test_metrics_values_and_zero_handling() -> None:
    m = regression_metrics([0, 10, 20], [1, 12, 18])
    assert m["mae"] == pytest.approx(5 / 3)
    assert m["rmse"] == pytest.approx(np.sqrt((1 + 4 + 4) / 3))
    assert m["mape"] == pytest.approx((0.2 + 0.1) / 2)  # zero actual excluded
    assert m["wape"] == pytest.approx(5 / 30)
    assert m["mape_excluded_zero_share"] == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        regression_metrics([], [])


def test_seasonal_naive_repeats_last_week() -> None:
    dates = pd.date_range("2026-03-01", periods=14, freq="D")
    hist = pd.DataFrame({"sale_date": dates, "location": "A", "category": "X", "y": np.arange(14.0)})
    fc = seasonal_naive_forecast(hist, dates[-1] + pd.Timedelta(days=1), 10)
    assert fc["y_pred"].tolist() == [7, 8, 9, 10, 11, 12, 13, 7, 8, 9]
    assert fc["step"].tolist() == list(range(1, 11))


def test_recursive_forecast_contract(trained) -> None:
    from ml.registry import load_bundle

    b = load_bundle(trained.artifact_dir / "model.joblib")
    dates = pd.date_range("2026-01-01", periods=90, freq="D")
    hist = pd.concat([pd.DataFrame({"sale_date": dates, "location": "ISEVEN", "category": c,
                                    "y": np.full(90, 50000.0)}) for c in ("ACCESSORIES", "REPAIRS")])
    start = dates[-1] + pd.Timedelta(days=1)
    fc = recursive_forecast(b.pipeline, b.fcfg, hist, start, 5)
    assert len(fc) == 10 and fc["step"].max() == 5
    assert fc["sale_date"].min() == start and (fc["y_pred"] >= 0).all()
    with pytest.raises(ValueError, match="history must end"):
        recursive_forecast(b.pipeline, b.fcfg, hist, start + pd.Timedelta(days=3), 5)


def test_backtest_only_sees_past_data() -> None:
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    panel = pd.DataFrame({"sale_date": dates, "location": "A", "category": "X", "y": np.arange(60.0)})
    seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def fn(hist: pd.DataFrame, origin: pd.Timestamp, h: int) -> pd.DataFrame:
        seen.append((hist["sale_date"].max(), origin))
        return seasonal_naive_forecast(hist, origin, h)

    bt = rolling_origin_backtest(panel, fn, dates[32], dates[-1], horizon=14, step_days=7)
    assert all(last < origin for last, origin in seen)
    assert bt["sale_date"].between(dates[32], dates[-1]).all()
    assert len(seen) == 4


def test_feature_config_roundtrip() -> None:
    cfg = FeatureConfig(target="profit", lags=(1, 7), rolling_windows=(7,), holiday_country=None)
    assert FeatureConfig.from_dict(cfg.to_dict()) == cfg
    assert "is_holiday" not in cfg.feature_columns()
