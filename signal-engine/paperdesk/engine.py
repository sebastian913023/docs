"""Decision engine: per-horizon CALL / PUT / ABSTAIN with every gate recorded.

Order of gates (any failure => ABSTAIN): data quality -> validated calibrated model ->
calibrated p_win threshold -> fresh payout quote -> expected net payoff -> account risk gates.
The signal-strength score is reported but cannot pass or override any gate, and quantum
participation never adds confidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from .config import HORIZONS, USABLE_VALIDATION, Action, DataMode, DataQuality, RiskConfig, ValidationStatus
from .execution import assert_paper_only
from .exits import UNSUPPORTED, ExitCapability, ReferenceAlerts
from .indicators import components, data_quality, strength, volume_available
from .ledger import Account, Contract, new_id
from .model import FEATURES, HorizonModel, promote_forward
from .risk import RiskManager

BAR = timedelta(minutes=1)


@dataclass(frozen=True)
class PayoutQuote:
    payout_ratio: float          # net profit per $1 staked if the contract wins
    quote_time: datetime
    source: str


PayoutFn = Callable[[str, int, datetime], "PayoutQuote | None"]


def no_quotes(_instrument: str, _h: int, _t: datetime) -> None:
    return None


@dataclass
class Engine:
    instrument: str
    data_source: str
    data_mode: DataMode
    models: dict                                  # (horizon, variant) -> HorizonModel
    payout_fn: PayoutFn = no_quotes
    cfg: RiskConfig = field(default_factory=RiskConfig)
    exit_capability: ExitCapability = UNSUPPORTED
    alert_scale_sigmas: float = 1.0               # reference levels at +/- k * sigma * sqrt(h) * price
    horizons: tuple = HORIZONS
    cadence: dict | None = None                   # horizon -> decide every N minutes (default N=h)
    quantum: dict = field(default_factory=dict)   # horizon -> AsyncQuantumScorer (research primary)
    quantum_deadline_s: float = 20.0

    def __post_init__(self):
        assert_paper_only(self.cfg, self.data_mode)
        self.account = Account(self.data_mode, self.cfg)
        self.risk = RiskManager(self.account)
        self.decisions: list[dict] = []
        self.alerts: dict[str, ReferenceAlerts] = {}
        self.contract_meta: dict[str, dict] = {}
        self.cadence = self.cadence or {h: h for h in self.horizons}
        self.group = self.cfg.correlation_groups.get(self.instrument, self.instrument)

    # ---- per-bar loop -----------------------------------------------------------------------
    def step(self, bars: pd.DataFrame, now: datetime) -> list[dict]:
        """`bars` = every bar that has CLOSED at or before `now` (index = bar open time)."""
        self.settle_due(bars, now)
        if len(bars):
            price = float(bars["close"].iloc[-1])
            for a in list(self.alerts.values()):
                if self.account.contracts[a.contract_id].status == "OPEN":
                    a.check(self.account, price, now)
        self.risk.observe(now)
        q, notes = data_quality(bars, now)
        self.risk.observe_data_quality(q, now)
        out = []
        minute = int(pd.Timestamp(now).timestamp() // 60)
        for h in self.horizons:
            if minute % self.cadence[h] == 0:
                out.append(self.decide(bars, now, h, q, notes))
        return out

    def settle_due(self, bars: pd.DataFrame, now: datetime) -> None:
        for c in list(self.account.open_contracts):
            if c.expiration_time > now:
                continue
            bar_open = pd.Timestamp(c.expiration_time) - BAR
            if bar_open not in bars.index:
                continue        # settlement price not yet available: contract stays OPEN and tracked
            settle_px = float(bars.at[bar_open, "close"])
            self.account.settle(c.contract_id, settle_px, c.expiration_time)
            meta = self.contract_meta.get(c.contract_id, {})
            dm = meta.get("dm")
            if dm is not None and self.data_mode == DataMode.LOCAL_PAPER:
                dm.forward.append((meta["p"], 1.0 if c.outcome == "WIN" else 0.0))
                promote_forward(dm, self.cfg.minimum_calibrated_win_probability)
                fb = float(np.mean([(p - w) ** 2 for p, w in dm.forward]))
                self.risk.observe_drift(fb, dm.validation["brier"], len(dm.forward), now, meta["key"])
        self.risk.observe(now)

    # ---- one decision -----------------------------------------------------------------------
    def decide(self, bars: pd.DataFrame, now: datetime, h: int, q: DataQuality, q_notes: list[str]) -> dict:
        cfg = self.cfg
        has_vol = volume_available(bars)
        variant = "SS-1.0" if has_vol else "SS-1.0-NOVOL"
        hm: HorizonModel | None = self.models.get((h, variant))
        d = {"decision_id": new_id("dec"), "instrument": self.instrument, "data_source": self.data_source,
             "data_mode": self.data_mode.value, "decision_time_utc": now.isoformat(), "horizon_minutes": h,
             "proposed_expiration_time_utc": (now + timedelta(minutes=h)).isoformat(),
             "action": Action.ABSTAIN.value, "proposed_stake": 0.0, "calibrated_p_win": None,
             "probability_uncertainty": None, "signal_strength": None, "payout_available_at_decision": None,
             "expected_net_payoff_per_dollar": None, "validation_status": ValidationStatus.UNVALIDATED.value,
             "data_quality_status": q.value, "data_quality_notes": q_notes, "risk_gates": [],
             "exit_support": self.exit_capability.dict(), "reference_alert_levels": None,
             "abstention_reasons": [], "model_version": hm.model_version if hm else None,
             "model_variant": variant, "training_cutoff_utc": hm.training_cutoff_utc if hm else None,
             "settlement_definition": hm.settlement_definition if hm else None,
             "quantum_provenance": None}
        reasons = d["abstention_reasons"]
        comp = components(bars.tail(400)) if len(bars) >= 60 else None
        if comp is not None and np.isfinite(comp[FEATURES[variant]].iloc[-1]).all():
            d["signal_strength"] = strength(comp.iloc[-1], has_vol).dict()
        if q in (DataQuality.STALE, DataQuality.INVALID):
            reasons.append(f"data quality {q.value}")
        if hm is None:
            reasons.append(f"no model trained for horizon {h} variant {variant}")
        if reasons or d["signal_strength"] is None:
            if d["signal_strength"] is None and not reasons:
                reasons.append("insufficient indicator history")
            return self._record(d, now)

        x = comp[FEATURES[variant]].iloc[[-1]].to_numpy()
        p_by_dir, status_by_dir = {}, {}
        for direction, dm in hm.models.items():
            status_by_dir[direction] = dm.status
            if dm.status in USABLE_VALIDATION:
                p_by_dir[direction] = float(dm.p_win(x)[0])
        source = "classical"
        if h in self.quantum:
            qp, p_q = self._quantum(h, x, now)
            d["quantum_provenance"] = qp
            if p_q is not None:
                p_by_dir, source = p_q, "quantum"
            elif not p_by_dir:
                reasons.append("quantum result unavailable/stale and no separately validated classical fallback")
        if not p_by_dir:
            st = {k.value: v.value for k, v in status_by_dir.items()}
            d["validation_status"] = st[Action.CALL.value] if len(set(st.values())) == 1 else str(st)
            reasons.append(f"no validated calibrated probability ({st})")
            return self._record(d, now)
        direction = max(p_by_dir, key=p_by_dir.get)
        p = p_by_dir[direction]
        dm = hm.models[direction]
        d["validation_status"] = (d["quantum_provenance"]["validation_status"] if source == "quantum"
                                  else dm.status.value)
        if source == "quantum":
            dm = None                      # forward stats track the classical model only
        d["calibrated_p_win"] = round(p, 4)
        d["calibrated_direction"] = direction.value
        d["probability_source"] = source
        d["probability_uncertainty"] = dm.uncertainty(p) if dm is not None else None
        if p < cfg.minimum_calibrated_win_probability:
            reasons.append(f"calibrated p_win {p:.3f} < gate {cfg.minimum_calibrated_win_probability}")
        quote = self.payout_fn(self.instrument, h, now)
        if quote is None:
            reasons.append("no payout quote available at decision (platform quote history missing)")
        elif (now - quote.quote_time).total_seconds() > cfg.max_quote_age_s:
            reasons.append("payout quote stale")
        else:
            d["payout_available_at_decision"] = {"payout_ratio": quote.payout_ratio, "source": quote.source,
                                                 "quote_time_utc": quote.quote_time.isoformat()}
            ev = p * quote.payout_ratio - (1 - p)
            d["expected_net_payoff_per_dollar"] = round(ev, 4)
            if ev <= cfg.minimum_expected_net_payoff_per_dollar:
                reasons.append(f"expected net payoff {ev:.4f} <= {cfg.minimum_expected_net_payoff_per_dollar}")
        if reasons:
            return self._record(d, now)

        price = float(bars["close"].iloc[-1])
        with self.account.lock:
            stake = self.risk.proposed_stake()
            gates = self.risk.pre_trade(stake, self.group)
            d["risk_gates"] = [g.dict() for g in gates]
            d["proposed_stake"] = stake
            failed = [g.name for g in gates if not g.passed]
            if failed:
                reasons.append("risk gate failed: " + ", ".join(failed))
                return self._record(d, now)
            d["action"] = direction.value
            self._record(d, now)
            c = Contract(new_id("ctr"), d["decision_id"], self.instrument, direction, stake,
                         quote.payout_ratio, price, now, now + timedelta(minutes=h), self.data_mode, self.group)
            self.account.open_contract(c)
        self.contract_meta[c.contract_id] = {"dm": dm, "p": p, "key": hm.model_version + "/" + direction.value,
                                             "horizon": h}
        if not self.exit_capability.early_exit_supported:
            sig = float(np.log(bars["close"]).diff().tail(60).std())
            a = ReferenceAlerts.build(c, self.alert_scale_sigmas * sig * np.sqrt(h) * price)
            self.alerts[c.contract_id] = a
            d["reference_alert_levels"] = a.dict()
        return d

    def _quantum(self, h: int, x: np.ndarray, now: datetime):
        scorer = self.quantum[h]
        res = scorer.score_with_deadline(x, now, self.quantum_deadline_s)
        prov = res.provenance()
        if res.fresh and res.validated and res.p_by_dir:
            prov["used_for_decision"] = True
            return prov, {Action(k): v for k, v in res.p_by_dir.items()}
        prov["used_for_decision"] = False
        return prov, None

    def _record(self, d: dict, now: datetime) -> dict:
        self.account.log("DECISION", now, {k: d[k] for k in (
            "decision_id", "horizon_minutes", "action", "calibrated_p_win", "validation_status",
            "data_quality_status", "proposed_stake", "abstention_reasons")})
        self.decisions.append(d)
        return d


def replay(engine: Engine, bars: pd.DataFrame, start: int, end: int | None = None) -> Engine:
    """Bar-by-bar historical simulation. At step i the engine sees bars[:i+1] (all closed)."""
    end = len(bars) if end is None else end
    for i in range(start, end):
        now = (bars.index[i] + BAR).to_pydatetime()
        engine.step(bars.iloc[max(0, i - 400):i + 1], now)
    return engine


def fit_models(bars: pd.DataFrame, instrument: str, cfg: RiskConfig, horizons=HORIZONS,
               variants=("SS-1.0", "SS-1.0-NOVOL")) -> dict:
    from .model import fit_horizon
    if not volume_available(bars):
        variants = tuple(v for v in variants if v != "SS-1.0")   # no volume => only the NOVOL variant
    return {(h, v): fit_horizon(bars, instrument, h, v, cfg.minimum_calibrated_win_probability)
            for h in horizons for v in variants}
