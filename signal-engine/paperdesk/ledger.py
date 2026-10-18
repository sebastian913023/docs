"""Virtual paper account with an append-only, hash-chained event log.

One Account holds exactly one DataMode, so LOCAL_PAPER, PLATFORM_DEMO and REPLAY results can never be
merged into a single performance curve. Booked (settlement / executed-exit) P&L and hypothetical
early-exit P&L are kept in separate fields.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from types import MappingProxyType

from .config import Action, DataMode, RiskConfig, TieRule


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Record:
    seq: int
    kind: str              # DECISION | ENTRY | EXPIRATION | SETTLEMENT | EXIT_* | ALERT | HALT | RESET ...
    time_utc: str
    data: MappingProxyType
    prev_hash: str
    hash: str

    def dict(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "time_utc": self.time_utc, "data": dict(self.data),
                "prev_hash": self.prev_hash, "hash": self.hash}


def _digest(seq: int, kind: str, t: str, data: dict, prev: str) -> str:
    blob = json.dumps([seq, kind, t, data, prev], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class Contract:
    contract_id: str
    decision_id: str
    instrument: str
    direction: Action
    stake: float
    payout_ratio: float          # net profit per $1 staked on a win
    strike: float                # underlying price at entry
    entry_time: datetime
    expiration_time: datetime
    data_mode: DataMode
    group: str
    status: str = "OPEN"         # OPEN | SETTLED | EXITED
    outcome: str | None = None   # WIN | LOSS | TIE
    settle_price: float | None = None
    booked_pnl: float | None = None

    @property
    def worst_case_loss(self) -> float:
        return self.stake


@dataclass
class Account:
    data_mode: DataMode
    cfg: RiskConfig = field(default_factory=RiskConfig)

    def __post_init__(self):
        self.cash = float(self.cfg.initial_virtual_balance)
        self.reserved = 0.0
        self.realized_pnl = 0.0
        self.hypothetical_early_exit_pnl = 0.0     # analytics only, never booked
        self.high_water_mark = self.settled_equity
        self.max_drawdown = 0.0
        self.contracts: dict[str, Contract] = {}
        self._records: list[Record] = []
        self._lock = threading.RLock()
        self.session_day: str | None = None
        self.session_start_equity = self.settled_equity
        self.realized_today = 0.0
        self.equity_curve: list[tuple[str, float]] = []

    # ---- derived balances -------------------------------------------------------------------
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def settled_equity(self) -> float:
        return self.cash + self.reserved

    @property
    def open_contracts(self) -> list[Contract]:
        return [c for c in self.contracts.values() if c.status == "OPEN"]

    @property
    def drawdown(self) -> float:
        return self.high_water_mark - self.settled_equity

    def group_stake(self, group: str) -> float:
        return sum(c.stake for c in self.open_contracts if c.group == group)

    # ---- immutable log ----------------------------------------------------------------------
    @property
    def records(self) -> tuple[Record, ...]:
        return tuple(self._records)

    def log(self, kind: str, t: datetime, data: dict) -> Record:
        with self._lock:
            seq = len(self._records)
            prev = self._records[-1].hash if self._records else "GENESIS"
            ts = t.isoformat()
            clean = json.loads(json.dumps(data, default=str))
            rec = Record(seq, kind, ts, MappingProxyType(clean), prev, _digest(seq, kind, ts, clean, prev))
            self._records.append(rec)
            return rec

    def verify_chain(self) -> bool:
        prev = "GENESIS"
        for r in self._records:
            if r.prev_hash != prev or r.hash != _digest(r.seq, r.kind, r.time_utc, dict(r.data), prev):
                return False
            prev = r.hash
        return True

    # ---- session ----------------------------------------------------------------------------
    def roll_session(self, t: datetime) -> bool:
        """Start a new UTC day if `t` is on a later day. Forward-only: never reopens a past day."""
        day = t.date().isoformat()
        with self._lock:
            if self.session_day is not None and day <= self.session_day:
                return False
            self.session_day = day
            self.session_start_equity = self.settled_equity
            self.realized_today = 0.0
            self.log("SESSION_START", t, {"day": day, "session_start_equity": self.session_start_equity})
            return True

    # ---- state changes ----------------------------------------------------------------------
    def open_contract(self, c: Contract) -> None:
        """Reserve stake. Caller must already hold `lock` and have passed the risk checks."""
        if c.data_mode != self.data_mode:
            raise LedgerError(f"{c.data_mode} contract cannot enter a {self.data_mode} account")
        if c.stake <= 0 or c.stake > self.cash + 1e-9:
            raise LedgerError("stake must be positive and covered by available cash")
        self.cash -= c.stake
        self.reserved += c.stake
        self.contracts[c.contract_id] = c
        self.log("ENTRY", c.entry_time, {
            "contract_id": c.contract_id, "decision_id": c.decision_id, "instrument": c.instrument,
            "direction": c.direction.value, "stake": c.stake, "payout_ratio": c.payout_ratio,
            "strike": c.strike, "expiration_time_utc": c.expiration_time.isoformat(),
            "available_cash": self.cash, "reserved": self.reserved})

    def settle(self, cid: str, settle_price: float, t: datetime) -> Contract:
        with self._lock:
            c = self.contracts[cid]
            if c.status != "OPEN":
                raise LedgerError(f"contract {cid} already {c.status}")
            self.log("EXPIRATION", t, {"contract_id": cid, "settle_price": settle_price})
            if settle_price == c.strike:
                outcome = "TIE"
                pnl = 0.0 if self.cfg.tie_rule == TieRule.REFUND else -c.stake
            else:
                up = settle_price > c.strike
                win = up if c.direction == Action.CALL else not up
                outcome = "WIN" if win else "LOSS"
                pnl = c.stake * c.payout_ratio if win else -c.stake
            self._book(c, pnl, t)
            c.status, c.outcome, c.settle_price = "SETTLED", outcome, settle_price
            self.log("SETTLEMENT", t, {"contract_id": cid, "outcome": outcome, "booked_pnl": pnl,
                                       "tie_rule": self.cfg.tie_rule.value, "available_cash": self.cash,
                                       "realized_pnl": self.realized_pnl})
            return c

    def book_executed_exit(self, cid: str, recoverable_value: float, t: datetime, detail: dict) -> Contract:
        """Only reachable through a verified, supported exit (see exits.py)."""
        with self._lock:
            c = self.contracts[cid]
            if c.status != "OPEN":
                raise LedgerError(f"contract {cid} already {c.status}")
            pnl = recoverable_value - c.stake
            self._book(c, pnl, t)
            c.status, c.outcome = "EXITED", "EARLY_EXIT"
            self.log("EXIT_FILLED", t, {"contract_id": cid, "booked_pnl": pnl, **detail})
            return c

    def _book(self, c: Contract, pnl: float, t: datetime) -> None:
        self.roll_session(t)              # attribute P&L to the UTC day of the booking time
        self.reserved -= c.stake
        self.cash += c.stake + pnl
        c.booked_pnl = pnl
        self.realized_pnl += pnl
        self.realized_today += pnl
        self.high_water_mark = max(self.high_water_mark, self.settled_equity)
        self.max_drawdown = max(self.max_drawdown, self.drawdown)
        self.equity_curve.append((t.isoformat(), round(self.settled_equity, 6)))

    def add_hypothetical_exit_pnl(self, amount: float, t: datetime, detail: dict) -> None:
        with self._lock:
            self.hypothetical_early_exit_pnl += amount
            self.log("HYPOTHETICAL_EXIT", t, {"amount": amount, "booked": False, **detail})

    def summary(self) -> dict:
        with self._lock:
            open_c = self.open_contracts
            return {
                "data_mode": self.data_mode.value,
                "initial_virtual_balance": self.cfg.initial_virtual_balance,
                "available_cash": round(self.cash, 6),
                "reserved_stake": round(self.reserved, 6),
                "settled_equity": round(self.settled_equity, 6),
                "realized_pnl": round(self.realized_pnl, 6),
                "realized_today": round(self.realized_today, 6),
                "session_start_equity": round(self.session_start_equity, 6),
                "hypothetical_early_exit_pnl_not_booked": round(self.hypothetical_early_exit_pnl, 6),
                "open_contract_count": len(open_c),
                "open_stake_at_risk": round(sum(c.stake for c in open_c), 6),
                "worst_case_open_loss": round(sum(c.worst_case_loss for c in open_c), 6),
                "high_water_mark": round(self.high_water_mark, 6),
                "drawdown": round(self.drawdown, 6),
                "max_drawdown": round(self.max_drawdown, 6),
                "records": len(self._records),
                "chain_ok": self.verify_chain(),
            }


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def contract_dict(c: Contract) -> dict:
    d = asdict(c)
    d["direction"] = c.direction.value
    d["data_mode"] = c.data_mode.value
    d["entry_time"] = c.entry_time.isoformat()
    d["expiration_time"] = c.expiration_time.isoformat()
    return d
