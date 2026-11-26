from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml.config import Settings
from ml.data_loader import DataError
from ml.registry import ModelLoadError, load_bundle, sha256_file
from ml.train import GateDecision, evaluate_gate, run_training

S = Settings(_env_file=None)


def test_training_produces_bundle_and_metadata(trained) -> None:
    d: Path = trained.artifact_dir
    assert (d / "model.joblib").exists() and (d / "metadata.json").exists()
    meta = json.loads((d / "metadata.json").read_text())
    for k in ("mae", "rmse", "mape", "wape"):
        assert k in meta["metrics"] and k in meta["baseline_metrics"]
    assert meta["artifact_sha256"] == sha256_file(d / "model.joblib")
    b = load_bundle(d / "model.joblib", meta["artifact_sha256"])
    assert b.feature_columns == b.fcfg.feature_columns()
    assert b.target_metric == "revenue" and len(b.known_series) == 8
    # model must be meaningfully better than repeating last week on this synthetic data
    assert trained.metrics["wape"] < trained.baseline_metrics["wape"]
    assert (d.parent / "latest" / "model.joblib").exists()


def test_tampered_artifact_is_rejected(trained, tmp_path: Path) -> None:
    p = tmp_path / "model.joblib"
    p.write_bytes((trained.artifact_dir / "model.joblib").read_bytes() + b"x")
    with pytest.raises(ModelLoadError, match="SHA-256"):
        load_bundle(p, sha256_file(trained.artifact_dir / "model.joblib"))
    with pytest.raises(ModelLoadError, match="not found"):
        load_bundle(tmp_path / "nope.joblib")


PROD = {"model_version": "p", "metrics": {"wape": 0.4}}


@pytest.mark.parametrize(
    ("wape", "base", "champion", "expected"),
    [
        (0.50, 0.80, None, True),                                      # no comparison possible -> baseline decides
        (0.78, 0.80, None, False),                                     # not 5% better than baseline
        (float("nan"), 0.80, None, False),
        (0.50, 0.80, {"points": 100, "challenger_wape": 0.50, "champion_wape": 0.40, "champion_age_days": 7}, False),
        (0.50, 0.80, {"points": 100, "challenger_wape": 0.43, "champion_wape": 0.40, "champion_age_days": 7}, True),
        (0.50, 0.80, {"points": 10, "challenger_wape": 0.90, "champion_wape": 0.40, "champion_age_days": 7}, True),
        (0.50, 0.80, {"points": 100, "challenger_wape": 0.90, "champion_wape": 0.40, "champion_age_days": 35}, True),
    ],
)
def test_gate(wape: float, base: float, champion: dict | None, expected: bool) -> None:
    d = evaluate_gate(S, {"wape": wape}, {"wape": base}, PROD, champion)
    assert isinstance(d, GateDecision) and d.promote is expected, d.reason


def test_gate_without_production() -> None:
    assert evaluate_gate(S, {"wape": 0.5}, {"wape": 0.8}, None).promote


def test_max_wape_gate() -> None:
    s = Settings(_env_file=None, max_wape=0.3)
    assert not evaluate_gate(s, {"wape": 0.5}, {"wape": 0.9}, None).promote


def test_not_enough_history(sample_csv: Path, tmp_path: Path) -> None:
    short = tmp_path / "short.csv"
    df = pd.read_csv(sample_csv)
    df[df["Date"] >= sorted(df["Date"].unique())[-60]].to_csv(short, index=False)
    s = Settings(_env_file=None, data_source="csv", local_data_path=short)
    with pytest.raises(DataError, match="at least"):
        run_training(s, output_dir=tmp_path, upload=False)


def test_units_model_and_weekly_accuracy(sample_csv: Path, tmp_path: Path) -> None:
    s = Settings(_env_file=None, data_source="csv", local_data_path=sample_csv, target_metric="units")
    r = run_training(s, output_dir=tmp_path, upload=False)
    assert r.model_version.startswith("units-")
    for res in (r.metrics, r.baseline_metrics):
        assert res["weekly"]["n"] > 0 and 0 <= res["weekly"]["wape"] < 1
    # weekly totals are much more accurate than single days
    assert r.metrics["weekly"]["wape"] < r.metrics["wape"]
    assert r.metrics["wape"] < r.baseline_metrics["wape"]


def test_weekly_metrics_uses_only_complete_weeks() -> None:
    import pandas as pd

    from ml.train import weekly_metrics

    rows = []
    for origin, n in (("2026-01-01", 7), ("2026-01-08", 3)):  # second window is incomplete
        for step in range(1, n + 1):
            rows.append({"origin": origin, "location": "A", "category": "X", "step": step,
                         "y_true": 10.0, "y_pred": 12.0})
    m = weekly_metrics(pd.DataFrame(rows))
    assert m is not None and m["n"] == 1 and abs(m["wape"] - 0.2) < 1e-9


def test_walk_forward_refits_only_on_past_data(
    sample_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ml.train as train_mod
    from ml.forecasting import weekly_origins

    calls: list[pd.Timestamp] = []
    real = train_mod.make_xy

    def spy(features, cfg):
        calls.append(features["sale_date"].max())
        return real(features, cfg)

    monkeypatch.setattr(train_mod, "make_xy", spy)
    s = Settings(_env_file=None, data_source="csv", local_data_path=sample_csv, validation_days=56)
    r = run_training(s, output_dir=tmp_path, upload=False)
    origins = weekly_origins(s.cutoff_date(), 8)
    assert r.metrics["test"]["origins"] == 8
    assert origins[-1] == pd.Timestamp(s.cutoff_date()) - pd.Timedelta(days=6)
    walk = calls[2:2 + len(origins)]           # after the train / early-stopping splits, before the final fit
    assert all(last < o for last, o in zip(walk, origins, strict=True))
    assert calls[-1] == pd.Timestamp(s.cutoff_date())  # shipped model uses everything up to yesterday


def test_forecast_ranges_are_calibrated(trained) -> None:
    iv = trained.metrics["intervals"]
    assert 0.6 <= iv["coverage_d1_7"] <= 0.95
    assert len(iv["combos"]) == 1 + 2 + 4 + 8      # all, per location, per category, per pair
    q = iv["combos"]["*|*"]
    for k in ("d1_7", "d8_14", "d7", "d14"):
        assert q[k]["lo"] < q[k]["hi"]
    from ml.intervals import apply_range

    low, high = apply_range(1000.0, q["d7"])
    assert 0 <= low < high
    assert apply_range(1000.0, None) == (None, None)
