"""Model definition: an sklearn Pipeline = categorical aligner + LightGBM.

The aligner freezes the category levels seen in training so that serving uses
identical integer codes (unseen locations/categories become NaN, which LightGBM
routes like missing values instead of silently mapping them to another level).
"""

from __future__ import annotations

from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import Pipeline

DEFAULT_LGBM_PARAMS: dict[str, Any] = {
    "objective": "tweedie",          # zero-inflated, right-skewed daily sales
    "tweedie_variance_power": 1.2,
    "learning_rate": 0.03,
    "n_estimators": 3000,            # upper bound; early stopping picks the real number
    "num_leaves": 31,
    "min_child_samples": 20,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "verbose": -1,
}


class CategoryAligner(BaseEstimator, TransformerMixin):
    def __init__(self, columns: list[str] | None = None) -> None:
        self.columns = columns

    def fit(self, X: pd.DataFrame, y: Any = None) -> CategoryAligner:
        cols = self.columns or []
        self.categories_: dict[str, list[str]] = {
            c: sorted(X[c].dropna().astype(str).unique().tolist()) for c in cols
        }
        self.feature_names_in_ = np.asarray(X.columns, dtype=object)
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.loc[:, list(self.feature_names_in_)].copy()
        for c, cats in self.categories_.items():
            X[c] = pd.Categorical(X[c].astype(str), categories=cats)
        return X

    def get_feature_names_out(self, input_features: Any = None) -> np.ndarray:
        return self.feature_names_in_


def make_pipeline(categorical: list[str], params: dict[str, Any] | None = None, seed: int = 42) -> Pipeline:
    p = {**DEFAULT_LGBM_PARAMS, **(params or {}), "random_state": seed}
    return Pipeline([("align", CategoryAligner(categorical)), ("model", lgb.LGBMRegressor(**p))])


def fit_with_early_stopping(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_valid: pd.DataFrame,
    y_valid: pd.Series,
    categorical: list[str],
    params: dict[str, Any] | None = None,
    seed: int = 42,
    patience: int = 100,
) -> tuple[Pipeline, int]:
    """Fit the pipeline with early stopping on (X_valid, y_valid); returns the
    fitted pipeline and the best iteration."""
    pipe = make_pipeline(categorical, params, seed)
    align: CategoryAligner = pipe.named_steps["align"]
    align.fit(X_train)
    model: lgb.LGBMRegressor = pipe.named_steps["model"]
    model.fit(
        align.transform(X_train), y_train,
        eval_X=(align.transform(X_valid),),  # lightgbm>=4.7 API (eval_set is deprecated)
        eval_y=(y_valid,),
        eval_metric="l1",
        callbacks=[lgb.early_stopping(patience, first_metric_only=True, verbose=False)],
    )
    best = int(model.best_iteration_ or model.n_estimators)
    return pipe, max(best, 1)


def fit_fixed(
    X: pd.DataFrame, y: pd.Series, categorical: list[str], n_estimators: int,
    params: dict[str, Any] | None = None, seed: int = 42,
) -> Pipeline:
    pipe = make_pipeline(categorical, {**(params or {}), "n_estimators": n_estimators}, seed)
    pipe.fit(X, y)
    return pipe
