"""Pydantic v2 request/response models for the inference API."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _norm(v: str) -> str:
    return " ".join(v.split()).upper()


class SeriesRef(BaseModel):
    """One forecast entity. CUSTOMIZE together with ml.data_loader.SERIES_KEYS."""

    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1, max_length=120, examples=["ISEVEN"])
    category: str = Field(min_length=1, max_length=120, examples=["ACCESSORIES"])

    @field_validator("location", "category")
    @classmethod
    def _normalise(cls, v: str) -> str:
        v = _norm(v)
        if not v:
            raise ValueError("must not be blank")
        return v


class PredictRequest(BaseModel):
    """Single prediction = one item in `series`; batch = many. Omit `series` to
    forecast every series that sold something recently."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"series": [{"location": "ISEVEN", "category": "ACCESSORIES"}], "horizon": 7}]},
    )

    series: list[SeriesRef] | None = Field(default=None, min_length=1, max_length=500)
    horizon: int = Field(default=7, ge=1, le=90, description="days to forecast, starting today")

    @field_validator("series")
    @classmethod
    def _dedupe(cls, v: list[SeriesRef] | None) -> list[SeriesRef] | None:
        if v is None:
            return v
        seen: dict[tuple[str, str], SeriesRef] = {}
        for s in v:
            seen.setdefault((s.location, s.category), s)
        return list(seen.values())


class ForecastPoint(BaseModel):
    date: dt.date
    step: int = Field(ge=1)
    predicted_value: float = Field(ge=0)


class SeriesForecast(BaseModel):
    location: str
    category: str
    entity_id: str
    seen_in_training: bool
    forecasts: list[ForecastPoint]


class PredictResponse(BaseModel):
    model_version: str
    target_metric: str
    generated_at: dt.datetime
    history_end_date: dt.date
    forecast_start_date: dt.date
    horizon: int
    results: list[SeriesForecast]
    unknown_series: list[SeriesRef] = Field(default_factory=list,
                                            description="requested series with no sales history at all")
    warnings: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    ready: bool
    model_version: str | None
    target_metric: str | None
    model_trained_at: str | None
    model_data_end: str | None
    started_at: dt.datetime
    uptime_seconds: float
    history_cache_age_seconds: float | None = None


class ErrorResponse(BaseModel):
    detail: str
    request_id: str | None = None
