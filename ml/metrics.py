"""Forecast evaluation metrics.

MAPE is undefined when actual = 0, which is common for daily category sales, so
it is computed over non-zero actuals only (and the share of excluded points is
reported). WAPE (= sum|err| / sum|actual|) is robust to zeros and is the metric
used by the promotion gate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def regression_metrics(y_true: np.ndarray | pd.Series, y_pred: np.ndarray | pd.Series) -> dict[str, float]:
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    if yt.shape != yp.shape:
        raise ValueError(f"shape mismatch {yt.shape} vs {yp.shape}")
    if yt.size == 0:
        raise ValueError("cannot compute metrics on empty arrays")
    err = yp - yt
    abs_err = np.abs(err)
    nz = yt != 0
    denom = np.abs(yt).sum()
    return {
        "mae": float(abs_err.mean()),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "mape": float(np.mean(abs_err[nz] / np.abs(yt[nz]))) if nz.any() else float("nan"),
        "wape": float(abs_err.sum() / denom) if denom > 0 else float("nan"),
        "bias": float(err.sum() / denom) if denom > 0 else float("nan"),  # + = over-forecast
        "n": int(yt.size),
        "mape_excluded_zero_share": float(1 - nz.mean()),
    }


def metrics_by(
    df: pd.DataFrame, by: str, true_col: str = "y_true", pred_col: str = "y_pred"
) -> dict[str, dict[str, float]]:
    return {str(k): regression_metrics(g[true_col], g[pred_col]) for k, g in df.groupby(by, observed=True)}
