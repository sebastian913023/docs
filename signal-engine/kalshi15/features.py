"""Feature engineering. Every feature at decision time t uses only bars that closed at or before t.

Window convention (matches Kalshi KX*15M): a window opens at T0 (minute % 15 == 0) and settles at
T0+15. YES wins if the settlement reference >= the opening reference (ties go to YES). Both
references are proxied by the close of the last 1m bar before the boundary.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

WINDOW = 15
FEATURES = ["z", "mom5", "mom15", "mom60", "rsi", "flow5", "volratio", "prev_win",
            "hour_sin", "hour_cos", "tau_frac"]


def bar_features(bars: pd.DataFrame, vol_span: int = 60) -> pd.DataFrame:
    """Per-bar features (rows align with `bars`)."""
    c, h, l, v = bars["close"], bars["high"], bars["low"], bars["volume"]
    logc = np.log(c)
    r = logc.diff()
    sig = r.ewm(span=vol_span, min_periods=vol_span).std()           # per-minute log-return vol
    out = pd.DataFrame(index=bars.index)
    out["logc"] = logc
    out["sig"] = sig
    for k in (5, 15, 60):                                              # vol-scaled momentum
        out[f"mom{k}"] = (logc - logc.shift(k)) / (sig * np.sqrt(k))
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out["rsi"] = ((rsi - 50) / 50).fillna(0.0)
    rng = (h - l).replace(0, np.nan)
    clv = (2 * (c - l) / rng - 1).fillna(0.0)                          # close location value in [-1, 1]
    out["flow5"] = (clv * v).rolling(5).sum() / (v.rolling(60).mean() * 5).replace(0, np.nan)
    out["volratio"] = (sig / r.rolling(240).std()).clip(0, 5)
    hr = bars.index.hour + bars.index.minute / 60
    out["hour_sin"] = np.sin(2 * np.pi * hr / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hr / 24)
    return out


def assemble(bf: pd.DataFrame, pos0: np.ndarray, m: int) -> pd.DataFrame:
    """Window-level feature rows. pos0 = integer position of each window-open bar (T0).
    The reference is bar pos0-1 (ends at T0); the decision price is bar pos0+m-1 (ends at T0+m)."""
    p = pos0 + m - 1
    logc = bf["logc"].to_numpy()
    sig = bf["sig"].to_numpy()[p]
    tau = WINDOW - m
    dist = logc[p] - logc[pos0 - 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.clip(dist / (sig * np.sqrt(tau)), -6, 6)
        prev = (logc[pos0 - 1] - logc[pos0 - 1 - WINDOW]) / (sig * np.sqrt(WINDOW))
    X = pd.DataFrame({"z": z, "prev_win": prev, "tau_frac": tau / WINDOW}, index=bf.index[pos0])
    for col in ("mom5", "mom15", "mom60", "rsi", "flow5", "volratio", "hour_sin", "hour_cos"):
        X[col] = bf[col].to_numpy()[p]
    X["p0"] = norm.cdf(X["z"])                                          # random-walk fair probability
    X["price_ref"] = np.exp(logc[pos0 - 1])
    X["price_now"] = np.exp(logc[p])
    return X


def window_table(bars: pd.DataFrame, m: int, vol_span: int = 60) -> pd.DataFrame:
    """Build the training/backtest table for decision minute `m` (1..14) with labels."""
    assert 1 <= m <= WINDOW - 1
    bf = bar_features(bars, vol_span)
    idx = bars.index
    is_open = (idx.minute % WINDOW == 0)
    pos0 = np.flatnonzero(is_open)
    pos0 = pos0[(pos0 >= 260) & (pos0 + WINDOW - 1 < len(idx))]
    # Require gap-free data from the previous window through settlement.
    need = pd.Timedelta(minutes=2 * WINDOW)  # bars pos0-16 .. pos0+14 must be contiguous
    ok = (idx[pos0 + WINDOW - 1] - idx[pos0 - 1 - WINDOW]) == need
    pos0 = pos0[ok]
    X = assemble(bf, pos0, m)
    close = bars["close"].to_numpy()
    X["y"] = (close[pos0 + WINDOW - 1] >= close[pos0 - 1]).astype(int)
    X["m"] = m
    return X.dropna(subset=FEATURES + ["y"])
