from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pandas as pd
import pytest

from ml import supabase_client as sc
from ml.config import Settings
from ml.data_loader import DataError, check_freshness, load_daily_sales, load_transactions_csv
from ml.supabase_client import SupabaseClient, SupabaseError, auth_headers
from tests.fake_supabase import FakeSupabase


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)


def test_auth_headers_new_and_legacy_keys() -> None:
    assert auth_headers("sb_secret_abc") == {"apikey": "sb_secret_abc"}
    assert auth_headers("eyJhbGciOi.x.y") == {"apikey": "eyJhbGciOi.x.y", "Authorization": "Bearer eyJhbGciOi.x.y"}


def test_pagination_walks_all_pages(sample_daily: pd.DataFrame) -> None:
    fake = FakeSupabase(sample_daily, page_cap=1000)
    with SupabaseClient("https://x.supabase.co", "sb_secret_k", transport=fake.transport()) as c:
        rows = c.select_all("v_daily_sales", select="sale_date,location,category,revenue",
                            order="sale_date.asc,location.asc,category.asc", page_size=700)
    assert len(rows) == len(sample_daily)
    offsets = [int(dict(r.url.params)["offset"]) for r in fake.requests]
    assert offsets[:3] == [0, 700, 1400]
    assert len({(r["sale_date"], r["location"], r["category"]) for r in rows}) == len(rows)


def test_retries_then_succeeds_and_fails_fast_on_4xx() -> None:
    fake = FakeSupabase()
    fake.fail_next = [503, 429]
    with SupabaseClient("https://x.supabase.co", "sb_secret_k", transport=fake.transport()) as c:
        c.upsert("sales_forecasts", [{"a": 1, "b": 2}], on_conflict="a,b")
        assert len(fake.requests) == 3
        fake.fail_next = [400]
        with pytest.raises(SupabaseError, match="HTTP 400"):
            c.upsert("sales_forecasts", [{"a": 1, "b": 2}], on_conflict="a,b")
        assert len(fake.requests) == 4


def test_upsert_chunks_and_headers() -> None:
    fake = FakeSupabase()
    rows = [{"model_version": "v", "target_metric": "revenue", "location": "L", "category": str(i),
             "forecast_date": "2026-01-01"} for i in range(1200)]
    with SupabaseClient("https://x.supabase.co", "sb_secret_k", transport=fake.transport()) as c:
        c.upsert("sales_forecasts", rows, on_conflict="model_version,target_metric,location,category,forecast_date")
        c.upsert("sales_forecasts", rows, on_conflict="model_version,target_metric,location,category,forecast_date")
    assert len(fake.requests) == 6  # 3 chunks x 2 runs
    assert len(fake.forecasts) == 1200  # idempotent
    assert fake.requests[0].headers["prefer"] == "resolution=merge-duplicates,return=minimal"


def test_load_daily_from_supabase_matches_csv(sample_daily: pd.DataFrame, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSupabase(sample_daily, page_cap=500)
    s = Settings(_env_file=None, supabase_url="https://x.supabase.co", supabase_service_role_key="sb_secret_k",
                 supabase_page_size=500)
    client = SupabaseClient("https://x.supabase.co", "sb_secret_k", transport=fake.transport())
    got = load_daily_sales(s, client=client)
    pd.testing.assert_frame_equal(got, sample_daily.reset_index(drop=True), check_dtype=False)
    start = sample_daily["sale_date"].max().date() - dt.timedelta(days=9)
    part = load_daily_sales(s, client=client, start=start)
    assert part["sale_date"].min().date() == start


def test_csv_cleaning(tmp_path: Path) -> None:
    p = tmp_path / "s.csv"
    p.write_text(
        "Date,Location,Category,Bill No.,Brand/Item,Sales Rep,Revenue,Cost,Profit\n"
        "2026-07-01,iseven ,Accessories,1604,X,A,\"10,000\",7500,2500\n"
        "01/07/2026,ISEVEN,ACCESSORIES,1604,Y,A,Rs. 3000,(200),\n"
        ",ISEVEN,ACCESSORIES,1605,Z,A,100,50,50\n"
        "2026-07-02,ISEVEN,ACCESSORIES,,Z,A,100,50,50\n"
    )
    tx = load_transactions_csv(p)
    assert len(tx) == 2
    assert tx["revenue"].tolist() == [10000.0, 3000.0]
    assert tx["cost"].tolist() == [7500.0, -200.0]
    assert (tx["sale_date"] == pd.Timestamp("2026-07-01")).all()
    assert set(tx["location"]) == {"ISEVEN"} and set(tx["category"]) == {"ACCESSORIES"}


def test_freshness_check(sample_daily: pd.DataFrame) -> None:
    newest = sample_daily["sale_date"].max().date()
    assert check_freshness(sample_daily, newest + dt.timedelta(days=3), 3) == newest
    with pytest.raises(DataError, match="stale"):
        check_freshness(sample_daily, newest + dt.timedelta(days=4), 3)


def test_missing_credentials_message() -> None:
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        Settings(_env_file=None, supabase_url=None).require_supabase()


def test_secret_key_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_alias")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    s = Settings(_env_file=None)
    assert s.supabase_service_role_key is not None
    assert s.supabase_service_role_key.get_secret_value() == "sb_secret_alias"
    assert httpx  # keep import used
