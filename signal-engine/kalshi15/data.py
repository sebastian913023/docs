"""Spot-market data: public Coinbase 1-minute candles (proxy for the CF Benchmarks BRTI index)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from .config import ASSETS, Config

COLS = ["open", "high", "low", "close", "volume"]


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def fetch_candles(asset: str, start, end, cfg: Config = Config(), pause: float = 0.35) -> pd.DataFrame:
    """Download 1m candles [start, end) from Coinbase (300 per call). Index = minute start (UTC)."""
    product = ASSETS[asset]["product"]
    start, end = _utc(start), _utc(end)
    rows: list[list[float]] = []
    cur = start
    step = timedelta(minutes=300)
    s = requests.Session()
    while cur < end:
        nxt = min(cur + step, end)
        r = s.get(f"{cfg.coinbase_base}/products/{product}/candles",
                  params={"granularity": 60, "start": cur.isoformat(), "end": nxt.isoformat()},
                  timeout=20)
        r.raise_for_status()
        rows.extend(r.json())  # [time, low, high, open, close, volume]
        cur = nxt
        time.sleep(pause)
    return normalize(pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"]))


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Return a clean 1m frame indexed by UTC minute start with COLS columns."""
    df = df.copy()
    if "time" in df.columns:
        t = df["time"]
        idx = pd.to_datetime(t, unit="s", utc=True) if pd.api.types.is_numeric_dtype(t) else pd.to_datetime(t, utc=True)
        df.index = idx
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC") if df.index.tz else pd.DatetimeIndex(df.index).tz_localize("UTC")
    df = df[COLS].astype(float).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = "time"
    return df


def load_csv(path: str) -> pd.DataFrame:
    return normalize(pd.read_csv(path))


def recent_candles(asset: str, minutes: int = 420, cfg: Config = Config()) -> pd.DataFrame:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return fetch_candles(asset, end - timedelta(minutes=minutes), end + timedelta(minutes=1), cfg, pause=0.2)
