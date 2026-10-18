"""Account-level controls. They only prevent NEW entries; open contracts keep running to settlement."""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from datetime import datetime

from .config import DataQuality, RiskConfig
from .ledger import Account

LATCHING = {"DAILY_LOSS", "DRAWDOWN", "KILL_SWITCH", "DATA_QUALITY", "MODEL_DRIFT"}


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str

    def dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


class RiskManager:
    def __init__(self, account: Account, cfg: RiskConfig | None = None):
        self.acct = account
        self.cfg = cfg or account.cfg
        self.halts: dict[str, dict] = {}          # name -> {"since": iso, "reason": str}
        self.profit_paused_day: str | None = None
        self._bad_data_streak = 0
        self.timeline: list[dict] = []

    # ---- halts ------------------------------------------------------------------------------
    def _halt(self, name: str, t: datetime, reason: str) -> None:
        if name in self.halts:
            return
        self.halts[name] = {"since": t.isoformat(), "reason": reason}
        self.acct.log("HALT", t, {"halt": name, "reason": reason})
        self.timeline.append({"time_utc": t.isoformat(), "event": "HALT", "halt": name, "reason": reason})

    def kill_switch(self, t: datetime, reason: str = "manual") -> None:
        self._halt("KILL_SWITCH", t, reason)

    def reset(self, name: str, t: datetime, authorization: str | None) -> bool:
        """Clear a latched halt. With require_authorized_reset, the token must match the
        PAPERDESK_RESET_TOKEN environment variable (never stored in config or logs)."""
        if name not in self.halts:
            return False
        if self.cfg.require_authorized_reset:
            expected = os.getenv("PAPERDESK_RESET_TOKEN")
            if not expected or not authorization or not hmac.compare_digest(expected, authorization):
                self.acct.log("RESET_DENIED", t, {"halt": name})
                return False
        del self.halts[name]
        if name == "DATA_QUALITY":
            self._bad_data_streak = 0
        self.acct.log("RESET", t, {"halt": name})
        self.timeline.append({"time_utc": t.isoformat(), "event": "RESET", "halt": name})
        return True

    def observe(self, t: datetime) -> None:
        """Re-evaluate account-level stops after every booking or session roll."""
        a, c = self.acct, self.cfg
        if a.roll_session(t):
            self.profit_paused_day = None
        if a.realized_today <= -c.daily_loss_halt_fraction * a.session_start_equity:
            self._halt("DAILY_LOSS", t, f"realized_today {a.realized_today:.2f} <= "
                       f"-{c.daily_loss_halt_fraction} x session-start equity {a.session_start_equity:.2f}")
        if a.drawdown >= c.peak_to_trough_drawdown_halt_fraction * a.high_water_mark:
            self._halt("DRAWDOWN", t, f"drawdown {a.drawdown:.2f} >= {c.peak_to_trough_drawdown_halt_fraction}"
                       f" x HWM {a.high_water_mark:.2f}")
        if (self.profit_paused_day != a.session_day and
                a.realized_today >= c.daily_profit_pause_fraction * a.session_start_equity):
            self.profit_paused_day = a.session_day
            a.log("PROFIT_PAUSE", t, {"realized_today": a.realized_today})
            self.timeline.append({"time_utc": t.isoformat(), "event": "PROFIT_PAUSE", "halt": "DAILY_PROFIT_PAUSE",
                                  "reason": f"realized_today {a.realized_today:.2f}"})

    def observe_data_quality(self, q: DataQuality, t: datetime) -> None:
        self._bad_data_streak = self._bad_data_streak + 1 if q in (DataQuality.STALE, DataQuality.INVALID) else 0
        if self._bad_data_streak >= self.cfg.data_quality_halt_after:
            self._halt("DATA_QUALITY", t, f"{self._bad_data_streak} consecutive STALE/INVALID evaluations")

    def observe_drift(self, forward_brier: float | None, validation_brier: float | None, n: int,
                      t: datetime, key: str) -> None:
        if forward_brier is None or validation_brier is None or n < self.cfg.drift_min_samples:
            return
        if forward_brier > validation_brier + self.cfg.drift_brier_margin:
            self._halt("MODEL_DRIFT", t, f"{key}: forward Brier {forward_brier:.3f} vs validation "
                       f"{validation_brier:.3f} over {n} settled")

    @property
    def profit_paused(self) -> bool:
        return self.profit_paused_day is not None and self.profit_paused_day == self.acct.session_day

    def status(self) -> dict:
        return {"halts": dict(self.halts), "profit_paused": self.profit_paused,
                "entries_blocked": bool(self.halts) or self.profit_paused, "timeline": list(self.timeline)}

    # ---- pre-trade --------------------------------------------------------------------------
    def proposed_stake(self) -> float:
        return round(self.cfg.stake_fraction_of_available_equity * self.acct.cash, 2)

    def pre_trade(self, stake: float, group: str) -> list[Gate]:
        """Caller must hold acct.lock so that check + reserve is atomic."""
        a, c = self.acct, self.cfg
        eq = a.settled_equity
        open_stake = a.reserved
        g = [Gate("no_latched_halt", not self.halts, ", ".join(self.halts) or "none"),
             Gate("no_profit_pause", not self.profit_paused, "paused" if self.profit_paused else "none")]
        g.append(Gate("stake_positive", stake > 0 and stake <= a.cash, f"stake {stake:.2f}, cash {a.cash:.2f}"))
        g.append(Gate("max_simultaneous_contracts", len(a.open_contracts) < c.maximum_simultaneous_contracts,
                      f"{len(a.open_contracts)} open / max {c.maximum_simultaneous_contracts}"))
        cap = c.maximum_total_open_stake_fraction * eq
        g.append(Gate("max_total_open_stake", open_stake + stake <= cap + 1e-9,
                      f"{open_stake + stake:.2f} vs cap {cap:.2f}"))
        gcap = c.maximum_correlated_group_stake_fraction * eq
        gs = a.group_stake(group)
        g.append(Gate("max_correlated_exposure", gs + stake <= gcap + 1e-9,
                      f"group {group}: {gs + stake:.2f} vs cap {gcap:.2f}"))
        loss_lim = c.daily_loss_halt_fraction * a.session_start_equity
        worst_day = -a.realized_today + open_stake + stake
        g.append(Gate("daily_loss_worst_case", worst_day <= loss_lim + 1e-9,
                      f"worst-case day loss {worst_day:.2f} vs limit {loss_lim:.2f}"))
        dd_lim = c.peak_to_trough_drawdown_halt_fraction * a.high_water_mark
        worst_dd = a.drawdown + open_stake + stake
        g.append(Gate("drawdown_worst_case", worst_dd <= dd_lim + 1e-9,
                      f"worst-case drawdown {worst_dd:.2f} vs limit {dd_lim:.2f}"))
        return g
