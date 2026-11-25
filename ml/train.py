"""Train -> evaluate -> gate -> (upload, register, promote).

    python -m ml.train                         # full pipeline against Supabase
    python -m ml.train --no-upload             # train/evaluate locally only
    python -m ml.train --promote never         # register as candidate, do not promote
    python -m ml.train --as-of 2026-10-01      # reproduce a past training run

Time-based protocol (never random splits):

    |------------- train -------------|-- es --|====== holdout ======|
                                      ^        ^                     ^
                         early-stopping window  val_start          cutoff (yesterday)

  1. Fit on `train`, early-stop on `es`            -> best number of trees
  2. Refit on train+es with that many trees        -> holdout model
  3. Rolling-origin backtest over the holdout: at every origin (each
     BACKTEST_STEP_DAYS) forecast FORECAST_HORIZON days recursively using only
     data before the origin — exactly what production does.
  4. Same backtest for a seasonal-naive baseline.
  5. Gate, then refit on ALL data up to the cutoff for the shipped model.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ml.config import Settings, get_settings
from ml.data_loader import SERIES_KEYS, DataError, check_freshness, load_daily_sales
from ml.features import CATEGORICAL_FEATURES, FeatureConfig, add_features, build_panel, make_xy
from ml.forecasting import recursive_forecast, rolling_origin_backtest, seasonal_naive_forecast
from ml.logging_utils import configure_logging
from ml.metrics import metrics_by, regression_metrics
from ml.model import DEFAULT_LGBM_PARAMS, fit_fixed, fit_with_early_stopping
from ml.registry import ModelBundle, ModelRegistry, save_bundle, utc_now
from ml.supabase_client import client_from_settings

log = logging.getLogger("ml.train")


@dataclass
class GateDecision:
    promote: bool
    reason: str


@dataclass
class TrainResult:
    model_version: str
    artifact_dir: Path
    metrics: dict[str, Any]
    baseline_metrics: dict[str, Any]
    decision: GateDecision
    promoted: bool


def git_sha() -> str:
    sha = os.getenv("GITHUB_SHA")
    if sha:
        return sha
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "local"


def evaluate_gate(
    settings: Settings,
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    production: dict[str, Any] | None,
) -> GateDecision:
    """Promotion policy (WAPE, lower is better).

    Production is compared on its *recorded* holdout WAPE rather than re-scored on
    today's holdout: the production model was trained on data that overlaps
    today's holdout, which would bias a head-to-head comparison in its favour.
    """
    wape, base = metrics["wape"], baseline["wape"]
    if not pd.notna(wape):
        return GateDecision(False, "candidate WAPE is undefined (holdout has no sales)")
    need = base * (1 - settings.min_improvement_vs_baseline)
    if not wape < need:
        return GateDecision(False, f"WAPE {wape:.4f} does not beat seasonal-naive {base:.4f} "
                                   f"by {settings.min_improvement_vs_baseline:.0%} (needs < {need:.4f})")
    if settings.max_wape is not None and wape > settings.max_wape:
        return GateDecision(False, f"WAPE {wape:.4f} exceeds MAX_WAPE {settings.max_wape:.4f}")
    if production:
        prod_wape = float(production.get("metrics", {}).get("wape", float("nan")))
        limit = prod_wape * (1 + settings.max_regression_vs_production)
        if pd.notna(prod_wape) and wape > limit:
            return GateDecision(False, f"WAPE {wape:.4f} is worse than production {production['model_version']} "
                                       f"({prod_wape:.4f}) by more than {settings.max_regression_vs_production:.0%}")
        return GateDecision(True, f"WAPE {wape:.4f} vs baseline {base:.4f}, production {prod_wape:.4f}")
    return GateDecision(True, f"WAPE {wape:.4f} vs baseline {base:.4f}; no production model yet")


def weekly_metrics(bt: pd.DataFrame) -> dict[str, float] | None:
    """Accuracy of 7-day TOTALS per series: for every backtest origin, sum the first
    7 forecast days and compare with the actual 7-day total (what weekly planning uses)."""
    wk = bt.loc[bt["step"] <= 7]
    sums = wk.groupby(["origin", *SERIES_KEYS], observed=True).agg(
        y_true=("y_true", "sum"), y_pred=("y_pred", "sum"), days=("step", "size"))
    sums = sums.loc[sums["days"] == 7]
    if sums.empty:
        return None
    return regression_metrics(sums["y_true"], sums["y_pred"])


def _horizon_bucket(step: pd.Series) -> pd.Series:
    return ((step - 1) // 7 * 7 + 1).astype(int).astype(str) + "-" + ((step - 1) // 7 * 7 + 7).astype(int).astype(str)


def run_training(
    settings: Settings,
    *,
    as_of: dt.date | None = None,
    output_dir: Path = Path("artifacts"),
    promote: str = "auto",
    upload: bool = True,
) -> TrainResult:
    t0 = time.monotonic()
    target = settings.target_metric
    cutoff = settings.cutoff_date(as_of)
    cfg = FeatureConfig(target=target, holiday_country=settings.holiday_country)
    start = (cutoff - dt.timedelta(days=settings.training_lookback_days)) if settings.training_lookback_days else None

    client = client_from_settings(settings) if (upload or settings.data_source == "supabase") else None
    try:
        # ---------------- data ----------------
        daily = load_daily_sales(settings, start=start, end=cutoff, client=client)
        newest = check_freshness(daily, cutoff, settings.stale_data_days)
        panel = build_panel(daily, target, cutoff)
        n_days = int(panel["sale_date"].nunique())
        min_days = 2 * settings.validation_days + cfg.lookback_days() + 28
        if n_days < min_days:
            raise DataError(f"Need at least {min_days} days of history to train/validate, have {n_days}.")

        val_start = pd.Timestamp(cutoff) - pd.Timedelta(days=settings.validation_days - 1)
        es_start = val_start - pd.Timedelta(days=settings.validation_days)
        feats = add_features(panel, cfg)
        feats = feats.loc[feats["lag_1"].notna()]  # first day of each series has no history at all

        pre = feats.loc[feats["sale_date"] < val_start]
        X_tr, y_tr = make_xy(pre.loc[pre["sale_date"] < es_start], cfg)
        X_es, y_es = make_xy(pre.loc[pre["sale_date"] >= es_start], cfg)
        log.info("training", extra={"cutoff": str(cutoff), "newest_sale": str(newest), "panel_rows": len(panel),
                                    "series": int(panel.groupby(SERIES_KEYS).ngroups), "train_rows": len(X_tr),
                                    "es_rows": len(X_es), "val_start": str(val_start.date())})

        # ---------------- fit + early stopping ----------------
        _, best_iter = fit_with_early_stopping(X_tr, y_tr, X_es, y_es, CATEGORICAL_FEATURES, seed=settings.random_seed)
        X_pre, y_pre = make_xy(pre, cfg)
        holdout_model = fit_fixed(X_pre, y_pre, CATEGORICAL_FEATURES, best_iter, seed=settings.random_seed)

        # ---------------- backtests ----------------
        def model_fn(hist: pd.DataFrame, origin: pd.Timestamp, h: int) -> pd.DataFrame:
            return recursive_forecast(holdout_model, cfg, hist, origin, h)

        def naive_fn(hist: pd.DataFrame, origin: pd.Timestamp, h: int) -> pd.DataFrame:
            return seasonal_naive_forecast(hist, origin, h)

        bt = rolling_origin_backtest(panel, model_fn, val_start, cutoff, settings.forecast_horizon,
                                     settings.backtest_step_days)
        bt_base = rolling_origin_backtest(panel, naive_fn, val_start, cutoff, settings.forecast_horizon,
                                          settings.backtest_step_days)
        metrics = regression_metrics(bt["y_true"], bt["y_pred"])
        baseline = regression_metrics(bt_base["y_true"], bt_base["y_pred"])
        metrics["by_horizon"] = metrics_by(bt.assign(h=_horizon_bucket(bt["step"])), "h")
        metrics["by_category"] = metrics_by(bt, "category")
        metrics["best_iteration"] = best_iter
        weekly, weekly_base = weekly_metrics(bt), weekly_metrics(bt_base)
        if weekly:
            metrics["weekly"] = weekly
        if weekly_base:
            baseline["weekly"] = weekly_base

        # ---------------- gate ----------------
        registry = ModelRegistry(client, settings.model_bucket) if (upload and client) else None
        production = registry.production(target) if registry else None
        if promote == "force":
            decision = GateDecision(True, "forced promotion (--promote force)")
        elif promote == "never":
            decision = GateDecision(False, "promotion disabled (--promote never)")
        else:
            decision = evaluate_gate(settings, metrics, baseline, production)

        # ---------------- final model on all data ----------------
        X_all, y_all = make_xy(feats, cfg)
        final = fit_fixed(X_all, y_all, CATEGORICAL_FEATURES, best_iter, seed=settings.random_seed)
        trained_at = utc_now()
        sha = git_sha()
        version = f"{target}-{trained_at:%Y%m%dT%H%M%SZ}-{sha[:7]}"
        importances = dict(sorted(zip(cfg.feature_columns(),
                                      final.named_steps["model"].booster_.feature_importance("gain").round(1).tolist(),
                                      strict=True), key=lambda kv: -kv[1])[:15])
        bundle = ModelBundle(
            pipeline=final,
            feature_config=cfg.to_dict(),
            feature_columns=cfg.feature_columns(),
            target_metric=target,
            model_version=version,
            trained_at=trained_at.isoformat(),
            data_start=str(panel["sale_date"].min().date()),
            data_end=str(cutoff),
            known_series=panel[SERIES_KEYS].drop_duplicates().values.tolist(),
            metrics=metrics,
        )
        out = output_dir / version
        extra = {"baseline_metrics": baseline, "decision": decision.reason, "promote_candidate": decision.promote,
                 "params": {**DEFAULT_LGBM_PARAMS, "n_estimators": best_iter}, "feature_importance_gain": importances,
                 "train_rows": len(X_all), "git_sha": sha, "settings": {
                     "forecast_horizon": settings.forecast_horizon, "validation_days": settings.validation_days,
                     "backtest_step_days": settings.backtest_step_days, "holiday_country": settings.holiday_country}}
        model_path, digest = save_bundle(bundle, out, extra)
        latest = output_dir / "latest"
        shutil.rmtree(latest, ignore_errors=True)
        shutil.copytree(out, latest)

        # ---------------- register / promote ----------------
        promoted = False
        if registry:
            artifact_path = registry.upload(out, target, version)
            registry.register({
                "model_version": version, "target_metric": target,
                "status": "candidate" if decision.promote or promote == "never" else "rejected",
                "artifact_path": artifact_path, "artifact_sha256": digest,
                "metrics": _jsonable(metrics), "baseline_metrics": _jsonable(baseline),
                "params": extra["params"], "feature_columns": cfg.feature_columns(),
                "train_rows": len(X_all), "n_series": len(bundle.known_series),
                "data_start": bundle.data_start, "data_end": bundle.data_end,
                "git_sha": sha, "decision_reason": decision.reason, "trained_at": trained_at.isoformat(),
            })
            if decision.promote:
                registry.promote(version)
                promoted = True

        result = TrainResult(version, out, metrics, baseline, decision, promoted)
        log.info("training finished", extra={
            "model_version": version, "wape": round(metrics["wape"], 4), "baseline_wape": round(baseline["wape"], 4),
            "mae": round(metrics["mae"], 2), "rmse": round(metrics["rmse"], 2), "mape": round(metrics["mape"], 4),
            "weekly_wape": round(metrics["weekly"]["wape"], 4) if "weekly" in metrics else None,
            "target": target,
            "promote_candidate": decision.promote, "promoted": promoted, "reason": decision.reason,
            "artifact": str(model_path), "duration_s": round(time.monotonic() - t0, 1)})
        _write_github_outputs(result)
        return result
    finally:
        if client:
            client.close()


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(d, default=str).replace("NaN", "null"))


def _write_github_outputs(r: TrainResult) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    lines = [f"model_version={r.model_version}", f"promoted={str(r.promoted).lower()}",
             f"wape={r.metrics['wape']:.4f}", f"baseline_wape={r.baseline_metrics['wape']:.4f}"]
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        m, b = r.metrics, r.baseline_metrics
        with open(summary, "a") as f:
            f.write(f"## Training run `{r.model_version}`\n\n| metric | model | seasonal naive |\n|---|---|---|\n")
            for k in ("wape", "mae", "rmse", "mape", "bias"):
                f.write(f"| {k} | {m[k]:.4f} | {b[k]:.4f} |\n")
            if "weekly" in m and "weekly" in b:
                f.write(f"| weekly wape (7-day totals) | {m['weekly']['wape']:.4f} | {b['weekly']['wape']:.4f} |\n")
            f.write(f"\n**Promoted:** {r.promoted} — {r.decision.reason}\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as-of", type=dt.date.fromisoformat, help="pretend today is this date (YYYY-MM-DD)")
    p.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    p.add_argument("--promote", choices=["auto", "never", "force"], default="auto")
    p.add_argument("--no-upload", action="store_true", help="do not touch Supabase Storage / model_registry")
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        run_training(settings, as_of=args.as_of, output_dir=args.output_dir,
                     promote=args.promote, upload=not args.no_upload)
    except DataError as exc:
        log.error("training aborted: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
