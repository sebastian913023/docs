"""Turn a model probability and market prices into an UP / DOWN / NO-TRADE decision."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import Config
from .fees import expected_edge, kelly_fraction


@dataclass
class Signal:
    asset: str
    window_open: str
    minute: int
    action: str            # "UP" | "DOWN" | "NO-TRADE" | "WAIT"
    p_up: float
    yes_ask: float | None
    no_ask: float | None
    edge: float | None     # expected $ profit per contract after fee, chosen side
    stake_frac: float      # suggested fraction of bankroll
    reason: str
    ticker: str | None = None

    def dict(self) -> dict:
        return asdict(self)


def decide(asset: str, window_open: str, minute: int, p_up: float, yes_ask: float | None,
           no_ask: float | None, cfg: Config = Config(), ticker: str | None = None) -> Signal:
    base = dict(asset=asset, window_open=window_open, minute=minute, p_up=round(p_up, 4),
                yes_ask=yes_ask, no_ask=no_ask, ticker=ticker)
    if not (cfg.min_decision_min <= minute <= cfg.max_decision_min):
        return Signal(**base, action="WAIT", edge=None, stake_frac=0.0,
                      reason=f"outside decision range {cfg.min_decision_min}-{cfg.max_decision_min} min")
    if yes_ask is None or no_ask is None:
        return Signal(**base, action="NO-TRADE", edge=None, stake_frac=0.0,
                      reason="no Kalshi quote available; cannot compute edge")
    e_up = expected_edge(p_up, yes_ask, cfg.fee_rate)
    e_dn = expected_edge(1 - p_up, no_ask, cfg.fee_rate)
    if abs(p_up - 0.5) < cfg.min_confidence:
        return Signal(**base, action="NO-TRADE", edge=max(e_up, e_dn), stake_frac=0.0, reason="low confidence")
    if e_up >= cfg.min_edge and e_up >= e_dn:
        f = min(cfg.max_stake_frac, cfg.kelly_mult * kelly_fraction(p_up, yes_ask, cfg.fee_rate))
        return Signal(**base, action="UP", edge=round(e_up, 4), stake_frac=round(f, 4), reason="edge on YES")
    if e_dn >= cfg.min_edge:
        f = min(cfg.max_stake_frac, cfg.kelly_mult * kelly_fraction(1 - p_up, no_ask, cfg.fee_rate))
        return Signal(**base, action="DOWN", edge=round(e_dn, 4), stake_frac=round(f, 4), reason="edge on NO")
    return Signal(**base, action="NO-TRADE", edge=round(max(e_up, e_dn), 4), stake_frac=0.0,
                  reason=f"best edge below {cfg.min_edge}")
