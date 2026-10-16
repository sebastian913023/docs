"""Configuration. Secrets come from environment variables only, never from code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Coinbase product and (unverified: confirm on kalshi.com) Kalshi series ticker per asset.
ASSETS = {
    "BTC": {"product": "BTC-USD", "series": "KXBTC15M"},
    "ETH": {"product": "ETH-USD", "series": "KXETH15M"},
    "SOL": {"product": "SOL-USD", "series": "KXSOL15M"},
    "XRP": {"product": "XRP-USD", "series": "KXXRP15M"},
}


@dataclass(frozen=True)
class Config:
    window_min: int = 15
    # Decision happens m minutes after the window opens; signals are only valid in this range.
    min_decision_min: int = 3
    max_decision_min: int = 10
    # Trading rules
    min_edge: float = 0.04        # required edge per contract after fees, in dollars
    min_confidence: float = 0.0   # optional extra |p-0.5| floor
    kelly_mult: float = 0.25      # fractional Kelly
    max_stake_frac: float = 0.02  # hard cap per signal as fraction of bankroll
    # Fees: Kalshi taker fee = ceil(rate * P * (1-P) * 100)/100 per contract. Verify current schedule.
    fee_rate: float = 0.07
    # Backtest-only assumed full spread when no real Kalshi prices are supplied
    assumed_spread: float = 0.02
    # Model
    vol_span: int = 60
    train_min_windows: int = 2000
    retrain_every: int = 96
    # Endpoints
    kalshi_base: str = field(default_factory=lambda: os.getenv(
        "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"))
    coinbase_base: str = "https://api.exchange.coinbase.com"

    @staticmethod
    def kalshi_key_id() -> str | None:
        return os.getenv("KALSHI_API_KEY_ID")

    @staticmethod
    def kalshi_private_key_path() -> str | None:
        return os.getenv("KALSHI_PRIVATE_KEY_PATH")
