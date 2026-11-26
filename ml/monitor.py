"""Live accuracy monitor: compares forecasts that were actually issued (from
`forecast_snapshots`, via the `v_forecast_accuracy` view) with what was sold, and
raises an alert when they are much worse than the model's tested accuracy.

    TARGET_METRIC=revenue python -m ml.monitor

Exit codes: 0 ok / not enough data yet, 3 no production model, 4 ALERT.
GitHub marks a run red on a non-zero exit and e-mails per your notification settings.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import Any

import pandas as pd

from ml.config import Settings, get_settings
from ml.data_loader import SERIES_KEYS
from ml.logging_utils import configure_logging
from ml.metrics import regression_metrics
from ml.registry import ModelRegistry
from ml.supabase_client import SupabaseClient, SupabaseError, client_from_settings

log = logging.getLogger("ml.monitor")
VIEW = "v_forecast_accuracy"
WINDOW_DAYS = 28


def live_accuracy(rows: pd.DataFrame, today: dt.date, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Live metrics from issued forecasts. `rows` = v_forecast_accuracy rows (only past days)."""
    out: dict[str, Any] = {"next_day": None, "next_day_last7": None, "week_totals": None}
    if rows.empty:
        return out
    df = rows.copy()
    for c in ("issue_date", "forecast_date"):
        df[c] = pd.to_datetime(df[c])
    for c in ("predicted_value", "predicted_lower", "predicted_upper", "actual_value", "horizon_days"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    t = pd.Timestamp(today)

    def score(sub: pd.DataFrame) -> dict[str, Any] | None:
        if sub.empty:
            return None
        m = regression_metrics(sub["actual_value"], sub["predicted_value"])
        has = sub["predicted_lower"].notna() & sub["predicted_upper"].notna()
        cov = ((sub.loc[has, "actual_value"] >= sub.loc[has, "predicted_lower"])
               & (sub.loc[has, "actual_value"] <= sub.loc[has, "predicted_upper"])).mean() if has.any() else None
        return {"wape": m["wape"], "bias": m["bias"], "days": int(sub["forecast_date"].nunique()),
                "points": len(sub), "range_coverage": None if cov is None else float(cov)}

    d1 = df.loc[df["horizon_days"] == 1]
    out["next_day"] = score(d1.loc[d1["forecast_date"] >= t - pd.Timedelta(days=window_days)])
    out["next_day_last7"] = score(d1.loc[d1["forecast_date"] >= t - pd.Timedelta(days=7)])

    wk = df.loc[(df["horizon_days"] <= 7) & (df["issue_date"] + pd.Timedelta(days=6) < t)
                & (df["issue_date"] >= t - pd.Timedelta(days=window_days + 6))]
    tot = wk.groupby(["issue_date", *SERIES_KEYS]).agg(t=("actual_value", "sum"), p=("predicted_value", "sum"),
                                                      n=("horizon_days", "size"))
    tot = tot.loc[tot["n"] == 7]
    if not tot.empty:
        m = regression_metrics(tot["t"], tot["p"])
        out["week_totals"] = {"wape": m["wape"], "weeks": int(tot.index.get_level_values(0).nunique()),
                              "points": len(tot)}
    return out


def evaluate_alerts(settings: Settings, live: dict[str, Any], tested: dict[str, Any]) -> list[str]:
    """Compare live with tested accuracy (like for like: next-day vs the 1-7 day test
    error, 7-day totals vs the weekly test error)."""
    alerts: list[str] = []
    ref_day = (tested.get("by_horizon", {}).get("1-7") or {}).get("wape") or tested.get("wape")
    last7 = live.get("next_day_last7")
    if ref_day and last7 and last7["days"] >= settings.alert_min_days:
        limit = max(settings.alert_ratio * ref_day, ref_day + 0.15)
        if last7["wape"] > limit:
            alerts.append(f"next-day forecasts over the last {last7['days']} days missed by {last7['wape']:.0%} "
                          f"(tested {ref_day:.0%}, alert above {limit:.0%})")
    ref_week = (tested.get("weekly") or {}).get("wape")
    wk = live.get("week_totals")
    if ref_week and wk and wk["weeks"] >= 2:
        limit = max(settings.alert_ratio * ref_week, ref_week + 0.10)
        if wk["wape"] > limit:
            alerts.append(f"7-day totals over the last {wk['weeks']} weeks missed by {wk['wape']:.0%} "
                          f"(tested {ref_week:.0%}, alert above {limit:.0%})")
    return alerts


def fetch_rows(client: SupabaseClient, target: str, since: dt.date) -> pd.DataFrame:
    rows = client.select_all(
        VIEW, select="issue_date,forecast_date,horizon_days,location,category,predicted_value,predicted_lower,"
                     "predicted_upper,actual_value",
        filters=[("target_metric", f"eq.{target}"), ("forecast_date", f"gte.{since.isoformat()}")],
        order="issue_date.asc,forecast_date.asc,location.asc,category.asc")
    return pd.DataFrame(rows, columns=["issue_date", "forecast_date", "horizon_days", "location", "category",
                                       "predicted_value", "predicted_lower", "predicted_upper", "actual_value"])


def _summary(target: str, live: dict[str, Any], tested: dict[str, Any], alerts: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return

    def pct(x: Any) -> str:
        return "—" if x is None else f"{x:.0%}"

    nd, wk = live.get("next_day") or {}, live.get("week_totals") or {}
    with open(path, "a") as f:
        f.write(f"## Live accuracy: {target}\n\n| | live | tested |\n|---|---|---|\n")
        f.write(f"| next-day error (last {nd.get('days', 0)} days) | {pct(nd.get('wape'))} | "
                f"{pct((tested.get('by_horizon', {}).get('1-7') or {}).get('wape'))} |\n")
        f.write(f"| 7-day total error ({wk.get('weeks', 0)} weeks) | {pct(wk.get('wape'))} | "
                f"{pct((tested.get('weekly') or {}).get('wape'))} |\n")
        f.write(f"| actual inside 80% range | {pct(nd.get('range_coverage'))} | 80% |\n\n")
        f.write(("**ALERT:** " + "; ".join(alerts)) if alerts else "No alert.")
        f.write("\n\n")


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    target = settings.target_metric
    today = settings.today()
    with client_from_settings(settings) as client:
        prod = ModelRegistry(client, settings.model_bucket).production(target)
        if not prod:
            log.warning("no production model; nothing to monitor", extra={"target": target})
            return 3
        try:
            rows = fetch_rows(client, target, today - dt.timedelta(days=WINDOW_DAYS + 14))
        except SupabaseError as exc:
            if exc.status == 404:
                log.warning("v_forecast_accuracy missing: run the latest supabase/schema.sql", extra={"target": target})
                return 0
            raise
    tested = prod.get("metrics") or {}
    live = live_accuracy(rows, today)
    alerts = evaluate_alerts(settings, live, tested)
    log.info("live accuracy", extra={"target": target, "live": live, "alerts": alerts})
    _summary(target, live, tested, alerts)
    if alerts:
        for a in alerts:
            print(f"::error::{target}: {a}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
