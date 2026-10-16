"""Kalshi fee and payoff math (per 1 contract paying $1)."""
from __future__ import annotations

import math

import numpy as np


def taker_fee(price: float, rate: float = 0.07) -> float:
    """Fee in dollars for one contract bought at `price` (0-1): ceil(rate*P*(1-P) to the cent)."""
    return math.ceil(round(rate * price * (1.0 - price) * 100.0, 9)) / 100.0


def taker_fee_vec(price: np.ndarray, rate: float = 0.07) -> np.ndarray:
    return np.ceil(np.round(rate * price * (1.0 - price) * 100.0, 9)) / 100.0


def expected_edge(p_win: float, price: float, rate: float = 0.07) -> float:
    """Expected profit per contract = p_win - price - fee."""
    return p_win - price - taker_fee(price, rate)


def kelly_fraction(p_win: float, price: float, rate: float = 0.07) -> float:
    """Full-Kelly stake fraction for a $1 binary bought at `price` incl. fee. 0 if no edge."""
    cost = price + taker_fee(price, rate)
    if cost >= 1.0:
        return 0.0
    b = (1.0 - cost) / cost  # net odds
    f = (p_win * b - (1.0 - p_win)) / b
    return max(0.0, f)
