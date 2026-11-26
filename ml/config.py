"""Central, typed configuration. Every value can be overridden with an env var
(upper-case field name) or a `.env` file in the project root."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TargetMetric = Literal["revenue", "profit", "units"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Supabase -----------------------------------------------------------
    supabase_url: str | None = None
    supabase_service_role_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"),
    )
    supabase_timeout_s: float = 30.0
    supabase_page_size: int = Field(default=1000, ge=1, le=10_000)  # Supabase default max-rows = 1000
    model_bucket: str = "ml-models"

    # --- Data ---------------------------------------------------------------
    data_source: Literal["supabase", "csv"] = "supabase"
    local_data_path: Path = Path("data/sample_sales.csv")
    business_tz: str = "Asia/Colombo"  # CUSTOMIZE: the shop's timezone (defines "today")
    stale_data_days: int = Field(default=3, ge=0)  # fail if newest sale is older than this
    inactive_series_days: int = Field(default=60, ge=1)  # series with no sales for N days are not forecast

    # --- Modelling ----------------------------------------------------------
    target_metric: TargetMetric = "revenue"  # CUSTOMIZE: revenue | profit | units (line items)
    forecast_horizon: int = Field(default=14, ge=1, le=90)  # CUSTOMIZE: days written by the batch job
    max_horizon: int = Field(default=28, ge=1, le=90)  # API upper bound
    # Walk-forward test: 16 weekly origins (112 days); the model is re-fitted at every
    # origin on data before it, exactly as the weekly retrain does in production.
    validation_days: int = Field(default=112, ge=28)
    early_stopping_days: int = Field(default=28, ge=7)
    backtest_step_days: int = Field(default=7, ge=1)
    interval_low: float = Field(default=0.10, gt=0, lt=0.5)   # 80% forecast range
    interval_high: float = Field(default=0.90, gt=0.5, lt=1)
    training_lookback_days: int | None = Field(default=None, ge=90)  # None = use all history
    holiday_country: str | None = "LK"  # python-holidays code; "" disables holiday features
    random_seed: int = 42

    # --- Promotion gate -----------------------------------------------------
    # Candidate WAPE must beat the seasonal-naive baseline by at least this fraction...
    min_improvement_vs_baseline: float = Field(default=0.05, ge=0.0, lt=1.0)
    # ...and, on days neither model has seen, must not be more than this fraction worse
    # than the live (champion) model...
    max_regression_vs_production: float = Field(default=0.10, ge=0.0)
    min_champion_points: int = Field(default=30, ge=1)  # series-days needed for that comparison
    # ...unless the champion is older than this: then a challenger that beats the baseline wins.
    champion_max_age_days: int = Field(default=28, ge=7)

    # --- Live monitoring -------------------------------------------------------
    alert_ratio: float = Field(default=1.5, gt=1.0)       # live error > ratio x tested error -> alert
    alert_min_days: int = Field(default=5, ge=1)
    max_wape: float | None = Field(default=None, gt=0)  # optional absolute ceiling

    # --- Serving ------------------------------------------------------------
    model_source: Literal["local", "supabase"] = "local"
    model_path: Path = Path("artifacts/latest/model.joblib")
    model_cache_dir: Path = Path("/tmp/model_cache")
    history_cache_ttl_s: int = Field(default=300, ge=0)
    admin_token: SecretStr | None = None  # enables POST /admin/reload when set
    api_keys: SecretStr | None = None  # comma-separated; when set, /predict requires X-API-Key
    log_level: str = "INFO"

    @field_validator("supabase_url")
    @classmethod
    def _strip_url(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    @field_validator("holiday_country")
    @classmethod
    def _empty_is_none(cls, v: str | None) -> str | None:
        return v or None

    # ------------------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.business_tz)

    def today(self) -> dt.date:
        return dt.datetime.now(self.tz).date()

    def cutoff_date(self, as_of: dt.date | None = None) -> dt.date:
        """Last COMPLETE business day. Today's sales are still coming in, so they are
        never used for training or as forecast history."""
        return (as_of or self.today()) - dt.timedelta(days=1)

    def api_key_set(self) -> frozenset[str]:
        if not self.api_keys:
            return frozenset()
        return frozenset(k.strip() for k in self.api_keys.get_secret_value().split(",") if k.strip())

    def require_supabase(self) -> tuple[str, str]:
        if not self.supabase_url or not self.supabase_service_role_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
        return self.supabase_url, self.supabase_service_role_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
