"""Generate forecasts for the next FORECAST_HORIZON days and upsert them into
`sales_forecasts` (idempotent: re-running the same model on the same day
overwrites, it never duplicates).

    python -m ml.predict_batch                    # production model -> Supabase
    python -m ml.predict_batch --dry-run --out forecasts.csv
    MODEL_SOURCE=local MODEL_PATH=artifacts/latest/model.joblib python -m ml.predict_batch --dry-run

Forecasts start TODAY (business timezone): today's sales are still incomplete,
so history ends yesterday and today is forecast step 1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import uuid
from pathlib import Path

import pandas as pd

from ml.config import Settings, get_settings
from ml.data_loader import SERIES_KEYS, DataError, check_freshness, load_daily_sales
from ml.features import build_panel
from ml.forecasting import recursive_forecast
from ml.logging_utils import configure_logging
from ml.registry import ModelBundle, load_serving_bundle, utc_now
from ml.supabase_client import client_from_settings

log = logging.getLogger("ml.predict_batch")
FORECAST_TABLE = "sales_forecasts"
FORECAST_CONFLICT = "model_version,target_metric,location,category,forecast_date"


def active_history(panel: pd.DataFrame, cutoff: dt.date, inactive_days: int) -> pd.DataFrame:
    """Keep series with at least one sale in the last `inactive_days` days."""
    recent = panel.loc[panel["sale_date"] > pd.Timestamp(cutoff) - pd.Timedelta(days=inactive_days)]
    active = recent.loc[recent["y"] > 0, SERIES_KEYS].drop_duplicates()
    return panel.merge(active, on=SERIES_KEYS, how="inner")


def history_window_start(settings: Settings, bundle: ModelBundle, cutoff: dt.date) -> dt.date:
    """Enough history for the features plus the activity check."""
    return cutoff - dt.timedelta(days=max(bundle.fcfg.lookback_days(), settings.inactive_series_days) + 7)


def generate_forecasts(
    settings: Settings,
    bundle: ModelBundle,
    daily: pd.DataFrame,
    *,
    as_of: dt.date | None = None,
    horizon: int | None = None,
    window_start: dt.date | None = None,
) -> pd.DataFrame:
    """`window_start`: first date of `daily` when only a recent window was loaded."""
    cfg = bundle.fcfg
    cutoff = settings.cutoff_date(as_of)
    check_freshness(daily, cutoff, settings.stale_data_days)
    panel = build_panel(daily, bundle.target_metric, cutoff, start_date=window_start)
    history = active_history(panel, cutoff, settings.inactive_series_days)
    if history.empty:
        raise DataError(f"No active series in the last {settings.inactive_series_days} days.")
    start = cutoff + dt.timedelta(days=1)
    fc = recursive_forecast(bundle.pipeline, cfg, history, start, horizon or settings.forecast_horizon)
    return fc


def to_rows(fc: pd.DataFrame, bundle: ModelBundle, run_id: str) -> list[dict[str, object]]:
    generated_at = utc_now().isoformat()
    return [
        {
            "forecast_date": r.sale_date.date().isoformat(),
            "location": r.location,
            "category": r.category,
            "target_metric": bundle.target_metric,
            "predicted_value": round(float(r.y_pred), 2),
            "model_version": bundle.model_version,
            "run_id": run_id,
            "generated_at": generated_at,
        }
        for r in fc.itertuples(index=False)
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as-of", type=dt.date.fromisoformat, help="pretend today is this date")
    p.add_argument("--horizon", type=int, help="override FORECAST_HORIZON")
    p.add_argument("--dry-run", action="store_true", help="do not write to Supabase")
    p.add_argument("--out", type=Path, help="also write forecasts to this CSV")
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    run_id = str(uuid.uuid4())
    try:
        bundle = load_serving_bundle(settings)
        cutoff = settings.cutoff_date(args.as_of)
        start = history_window_start(settings, bundle, cutoff)
        daily = load_daily_sales(settings, start=start, end=cutoff)
        fc = generate_forecasts(settings, bundle, daily, as_of=args.as_of, horizon=args.horizon, window_start=start)
    except DataError as exc:
        log.error("batch forecast aborted: %s", exc)
        return 2

    rows = to_rows(fc, bundle, run_id)
    if args.out:
        pd.DataFrame(rows).to_csv(args.out, index=False)
    if not args.dry_run:
        with client_from_settings(settings) as client:
            client.upsert(FORECAST_TABLE, rows, on_conflict=FORECAST_CONFLICT)
    log.info("batch forecast finished", extra={
        "run_id": run_id, "model_version": bundle.model_version, "rows": len(rows),
        "series": int(fc.groupby(SERIES_KEYS).ngroups), "first_date": rows[0]["forecast_date"],
        "last_date": rows[-1]["forecast_date"], "written_to_supabase": not args.dry_run})
    return 0


if __name__ == "__main__":
    sys.exit(main())
