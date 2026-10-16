"""Synthetic 1m bars for tests: stochastic-vol random walk with an optional planted predictable drift."""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_bars(n_minutes: int = 60 * 24 * 60, seed: int = 1, signal_strength: float = 0.0) -> pd.DataFrame:
    """signal_strength > 0 plants a weak 15m-lagged-momentum drift (the engine should find it);
    0.0 is a pure random walk (the engine must NOT claim an edge)."""
    rng = np.random.default_rng(seed)
    vol = np.empty(n_minutes)
    lv = np.log(0.0006)
    for i in range(n_minutes):
        lv += 0.02 * (np.log(0.0006) - lv) + 0.05 * rng.standard_normal()
        vol[i] = np.exp(lv)
    ret = vol * rng.standard_normal(n_minutes)
    if signal_strength:
        for i in range(30, n_minutes):
            ret[i] += signal_strength * vol[i] * np.sign(ret[i - 15:i].sum())
    close = 60000 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[60000], close[:-1]])
    spread = np.abs(rng.standard_normal(n_minutes)) * vol * close * 0.5
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volm = np.exp(rng.standard_normal(n_minutes) * 0.5) * 10
    idx = pd.date_range("2026-01-01", periods=n_minutes, freq="1min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volm}, index=idx)
