"""Indicators, signal-strength score and data-quality checks.

signal_strength_0_to_100 is a transparent, NONPROBABILISTIC alignment score. It is not a win
probability and is never used to pass a gate.

Formula (version SS-1.0, all components are clipped to [-1, 1], +1 = bullish):
    rsi_c    = clip((RSI14 - 50) / 25)
    macd_c   = tanh(MACD_hist(12,26,9) / (close * sigma_1m * sqrt(26)))
    fib_c    = clip(2 * (fib_pos - 0.5)),  fib_pos = (close - low60) / (high60 - low60)
    volume_c = sign(ret5) * clip(log(vol5_mean / vol60_mean))
    S        = sum(w_i * c_i) / sum(w_i)       weights rsi .3, macd .3, fib .2, volume .2
    strength = round(100 * |S|),  strength_direction = CALL if S > 0, PUT if S < 0
Variant SS-1.0-NOVOL drops volume_c and renormalizes the remaining weights; it is versioned and
validated as a separate model, never substituted silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from .config import DataQuality

WEIGHTS = {"SS-1.0": {"rsi": 0.3, "macd": 0.3, "fib": 0.2, "volume": 0.2},
           "SS-1.0-NOVOL": {"rsi": 0.3, "macd": 0.3, "fib": 0.2}}
LOOKBACK = 120


def volume_available(bars: pd.DataFrame) -> bool:
    if "volume" not in bars:
        return False
    v = bars["volume"].tail(LOOKBACK)
    return bool(v.notna().all() and (v > 0).mean() > 0.95)


def components(bars: pd.DataFrame) -> pd.DataFrame:
    """Per-bar indicator components using only bars at or before each row."""
    c = bars["close"]
    r = np.log(c).diff()
    sig = r.ewm(span=60, min_periods=30).std()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    hi, lo = bars["high"].rolling(60).max(), bars["low"].rolling(60).min()
    fib_pos = (c - lo) / (hi - lo).replace(0, np.nan)
    out = pd.DataFrame(index=bars.index)
    out["rsi"] = rsi
    out["rsi_c"] = ((rsi - 50) / 25).clip(-1, 1)
    out["macd_hist"] = hist
    out["macd_c"] = np.tanh(hist / (c * sig * np.sqrt(26)))
    out["fib_pos"] = fib_pos
    out["fib_c"] = (2 * (fib_pos - 0.5)).clip(-1, 1)
    out["mom5"] = (np.log(c) - np.log(c.shift(5))) / (sig * np.sqrt(5))
    if "volume" in bars and bars["volume"].notna().any():
        v = bars["volume"].astype(float)
        ratio = v.rolling(5).mean() / v.rolling(60).mean().replace(0, np.nan)
        out["volume_c"] = (np.sign(c - c.shift(5)) * np.log(ratio.where(ratio > 0)).clip(-1, 1))
    else:
        out["volume_c"] = np.nan
    return out


@dataclass(frozen=True)
class Strength:
    score: int
    direction: str            # CALL | PUT | NEUTRAL
    version: str
    contributions: dict       # name -> {"component": c, "weight": w, "weighted": w*c/sum_w} or "UNAVAILABLE"

    def dict(self) -> dict:
        return {"signal_strength_0_to_100": self.score, "strength_direction": self.direction,
                "methodology_version": self.version, "is_probability": False,
                "formula": "100*|sum(w_i*c_i)/sum(w_i)|; see paperdesk/indicators.py",
                "contributions": self.contributions}


def strength(row: pd.Series, has_volume: bool) -> Strength:
    version = "SS-1.0" if has_volume else "SS-1.0-NOVOL"
    w = WEIGHTS[version]
    sw = sum(w.values())
    contrib, s = {}, 0.0
    for k, wk in w.items():
        ck = float(row[f"{k}_c"])
        if not np.isfinite(ck):
            ck = 0.0
        contrib[k] = {"component": round(ck, 4), "weight": wk, "weighted": round(wk * ck / sw, 4)}
        s += wk * ck / sw
    if not has_volume:
        contrib["volume"] = "UNAVAILABLE (no actual volume; variant SS-1.0-NOVOL in use)"
    direction = "CALL" if s > 0 else "PUT" if s < 0 else "NEUTRAL"
    return Strength(int(round(100 * min(1.0, abs(s)))), direction, version, contrib)


def data_quality(bars: pd.DataFrame, now: datetime, bar_s: int = 60, stale_after_s: int = 120) -> tuple[DataQuality, list[str]]:
    notes: list[str] = []
    if len(bars) < LOOKBACK:
        return DataQuality.INVALID, [f"only {len(bars)} bars; need {LOOKBACK}"]
    w = bars.tail(LOOKBACK)
    px = w[["open", "high", "low", "close"]]
    if px.isna().any().any() or (px <= 0).any().any() or (w["high"] < w["low"]).any():
        return DataQuality.INVALID, ["NaN, non-positive, or high<low prices in lookback"]
    last_close_time = w.index[-1] + pd.Timedelta(seconds=bar_s)
    age = (pd.Timestamp(now) - last_close_time).total_seconds()
    if age > stale_after_s:
        return DataQuality.STALE, [f"last bar closed {age:.0f}s ago (> {stale_after_s}s)"]
    gaps = int((w.index.to_series().diff().dt.total_seconds() > bar_s).sum())
    if gaps:
        notes.append(f"{gaps} gap(s) in last {LOOKBACK} bars")
    if not volume_available(bars):
        notes.append("actual volume unavailable")
    return (DataQuality.DEGRADED if gaps else DataQuality.OK), notes
