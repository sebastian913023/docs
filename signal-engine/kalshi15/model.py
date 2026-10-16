"""Walk-forward probability model: logistic regression on a structural random-walk feature set."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .features import FEATURES


def make_model(C: float = 0.05):
    return make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=500))


def fit(table: pd.DataFrame, C: float = 0.05):
    mdl = make_model(C)
    mdl.fit(table[FEATURES].to_numpy(), table["y"].to_numpy())
    return mdl


def walk_forward(table: pd.DataFrame, min_train: int = 2000, retrain_every: int = 96,
                 purge: int = 1, C: float = 0.05) -> pd.Series:
    """Out-of-sample P(YES) for every row after the first `min_train` windows.
    Training uses only rows strictly earlier than (test block start - purge windows)."""
    n = len(table)
    p = np.full(n, np.nan)
    X = table[FEATURES].to_numpy()
    y = table["y"].to_numpy()
    start = min_train
    while start < n:
        end = min(start + retrain_every, n)
        tr_end = start - purge
        if y[:tr_end].min() != y[:tr_end].max():
            mdl = make_model(C).fit(X[:tr_end], y[:tr_end])
            p[start:end] = mdl.predict_proba(X[start:end])[:, 1]
        start = end
    return pd.Series(p, index=table.index, name="p_model")
