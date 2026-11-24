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


@pytest.mark.parametrize(
    ("wape", "base", "prod", "expected"),
    [
        (0.50, 0.80, None, True),
        (0.78, 0.80, None, False),               # not 5% better than baseline
        (0.50, 0.80, 0.40, False),               # >10% worse than production
        (0.43, 0.80, 0.40, True),                # within tolerance of production
        (float("nan"), 0.80, None, False),
    ],
)
def test_gate(wape: float, base: float, prod: float | None, expected: bool) -> None:
    production = {"model_version": "p", "metrics": {"wape": prod}} if prod is not None else None
    d = evaluate_gate(S, {"wape": wape}, {"wape": base}, production)
    assert isinstance(d, GateDecision) and d.promote is expected, d.reason


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
