"""Contract-level exits and nonexecutable reference alerts.

Early exit is OFF unless every capability below has been verified against the real contract terms
and the authorized interface. A price threshold alone never creates an executable exit. A failed
exit leaves the contract open and booked P&L unchanged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from .config import ALERT_LABEL, Action
from .ledger import Account, Contract


@dataclass(frozen=True)
class ExitCapability:
    contract_permits_early_closure: bool = False
    interface_exposes_close: bool = False
    executable_exit_value_available: bool = False
    exit_costs_and_timing_known: bool = False
    historical_replay_supports_exits: bool = False
    exit_cost_per_contract: float = 0.0
    exit_latency_s: float = 0.0
    evidence: str = "none: no platform contract terms or interface verified"

    @property
    def early_exit_supported(self) -> bool:
        return all((self.contract_permits_early_closure, self.interface_exposes_close,
                    self.executable_exit_value_available, self.exit_costs_and_timing_known,
                    self.historical_replay_supports_exits))

    def dict(self) -> dict:
        d = asdict(self)
        d["early_exit_supported"] = self.early_exit_supported
        return d


UNSUPPORTED = ExitCapability()


@dataclass(frozen=True)
class ExitQuote:
    """An executable bid for the contract itself (not the underlying)."""
    recoverable_value: float     # dollars returned if the close fills, before exit costs
    quote_time: datetime


@dataclass(frozen=True)
class ExitResult:
    accepted: bool
    reason: str
    booked_pnl: float | None = None


def break_even_reached(c: Contract, q: ExitQuote, cap: ExitCapability) -> bool:
    """Break-even for supported exits = recoverable value after costs >= stake (not underlying == strike)."""
    return q.recoverable_value - cap.exit_cost_per_contract >= c.stake


def request_exit(acct: Account, c: Contract, q: ExitQuote | None, cap: ExitCapability, now: datetime,
                 max_quote_age_s: float, venue_rejects: bool = False) -> ExitResult:
    acct.log("EXIT_REQUEST", now, {"contract_id": c.contract_id, "supported": cap.early_exit_supported})

    def reject(reason: str) -> ExitResult:
        acct.log("EXIT_REJECTED", now, {"contract_id": c.contract_id, "reason": reason, "contract_status": c.status})
        return ExitResult(False, reason)

    if c.status != "OPEN":
        return reject(f"contract is {c.status}")
    if not cap.early_exit_supported:
        return reject("EARLY_EXIT_UNSUPPORTED")
    if q is None:
        return reject("NO_EXECUTABLE_EXIT_VALUE")
    fill_time_age = (now - q.quote_time).total_seconds() + cap.exit_latency_s
    if fill_time_age > max_quote_age_s:
        return reject(f"STALE_QUOTE ({fill_time_age:.1f}s incl. latency > {max_quote_age_s}s)")
    if venue_rejects:
        return reject("VENUE_REJECTED")
    value = max(0.0, q.recoverable_value - cap.exit_cost_per_contract)
    acct.book_executed_exit(c.contract_id, value, now, {"recoverable_value": q.recoverable_value,
                                                        "exit_cost": cap.exit_cost_per_contract})
    return ExitResult(True, "FILLED", value - c.stake)


@dataclass
class ReferenceAlerts:
    """Visual underlying-price levels. Crossing one changes nothing in the booked ledger."""
    contract_id: str
    direction: Action
    reference_stop: float
    reference_target: float
    break_even_marker: float
    label: str = ALERT_LABEL
    executable: bool = False
    crossed: tuple = ()

    @classmethod
    def build(cls, c: Contract, scale: float) -> "ReferenceAlerts":
        s = 1 if c.direction == Action.CALL else -1
        return cls(c.contract_id, c.direction, reference_stop=c.strike - s * scale,
                   reference_target=c.strike + s * scale, break_even_marker=c.strike)

    def check(self, acct: Account, price: float, now: datetime) -> list[str]:
        s = 1 if self.direction == Action.CALL else -1
        hits = []
        for name, level in (("reference_stop", self.reference_stop), ("reference_target", self.reference_target)):
            if name in self.crossed:
                continue
            crossed = (price - level) * s <= 0 if name == "reference_stop" else (price - level) * s >= 0
            if crossed:
                hits.append(name)
                acct.log("ALERT", now, {"contract_id": self.contract_id, "alert": name, "level": level,
                                        "price": price, "label": self.label, "booked_pnl_effect": 0.0})
        self.crossed = self.crossed + tuple(hits)
        return hits

    def dict(self) -> dict:
        d = asdict(self)
        d["direction"] = self.direction.value
        return d
