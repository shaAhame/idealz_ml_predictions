from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tests.conftest import CUTOFF


@pytest.fixture
def client(env: pytest.MonkeyPatch, trained, sample_csv: Path):
    env.setenv("DATA_SOURCE", "csv")
    env.setenv("LOCAL_DATA_PATH", str(sample_csv))
    env.setenv("MODEL_SOURCE", "local")
    env.setenv("MODEL_PATH", str(trained.artifact_dir / "model.joblib"))
    env.setenv("ADMIN_TOKEN", "s3cret-token")
    from api.main import app

    with TestClient(app) as c:
        yield c


def test_health(client: TestClient, trained) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["ready"] is True
    assert body["model_version"] == trained.model_version
    assert body["uptime_seconds"] >= 0


def test_single_prediction(client: TestClient) -> None:
    r = client.post("/predict", json={"series": [{"location": " iseven ", "category": "accessories"}], "horizon": 7})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["history_end_date"] == CUTOFF.isoformat()
    assert body["forecast_start_date"] == (CUTOFF + dt.timedelta(days=1)).isoformat()
    [res] = body["results"]
    assert res["entity_id"] == "ISEVEN|ACCESSORIES" and res["seen_in_training"] is True
    assert [p["step"] for p in res["forecasts"]] == list(range(1, 8))
    assert all(p["predicted_value"] >= 0 for p in res["forecasts"])
    assert r.headers["x-request-id"]


def test_batch_and_all_series(client: TestClient) -> None:
    r = client.post("/predict", json={"series": [
        {"location": "ISEVEN", "category": "PHONES"}, {"location": "ISEVEN CITY", "category": "REPAIRS"},
        {"location": "ISEVEN", "category": "PHONES"}, {"location": "GHOST", "category": "X"}], "horizon": 3})
    body = r.json()
    assert r.status_code == 200
    assert len(body["results"]) == 2  # duplicate removed
    assert body["unknown_series"] == [{"location": "GHOST", "category": "X"}]
    assert body["warnings"]
    r = client.post("/predict", json={"horizon": 2})
    assert len(r.json()["results"]) == 8


def test_prediction_is_deterministic_and_cached(client: TestClient) -> None:
    req = {"series": [{"location": "ISEVEN", "category": "REPAIRS"}], "horizon": 5}
    a = client.post("/predict", json=req).json()["results"]
    b = client.post("/predict", json=req).json()["results"]
    assert a == b
    assert client.get("/health").json()["history_cache_age_seconds"] is not None


@pytest.mark.parametrize("payload", [
    {"horizon": 0},
    {"horizon": 29},                                       # > MAX_HORIZON (28)
    {"series": [], "horizon": 3},
    {"series": [{"location": " ", "category": "X"}]},
    {"series": [{"location": "A"}]},
    {"horizon": 3, "unexpected": True},
])
def test_validation_errors(client: TestClient, payload: dict) -> None:
    assert client.post("/predict", json=payload).status_code == 422


def test_admin_reload_requires_token(client: TestClient, trained) -> None:
    assert client.post("/admin/reload").status_code == 401
    assert client.post("/admin/reload", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.post("/admin/reload", headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200 and r.json()["model_version"] == trained.model_version


def test_stale_data_returns_503(env: pytest.MonkeyPatch, trained, sample_csv: Path, tmp_path: Path) -> None:
    stale = tmp_path / "stale.csv"
    df = pd.read_csv(sample_csv)
    df[pd.to_datetime(df["Date"]) < pd.Timestamp(CUTOFF) - pd.Timedelta(days=10)].to_csv(stale, index=False)
    env.setenv("DATA_SOURCE", "csv")
    env.setenv("LOCAL_DATA_PATH", str(stale))
    env.setenv("MODEL_PATH", str(trained.artifact_dir / "model.joblib"))
    from api.main import app

    with TestClient(app) as c:
        r = c.post("/predict", json={"horizon": 3})
    assert r.status_code == 503 and "stale" in r.json()["detail"]


def test_startup_fails_fast_without_model(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("DATA_SOURCE", "csv")
    env.setenv("MODEL_SOURCE", "local")
    env.setenv("MODEL_PATH", str(tmp_path / "missing.joblib"))
    from api.main import app
    from ml.registry import ModelLoadError

    with pytest.raises(ModelLoadError), TestClient(app):
        pass


def test_api_key_enforced_when_configured(env: pytest.MonkeyPatch, trained, sample_csv: Path) -> None:
    env.setenv("DATA_SOURCE", "csv")
    env.setenv("LOCAL_DATA_PATH", str(sample_csv))
    env.setenv("MODEL_PATH", str(trained.artifact_dir / "model.joblib"))
    env.setenv("API_KEYS", "key-one, key-two")
    from api.main import app

    req = {"series": [{"location": "ISEVEN", "category": "PHONES"}], "horizon": 1}
    with TestClient(app) as c:
        assert c.post("/predict", json=req).status_code == 401
        assert c.post("/predict", json=req, headers={"X-API-Key": "nope"}).status_code == 401
        assert c.post("/predict", json=req, headers={"X-API-Key": "key-two"}).status_code == 200
        assert c.get("/health").status_code == 200  # health stays open for load balancers
