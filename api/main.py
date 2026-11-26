"""Sales forecasting inference API.

    uvicorn api.main:app --host 0.0.0.0 --port 8000

GET  /health         liveness + readiness + loaded model version
POST /predict        single or batch multi-day forecast
POST /admin/reload   re-resolve the production model without a restart (requires ADMIN_TOKEN)
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from api.schemas import (
    ErrorResponse,
    ForecastPoint,
    HealthResponse,
    PredictRequest,
    PredictResponse,
    SeriesForecast,
    SeriesRef,
)
from ml.config import Settings, get_settings
from ml.data_loader import SERIES_KEYS, DataError, check_freshness, load_daily_sales
from ml.features import build_panel
from ml.forecasting import recursive_forecast
from ml.logging_utils import configure_logging
from ml.predict_batch import active_history, history_window_start
from ml.registry import ModelBundle, load_serving_bundle, utc_now
from ml.supabase_client import SupabaseClient, client_from_settings

log = logging.getLogger("api")


@dataclass
class _HistoryEntry:
    cutoff: dt.date
    window_start: dt.date
    fetched_at: float
    daily: pd.DataFrame


class ForecastService:
    """Holds the loaded model and a short-lived cache of recent daily history."""

    def __init__(self, settings: Settings, bundle: ModelBundle, client: SupabaseClient | None) -> None:
        self.settings = settings
        self.client = client
        self._bundle = bundle
        self._lock = threading.Lock()
        self._history: _HistoryEntry | None = None
        self.started_at = utc_now()
        self._t0 = time.monotonic()

    # -- model -------------------------------------------------------------------
    @property
    def bundle(self) -> ModelBundle:
        return self._bundle

    def reload(self) -> ModelBundle:
        new = load_serving_bundle(self.settings)  # load fully BEFORE swapping
        with self._lock:
            self._bundle = new
            self._history = None
        log.info("model reloaded", extra={"model_version": new.model_version})
        return new

    def uptime(self) -> float:
        return time.monotonic() - self._t0

    # -- history -------------------------------------------------------------------
    def history(self, cutoff: dt.date) -> _HistoryEntry:
        with self._lock:
            h = self._history
            ttl = self.settings.history_cache_ttl_s
            fresh = h is not None and h.cutoff == cutoff and (time.monotonic() - h.fetched_at) < ttl
            if fresh:
                assert h is not None
                return h
            start = history_window_start(self.settings, self._bundle, cutoff)
            daily = load_daily_sales(self.settings, start=start, end=cutoff, client=self.client)
            self._history = _HistoryEntry(cutoff, start, time.monotonic(), daily)
            return self._history

    def history_age(self) -> float | None:
        h = self._history
        return None if h is None else time.monotonic() - h.fetched_at

    # -- prediction ----------------------------------------------------------------
    def predict(self, req: PredictRequest) -> PredictResponse:
        s = self.settings
        if req.horizon > s.max_horizon:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"horizon {req.horizon} exceeds MAX_HORIZON={s.max_horizon}")
        bundle = self.bundle
        hist = self.history(s.cutoff_date())
        newest = check_freshness(hist.daily, s.cutoff_date(), s.stale_data_days)
        cutoff = min(s.cutoff_date(), newest)  # days not entered yet are not zero-sales days
        panel = build_panel(hist.daily, bundle.target_metric, cutoff, start_date=hist.window_start)

        unknown: list[SeriesRef] = []
        if req.series is None:
            history = active_history(panel, cutoff, s.inactive_series_days)
        else:
            wanted = pd.DataFrame([r.model_dump() for r in req.series])
            history = panel.merge(wanted, on=SERIES_KEYS, how="inner")
            present = set(map(tuple, history[SERIES_KEYS].drop_duplicates().to_numpy().tolist()))
            unknown = [r for r in req.series if (r.location, r.category) not in present]

        start = cutoff + dt.timedelta(days=1)
        results: list[SeriesForecast] = []
        warnings: list[str] = []
        if not history.empty:
            fc = recursive_forecast(bundle.pipeline, bundle.fcfg, history, start, req.horizon)
            known = {tuple(x) for x in bundle.known_series}
            for (loc, cat), g in fc.groupby(SERIES_KEYS, sort=True):
                seen = (loc, cat) in known
                if not seen:
                    warnings.append(f"{loc}|{cat} was not in the training data; forecast relies on its recent "
                                    "history only and may be less accurate until the next retrain.")
                results.append(SeriesForecast(
                    location=loc, category=cat, entity_id=f"{loc}|{cat}", seen_in_training=seen,
                    forecasts=[ForecastPoint(date=r.sale_date.date(), step=int(r.step),
                                             predicted_value=round(float(r.y_pred), 2))
                               for r in g.sort_values("step").itertuples(index=False)],
                ))
        if unknown:
            warnings.append(f"{len(unknown)} requested series have no sales history and were skipped.")

        return PredictResponse(
            model_version=bundle.model_version, target_metric=bundle.target_metric, generated_at=utc_now(),
            history_end_date=cutoff, forecast_start_date=start, horizon=req.horizon,
            results=results, unknown_series=unknown, warnings=warnings,
        )


# ----------------------------------------------------------------------------
# App wiring
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    client = client_from_settings(settings) if settings.data_source == "supabase" else None
    try:
        bundle = load_serving_bundle(settings)
    except Exception:
        # Fail fast: a server without a model must not report healthy.
        log.critical("could not load model at startup", exc_info=True,
                     extra={"model_source": settings.model_source, "model_path": str(settings.model_path)})
        if client:
            client.close()
        raise
    app.state.service = ForecastService(settings, bundle, client)
    log.info("api ready", extra={"model_version": bundle.model_version, "target": bundle.target_metric,
                                 "data_source": settings.data_source})
    try:
        yield
    finally:
        if client:
            client.close()


app = FastAPI(
    title="Sales Forecasting API",
    version="1.0.0",
    lifespan=lifespan,
    responses={503: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)


def get_service(request: Request) -> ForecastService:
    return request.app.state.service  # type: ignore[no-any-return]


Service = Annotated[ForecastService, Depends(get_service)]


@app.middleware("http")
async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = rid
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error", extra={"request_id": rid, "path": request.url.path})
        response = JSONResponse({"detail": "internal server error", "request_id": rid}, status_code=500)
    response.headers["x-request-id"] = rid
    if request.url.path != "/health":
        log.info("request", extra={"request_id": rid, "method": request.method, "path": request.url.path,
                                   "status": response.status_code,
                                   "duration_ms": round((time.perf_counter() - t0) * 1000, 1)})
    return response


@app.exception_handler(DataError)
async def data_error_handler(request: Request, exc: DataError) -> JSONResponse:
    log.error("data error", extra={"request_id": getattr(request.state, "request_id", None), "error": str(exc)})
    return JSONResponse({"detail": str(exc), "request_id": getattr(request.state, "request_id", None)},
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "sales-forecasting-api", "docs": "/docs", "health": "/health"}


@app.get("/health", response_model=HealthResponse)
def health(svc: Service) -> HealthResponse:
    b = svc.bundle
    return HealthResponse(
        status="ok", ready=True, model_version=b.model_version, target_metric=b.target_metric,
        model_trained_at=b.trained_at, model_data_end=b.data_end, started_at=svc.started_at,
        uptime_seconds=round(svc.uptime(), 1), history_cache_age_seconds=svc.history_age(),
    )


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Annotated[str | None, Depends(_api_key_header)], svc: Service) -> None:
    """No-op unless API_KEYS is configured."""
    allowed = svc.settings.api_key_set()
    if allowed and not (key and any(secrets.compare_digest(key, k) for k in allowed)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key",
                            headers={"WWW-Authenticate": "ApiKey"})


@app.post("/predict", response_model=PredictResponse, dependencies=[Depends(require_api_key)])
def predict(req: PredictRequest, svc: Service) -> PredictResponse:
    # Sync def -> FastAPI runs it in a worker thread, so the event loop never blocks
    # on Supabase I/O or LightGBM.
    return svc.predict(req)


_bearer = HTTPBearer(auto_error=False)


def require_admin(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)], svc: Service) -> None:
    token = svc.settings.admin_token
    if token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if creds is None or not secrets.compare_digest(creds.credentials, token.get_secret_value()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


@app.post("/admin/reload", dependencies=[Depends(require_admin)])
def reload_model(svc: Service) -> dict[str, str]:
    previous = svc.bundle.model_version
    new = svc.reload()
    return {"previous_model_version": previous, "model_version": new.model_version}
