"""Minimal Kalshi REST client (read-only). Public market data needs no key; signing is optional.
Auth scheme: RSA-PSS/SHA256 over `timestamp_ms + METHOD + path` (verify against docs.kalshi.com)."""
from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
import requests

from .config import ASSETS, Config


def _sign_headers(method: str, url: str, key_id: str, key_path: str) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    with open(key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    ts = str(int(time.time() * 1000))
    path = urlparse(url).path
    sig = key.sign((ts + method.upper() + path).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def _price(m: dict, field: str) -> float | None:
    """Prices arrive as integer cents or as '*_dollars' strings depending on API version."""
    if m.get(f"{field}_dollars") is not None:
        return float(m[f"{field}_dollars"])
    if m.get(field) is not None:
        return float(m[field]) / 100.0
    return None


def open_markets(asset: str, cfg: Config = Config(), series: str | None = None) -> list[dict]:
    url = f"{cfg.kalshi_base}/markets"
    headers = {}
    kid, kp = cfg.kalshi_key_id(), cfg.kalshi_private_key_path()
    if kid and kp:
        headers = _sign_headers("GET", url, kid, kp)
    r = requests.get(url, params={"series_ticker": series or ASSETS[asset]["series"],
                                  "status": "open", "limit": 20}, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json().get("markets", [])


def current_window_quote(asset: str, window_open: pd.Timestamp, cfg: Config = Config(),
                         series: str | None = None) -> dict | None:
    """Return {'ticker','yes_ask','no_ask'} for the market closing at window_open + 15m."""
    target = window_open + timedelta(minutes=cfg.window_min)
    for m in open_markets(asset, cfg, series):
        close = m.get("close_time") or m.get("expected_expiration_time")
        if not close:
            continue
        if abs((pd.Timestamp(close).tz_convert("UTC") - target).total_seconds()) <= 60:
            ya, na = _price(m, "yes_ask"), _price(m, "no_ask")
            if ya is None or na is None:
                return None
            return {"ticker": m["ticker"], "yes_ask": ya, "no_ask": na}
    return None


def load_market_csv(path: str) -> pd.DataFrame:
    """CSV with columns window_open (UTC), yes_ask, no_ask (dollars, taken at the decision minute)."""
    df = pd.read_csv(path, parse_dates=["window_open"])
    df["window_open"] = pd.to_datetime(df["window_open"], utc=True)
    return df.set_index("window_open")[["yes_ask", "no_ask"]].sort_index()
