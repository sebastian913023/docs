"""Deterministic SYNTHETIC 1-minute bar fixtures. They are test inputs, not market evidence."""
from __future__ import annotations

import numpy as np
import pandas as pd

START = "2026-01-05"


def bars(n: int = 60 * 24 * 12, seed: int = 7, planted: float = 0.0, with_volume: bool = True,
         start: str = START) -> pd.DataFrame:
    """Random walk; `planted` > 0 adds a drift that follows 5-minute momentum, so models can learn it.
    planted=0.0 is a pure random walk on which every horizon should abstain."""
    rng = np.random.default_rng(seed)
    vol = 0.0008
    eps = rng.standard_normal(n)
    ret = np.empty(n)
    for i in range(n):
        drift = planted * vol * np.tanh(ret[max(0, i - 5):i].sum() / (vol * np.sqrt(5))) if i >= 5 else 0.0
        ret[i] = drift + vol * eps[i]
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[100.0], close[:-1]])
    wick = np.abs(rng.standard_normal(n)) * vol * close * 0.3
    df = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick,
                       "low": np.minimum(open_, close) - wick, "close": close},
                      index=pd.date_range(start, periods=n, freq="1min", tz="UTC"))
    df["volume"] = np.exp(rng.standard_normal(n) * 0.4) * 50 if with_volume else np.nan
    return df
