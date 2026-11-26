"""Full pipeline against an emulated Supabase:
train (upload + register + promote) -> batch forecast upsert -> API serving the
production model downloaded from Storage with SHA-256 verification."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ml import supabase_client
from ml.config import Settings, get_settings
from ml.supabase_client import SupabaseClient
from tests.fake_supabase import FakeSupabase

PATCH_TARGETS = ["ml.supabase_client", "ml.train", "ml.predict_batch", "ml.registry", "ml.data_loader", "ml.monitor",
                 "api.main"]


@pytest.fixture
def fake(env: pytest.MonkeyPatch, sample_daily: pd.DataFrame, tmp_path: Path) -> FakeSupabase:
    import importlib

    fake = FakeSupabase(sample_daily)

    def factory(settings: Settings) -> SupabaseClient:
        url, key = settings.require_supabase()
        return SupabaseClient(url, key, transport=fake.transport())

    for mod in PATCH_TARGETS:
        env.setattr(importlib.import_module(mod), "client_from_settings", factory)
    env.setattr(supabase_client.time, "sleep", lambda s: None)
    env.setenv("SUPABASE_URL", "https://fake.supabase.co")
    env.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_test")
    env.setenv("DATA_SOURCE", "supabase")
    env.setenv("MODEL_SOURCE", "supabase")
    env.setenv("MODEL_CACHE_DIR", str(tmp_path / "cache"))
    env.setenv("SUPABASE_PAGE_SIZE", "400")
    return fake


def test_train_forecast_serve(fake: FakeSupabase, tmp_path: Path) -> None:
    from ml import predict_batch, train

    # 1. train + promote
    assert train.main(["--output-dir", str(tmp_path / "art")]) == 0
    [entry] = fake.registry.values()
    assert entry["status"] == "production", entry["decision_reason"]
    assert entry["metrics"]["wape"] < entry["baseline_metrics"]["wape"]
    assert any(k.endswith("/model.joblib") for k in fake.storage)

    # 2. a second training run that is forced NOT to promote stays a candidate
    assert train.main(["--output-dir", str(tmp_path / "art2"), "--promote", "never"]) == 0
    statuses = sorted(r["status"] for r in fake.registry.values())
    assert statuses == ["candidate", "production"]

    # 3. batch forecast (twice -> idempotent)
    get_settings.cache_clear()
    assert predict_batch.main([]) == 0
    n = len(fake.forecasts)
    assert n == 8 * 14
    assert predict_batch.main([]) == 0
    assert len(fake.forecasts) == n
    row = next(iter(fake.forecasts.values()))
    assert row["model_version"] == entry["model_version"] and row["target_metric"] == "revenue"

    # 4. API downloads the production model from Storage and serves it
    from api.main import app

    with TestClient(app) as c:
        h = c.get("/health").json()
        assert h["model_version"] == entry["model_version"]
        r = c.post("/predict", json={"series": [{"location": "ISEVEN", "category": "PHONES"}], "horizon": 14})
        assert r.status_code == 200
        api_vals = [p["predicted_value"] for p in r.json()["results"][0]["forecasts"]]
    batch_vals = [v["predicted_value"] for k, v in sorted(fake.forecasts.items())
                  if v["location"] == "ISEVEN" and v["category"] == "PHONES"]
    assert api_vals == pytest.approx(batch_vals, abs=0.01)  # batch and API agree exactly


def test_corrupted_storage_object_blocks_startup(fake: FakeSupabase, tmp_path: Path) -> None:
    from ml import train
    from ml.registry import ModelLoadError

    assert train.main(["--output-dir", str(tmp_path / "art")]) == 0
    key = next(k for k in fake.storage if k.endswith("model.joblib"))
    fake.storage[key] = fake.storage[key][:-10] + b"0123456789"
    get_settings.cache_clear()
    from api.main import app

    with pytest.raises(ModelLoadError, match="SHA-256"), TestClient(app):
        pass


def test_batch_forecast_without_promoted_model_exits_3(fake: FakeSupabase, env: pytest.MonkeyPatch) -> None:
    """The pipeline tolerates exit code 3 (no model yet for a target) but not other failures."""
    from ml import predict_batch

    env.setenv("TARGET_METRIC", "units")
    get_settings.cache_clear()
    assert predict_batch.main(["--dry-run"]) == 3
    assert not fake.forecasts


def test_champion_compared_on_unseen_days(fake: FakeSupabase, tmp_path: Path) -> None:
    """Train a champion 14 days ago, then a challenger today: both are scored on the
    two weeks after the champion's data_end, which neither was trained on."""
    import datetime as dt

    from ml import train

    today = Settings(_env_file=None).today()
    past = (today - dt.timedelta(days=14)).isoformat()
    assert train.main(["--output-dir", str(tmp_path / "a"), "--as-of", past]) == 0
    champ = next(iter(fake.registry.values()))
    assert champ["status"] == "production"
    assert train.main(["--output-dir", str(tmp_path / "b")]) == 0
    new = next(r for r in fake.registry.values() if r["model_version"] != champ["model_version"])
    cmp = new["metrics"]["champion_comparison"]
    assert cmp["champion_version"] == champ["model_version"]
    assert cmp["origins"] == 2 and cmp["points"] >= 100
    assert 0 < cmp["champion_wape"] < 2 and 0 < cmp["challenger_wape"] < 2
    assert "same unseen days" in new["decision_reason"] or "worse than" in new["decision_reason"]


def test_snapshots_ranges_and_monitor(fake: FakeSupabase, tmp_path: Path, env: pytest.MonkeyPatch) -> None:
    import datetime as dt

    from ml import monitor, predict_batch, train

    assert train.main(["--output-dir", str(tmp_path / "art")]) == 0
    get_settings.cache_clear()
    today = Settings(_env_file=None).today()
    # issue forecasts on each of the last 10 days, as the nightly job would have
    for k in range(10, 0, -1):
        assert predict_batch.main(["--as-of", (today - dt.timedelta(days=k)).isoformat()]) == 0
    assert predict_batch.main([]) == 0
    snaps = list(fake.snapshots.values())
    assert len(snaps) == 11 * 14 * 8
    assert all(s["predicted_lower"] <= s["predicted_value"] <= s["predicted_upper"] for s in snaps)
    assert predict_batch.main([]) == 0 and len(fake.snapshots) == 11 * 14 * 8   # idempotent

    live = monitor.live_accuracy(pd.DataFrame(fake._accuracy_rows()), today)
    assert live["next_day"]["days"] == 10 and live["week_totals"]["weeks"] >= 2
    assert 0.3 <= live["next_day"]["range_coverage"] <= 1
    assert monitor.main() == 0                                   # healthy

    for s in fake.snapshots.values():                            # forecasts suddenly 5x too high
        s["predicted_value"] *= 5
    assert monitor.main() == 4                                   # alert -> red run


def test_unmigrated_database_does_not_break_forecasts(fake: FakeSupabase, tmp_path: Path) -> None:
    from ml import monitor, predict_batch, train

    assert train.main(["--output-dir", str(tmp_path / "art")]) == 0
    get_settings.cache_clear()
    fake.snapshots_missing = True
    assert predict_batch.main([]) == 0
    assert len(fake.forecasts) == 8 * 14 and not fake.snapshots
    assert monitor.main() == 0
