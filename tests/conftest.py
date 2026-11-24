from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest

from ml import config as ml_config
from ml.config import Settings
from ml.data_loader import aggregate_daily, load_transactions_csv, normalise_daily
from scripts.make_sample_data import generate

TODAY = Settings().today()
CUTOFF = TODAY - dt.timedelta(days=1)


@pytest.fixture(scope="session")
def sample_csv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("data") / "sales.csv"
    generate(days=320, end=CUTOFF, seed=11).to_csv(path, index=False)
    return path


@pytest.fixture(scope="session")
def sample_daily(sample_csv: Path) -> pd.DataFrame:
    return normalise_daily(aggregate_daily(load_transactions_csv(sample_csv)))


@pytest.fixture
def csv_settings(sample_csv: Path, tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_source="csv", local_data_path=sample_csv,
                    model_source="local", model_path=tmp_path / "missing.joblib",
                    validation_days=28, forecast_horizon=14)


@pytest.fixture(scope="session")
def trained(sample_csv: Path, tmp_path_factory: pytest.TempPathFactory):  # -> TrainResult
    from ml.train import run_training

    out = tmp_path_factory.mktemp("artifacts")
    s = Settings(_env_file=None, data_source="csv", local_data_path=sample_csv)
    return run_training(s, output_dir=out, upload=False)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Isolated env for code that calls get_settings()."""
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY", "ADMIN_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    ml_config.get_settings.cache_clear()
    yield monkeypatch
    ml_config.get_settings.cache_clear()
