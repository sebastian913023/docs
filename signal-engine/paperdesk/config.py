"""Configuration and enums.

The numeric defaults are conservative research starting points, not proven or optimal parameters.

Denominators (all in virtual dollars):
- available_cash           = cash not reserved for open contracts.
- reserved_stake           = sum of stakes of open (unsettled) contracts.
- settled_equity           = available_cash + reserved_stake. Open binary contracts are carried at
                             cost; no unrealized mark is added because the contracts cannot be sold.
- session_start_equity     = settled_equity at the first event of the UTC trading day.
- worst_case_open_loss     = reserved_stake (a binary contract can lose its whole stake).

Rules that use them:
- stake                    = stake_fraction_of_available_equity * available_cash.
- open-stake cap           = maximum_total_open_stake_fraction * settled_equity.
- correlated-group cap     = maximum_correlated_group_stake_fraction * settled_equity.
- daily loss stop          = realized_today <= -daily_loss_halt_fraction * session_start_equity.
  Pre-trade: -realized_today + reserved_stake + proposed_stake must not exceed that limit.
- drawdown stop            = (high_water_mark - settled_equity) >= peak_to_trough_fraction * HWM.
  Pre-trade: (HWM - settled_equity) + reserved_stake + proposed_stake must not exceed that limit.
- daily profit pause       = realized_today >= daily_profit_pause_fraction * session_start_equity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DataMode(str, Enum):
    LOCAL_PAPER = "LOCAL_PAPER"        # local simulation on an authorized live data feed
    PLATFORM_DEMO = "PLATFORM_DEMO"    # verified official demo integration (none exists here)
    REPLAY = "REPLAY"                  # historical simulation


class Action(str, Enum):
    CALL = "CALL"
    PUT = "PUT"
    ABSTAIN = "ABSTAIN"


class ValidationStatus(str, Enum):
    UNVALIDATED = "UNVALIDATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    REJECTED = "REJECTED"
    HISTORICALLY_VALIDATED = "HISTORICALLY_VALIDATED"
    FORWARD_PAPER_VALIDATED = "FORWARD_PAPER_VALIDATED"


USABLE_VALIDATION = {ValidationStatus.HISTORICALLY_VALIDATED, ValidationStatus.FORWARD_PAPER_VALIDATED}


class DataQuality(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INVALID = "INVALID"


class QuantumStatus(str, Enum):
    DISABLED = "DISABLED"
    SIMULATOR = "SIMULATOR"
    AUTHENTICATED = "AUTHENTICATED"
    BACKEND_VERIFIED = "BACKEND_VERIFIED"
    JOB_SUBMITTED = "JOB_SUBMITTED"
    JOB_COMPLETED = "JOB_COMPLETED"
    FAILED = "FAILED"


class TieRule(str, Enum):
    LOSS = "LOSS"        # conservative default; match it to the real contract terms
    REFUND = "REFUND"


ALERT_LABEL = "ANALYTICAL ALERT — NOT AN EXECUTABLE CONTRACT EXIT"
HORIZONS = (3, 5, 15)


@dataclass(frozen=True)
class RiskConfig:
    initial_virtual_balance: float = 10_000.0
    stake_fraction_of_available_equity: float = 0.0025
    maximum_total_open_stake_fraction: float = 0.01
    maximum_simultaneous_contracts: int = 3
    daily_loss_halt_fraction: float = 0.02
    peak_to_trough_drawdown_halt_fraction: float = 0.05
    daily_profit_pause_fraction: float = 0.02
    minimum_calibrated_win_probability: float = 0.75   # model gate, not a promised win rate
    live_order_execution_enabled: bool = False
    qpu_submission_enabled: bool = False
    # Added by this build (not in the brief); set equal to the total cap so it never loosens it.
    maximum_correlated_group_stake_fraction: float = 0.01
    minimum_expected_net_payoff_per_dollar: float = 0.0
    # Latching: these halts need an authorized reset. The profit pause clears at the next UTC day.
    require_authorized_reset: bool = True
    data_quality_halt_after: int = 5          # consecutive STALE/INVALID evaluations
    drift_brier_margin: float = 0.05          # forward Brier worse than validation Brier by this
    drift_min_samples: int = 50
    tie_rule: TieRule = TieRule.LOSS
    max_quote_age_s: float = 2.0              # payout/exit quotes older than this are stale
    correlation_groups: dict = field(default_factory=lambda: {
        "BTC-USD": "crypto_major", "ETH-USD": "crypto_major", "SOL-USD": "crypto_alt"})

    def __post_init__(self):
        for name in ("stake_fraction_of_available_equity", "maximum_total_open_stake_fraction",
                     "daily_loss_halt_fraction", "peak_to_trough_drawdown_halt_fraction",
                     "daily_profit_pause_fraction", "maximum_correlated_group_stake_fraction"):
            v = getattr(self, name)
            if not 0 < v < 1:
                raise ValueError(f"{name} must be in (0, 1), got {v}")
        if not 0.5 < self.minimum_calibrated_win_probability < 1:
            raise ValueError("minimum_calibrated_win_probability must be in (0.5, 1)")
