"""Acceptance tests for the paperdesk paper-trading slice. All inputs are deterministic fixtures."""
import json
import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from paperdesk import execution, fixtures, report
from paperdesk.config import Action, DataMode, DataQuality, QuantumStatus, RiskConfig, TieRule, ValidationStatus
from paperdesk.engine import BAR, Engine, PayoutQuote, fit_models, replay
from paperdesk.exits import ExitCapability, ExitQuote, ReferenceAlerts, request_exit
from paperdesk.ledger import Account, Contract, LedgerError, new_id
from paperdesk.model import HorizonModel, fit_horizon
from paperdesk.quantum import AsyncQuantumScorer, QuantumConfig, QuantumConnector

PKG = Path(__file__).resolve().parent.parent / "paperdesk"


# ---- helpers --------------------------------------------------------------------------------
class StubDM:
    def __init__(self, direction, p, status=ValidationStatus.HISTORICALLY_VALIDATED):
        self.direction, self.p, self.status = direction, p, status
        self.validation = {"brier": 0.2}
        self.forward = []

    def p_win(self, x):
        return np.full(len(x), self.p)

    def uncertainty(self, p):
        return None


def stub_models(p_call=0.9, p_put=0.05, status=ValidationStatus.HISTORICALLY_VALIDATED, horizons=(3, 5, 15)):
    out = {}
    for h in horizons:
        for v in ("SS-1.0", "SS-1.0-NOVOL"):
            out[(h, v)] = HorizonModel("SYN", h, v, {Action.CALL: StubDM(Action.CALL, p_call, status),
                                                     Action.PUT: StubDM(Action.PUT, p_put, status)}, "2026-01-01")
    return out


def payout(_i, _h, t):
    return PayoutQuote(0.85, t, "FIXTURE_ASSUMPTION")


def make_engine(models=None, cfg=None, mode=DataMode.REPLAY, **kw):
    return Engine("BTC-USD", "fixture", mode, stub_models() if models is None else models, payout,
                  cfg or RiskConfig(), **kw)


@pytest.fixture(scope="module")
def bars():
    return fixtures.bars(n=600, seed=3)


def now_of(b):
    return (b.index[-1] + BAR).to_pydatetime()


def decide(eng, b, h=3, now=None):
    now = now or now_of(b)
    q, notes = __import__("paperdesk.indicators", fromlist=["x"]).data_quality(b, now)
    return eng.decide(b, now, h, q, notes)


def contract(acct, direction=Action.CALL, stake=25.0, strike=100.0, t=None, h=3):
    t = t or pd.Timestamp("2026-01-05 12:00", tz="UTC").to_pydatetime()
    c = Contract(new_id("ctr"), new_id("dec"), "BTC-USD", direction, stake, 0.85, strike, t,
                 t + timedelta(minutes=h), acct.data_mode, "crypto_major")
    with acct.lock:
        acct.open_contract(c)
    return c


# ---- 1. no real orders ----------------------------------------------------------------------
def test_no_real_orders_can_be_sent():
    with pytest.raises(execution.LiveExecutionForbidden):
        execution.submit_live_order("BTC-USD", "CALL", 100)
    with pytest.raises(execution.LiveExecutionForbidden):
        make_engine(cfg=RiskConfig(live_order_execution_enabled=True))
    with pytest.raises(execution.PlatformDemoUnavailable):
        make_engine(mode=DataMode.PLATFORM_DEMO)
    src = "\n".join(p.read_text() for p in PKG.glob("*.py"))
    for net in ("import requests", "import httpx", "import socket", "urllib.request"):
        assert net not in src


# ---- 2/3. account-level stops ---------------------------------------------------------------
def _settle_n(acct, risk, n, win, t0):
    for i in range(n):
        t = t0 + timedelta(minutes=10 * i)
        c = contract(acct, t=t)
        acct.settle(c.contract_id, 101.0 if win else 99.0, c.expiration_time)
        risk.observe(c.expiration_time)


def test_daily_loss_halt_blocks_new_entries(bars):
    eng = make_engine()
    t0 = bars.index[-1].to_pydatetime() - timedelta(hours=3)
    eng.risk.observe(t0)
    _settle_n(eng.account, eng.risk, 8, False, t0)          # 8 x -25 = -200 = 2% of 10,000
    assert "DAILY_LOSS" in eng.risk.halts
    d = decide(eng, bars)
    assert d["action"] == "ABSTAIN"
    assert any(g["name"] == "no_latched_halt" and not g["passed"] for g in d["risk_gates"])
    # latched: unauthorized reset is refused
    assert not eng.risk.reset("DAILY_LOSS", now_of(bars), "guess")
    assert "DAILY_LOSS" in eng.risk.halts


def test_authorized_reset(bars, monkeypatch):
    monkeypatch.setenv("PAPERDESK_RESET_TOKEN", "s3cret-reset")
    eng = make_engine()
    eng.risk.kill_switch(now_of(bars))
    assert decide(eng, bars)["action"] == "ABSTAIN"
    assert eng.risk.reset("KILL_SWITCH", now_of(bars), "s3cret-reset")
    assert decide(eng, bars)["action"] == "CALL"


def test_profit_pause_blocks_new_entries(bars):
    eng = make_engine()
    t0 = bars.index[-1].to_pydatetime() - timedelta(hours=3)
    eng.risk.observe(t0)
    _settle_n(eng.account, eng.risk, 10, True, t0)          # 10 x 21.25 = 212.5 >= 200
    assert eng.risk.profit_paused
    d = decide(eng, bars)
    assert d["action"] == "ABSTAIN" and "no_profit_pause" in d["abstention_reasons"][0]


def test_open_contracts_remain_tracked_after_halt(bars):
    eng = make_engine()
    first = decide(eng, bars, h=15)
    assert first["action"] == "CALL"
    eng.risk.kill_switch(now_of(bars), "test")
    cid = next(iter(eng.account.contracts))
    assert eng.account.contracts[cid].status == "OPEN"
    assert eng.account.summary()["open_contract_count"] == 1
    # extend bars past expiry; halt stays latched, contract settles by its own rules
    more = fixtures.bars(n=620, seed=3)
    eng.settle_due(more, (more.index[-1] + BAR).to_pydatetime())
    c = eng.account.contracts[cid]
    assert c.status == "SETTLED" and c.outcome in ("WIN", "LOSS", "TIE")
    assert c.settle_price == more.at[pd.Timestamp(c.expiration_time) - BAR, "close"]
    assert "KILL_SWITCH" in eng.risk.halts


# ---- 4. exposure under concurrency ----------------------------------------------------------
def test_concurrent_entries_cannot_exceed_exposure_cap(bars):
    eng = make_engine()
    now = now_of(bars)
    barrier = threading.Barrier(12)

    def go():
        barrier.wait()
        decide(eng, bars, h=15, now=now)

    th = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in th]
    [t.join() for t in th]
    a = eng.account
    assert len(a.open_contracts) <= RiskConfig().maximum_simultaneous_contracts
    assert a.reserved <= RiskConfig().maximum_total_open_stake_fraction * a.settled_equity + 1e-9
    assert a.cash + a.reserved == pytest.approx(10_000.0)
    assert sum(d["action"] != "ABSTAIN" for d in eng.decisions) == len(a.contracts)


def test_worst_case_open_loss_in_pretrade(bars):
    cfg = RiskConfig(daily_loss_halt_fraction=0.006, maximum_total_open_stake_fraction=0.05,
                     maximum_correlated_group_stake_fraction=0.05, maximum_simultaneous_contracts=10)
    eng = make_engine(cfg=cfg)
    eng.risk.observe(now_of(bars))
    actions = [decide(eng, bars, h=15)["action"] for _ in range(4)]
    assert actions == ["CALL", "CALL", "ABSTAIN", "ABSTAIN"]   # 3 x 25 = 75 > 60 worst-case limit
    assert any("daily_loss_worst_case" in r for r in eng.decisions[-1]["abstention_reasons"])


# ---- 5. exits and alerts --------------------------------------------------------------------
def test_unsupported_early_exit_cannot_improve_booked_pnl():
    acct = Account(DataMode.REPLAY)
    c = contract(acct)
    before = acct.summary()
    q = ExitQuote(30.0, c.entry_time + timedelta(seconds=60))       # attractive quote, but unsupported
    r = request_exit(acct, c, q, ExitCapability(), c.entry_time + timedelta(seconds=60), 2.0)
    assert not r.accepted and r.reason == "EARLY_EXIT_UNSUPPORTED"
    assert c.status == "OPEN" and acct.realized_pnl == before["realized_pnl"] == 0.0
    acct.settle(c.contract_id, 99.0, c.expiration_time)
    assert c.booked_pnl == -25.0


def test_break_even_alerts_do_not_rewrite_outcomes():
    acct = Account(DataMode.REPLAY)
    c = contract(acct, strike=100.0)
    a = ReferenceAlerts.build(c, 0.5)
    assert a.label == "ANALYTICAL ALERT — NOT AN EXECUTABLE CONTRACT EXIT" and not a.executable
    t = c.entry_time + timedelta(minutes=1)
    assert a.check(acct, 100.6, t) == ["reference_target"]          # target crossed while in profit
    assert acct.realized_pnl == 0.0 and acct.cash == 10_000 - 25
    acct.add_hypothetical_exit_pnl(21.25, t, {"contract_id": c.contract_id, "why": "target alert"})
    acct.settle(c.contract_id, 99.9, c.expiration_time)               # finishes below strike
    assert c.outcome == "LOSS" and acct.realized_pnl == -25.0
    assert acct.hypothetical_early_exit_pnl == 21.25                  # kept separate, never booked
    assert acct.summary()["realized_pnl"] == -25.0


def test_rejected_exits_leave_contracts_open():
    acct = Account(DataMode.REPLAY)
    cap = ExitCapability(True, True, True, True, True, exit_cost_per_contract=0.5, exit_latency_s=0.8)
    assert cap.early_exit_supported
    c = contract(acct)
    t = c.entry_time + timedelta(seconds=90)
    stale = request_exit(acct, c, ExitQuote(28.0, t - timedelta(seconds=3)), cap, t, 2.0)
    venue = request_exit(acct, c, ExitQuote(28.0, t), cap, t, 2.0, venue_rejects=True)
    none = request_exit(acct, c, None, cap, t, 2.0)
    assert [stale.accepted, venue.accepted, none.accepted] == [False, False, False]
    assert stale.reason.startswith("STALE_QUOTE") and venue.reason == "VENUE_REJECTED"
    assert c.status == "OPEN" and acct.realized_pnl == 0.0 and acct.reserved == 25.0
    ok = request_exit(acct, c, ExitQuote(28.0, t), cap, t, 2.0)        # supported + fresh => fills
    assert ok.accepted and ok.booked_pnl == pytest.approx(28.0 - 0.5 - 25.0)


def test_break_even_uses_recoverable_value_after_costs():
    from paperdesk.exits import break_even_reached
    acct = Account(DataMode.REPLAY)
    c = contract(acct)
    cap = ExitCapability(True, True, True, True, True, exit_cost_per_contract=1.0)
    t = c.entry_time
    assert not break_even_reached(c, ExitQuote(25.5, t), cap)
    assert break_even_reached(c, ExitQuote(26.0, t), cap)


# ---- 6. abstention rules --------------------------------------------------------------------
def test_missing_calibration_yields_abstain(bars):
    for models in ({}, stub_models(status=ValidationStatus.UNVALIDATED), stub_models(status=ValidationStatus.REJECTED)):
        d = decide(make_engine(models=models), bars)
        assert d["action"] == "ABSTAIN" and d["calibrated_p_win"] is None
        assert d["signal_strength"] is None or d["signal_strength"]["is_probability"] is False


def test_stale_data_yields_abstain(bars):
    d = decide(make_engine(), bars, now=now_of(bars) + timedelta(minutes=10))
    assert d["data_quality_status"] == DataQuality.STALE.value and d["action"] == "ABSTAIN"


def test_invalid_data_yields_abstain(bars):
    bad = bars.copy()
    bad.iloc[-3, bad.columns.get_loc("close")] = np.nan
    assert decide(make_engine(), bad)["data_quality_status"] == "INVALID"


def test_missing_payout_quote_yields_abstain(bars):
    eng = Engine("BTC-USD", "fixture", DataMode.REPLAY, stub_models())
    d = decide(eng, bars)
    assert d["action"] == "ABSTAIN" and any("quote history missing" in r for r in d["abstention_reasons"])


def test_strength_never_overrides_failed_gate(bars):
    d = decide(make_engine(models=stub_models(p_call=0.70, p_put=0.2)), bars)
    assert d["action"] == "ABSTAIN" and d["calibrated_p_win"] == 0.70
    assert any("< gate 0.75" in r for r in d["abstention_reasons"])


def test_required_decision_fields(bars):
    d = decide(make_engine(), bars)
    for k in ("decision_id", "instrument", "data_source", "data_mode", "decision_time_utc", "horizon_minutes",
              "proposed_expiration_time_utc", "action", "proposed_stake", "calibrated_p_win", "signal_strength",
              "payout_available_at_decision", "expected_net_payoff_per_dollar", "validation_status",
              "data_quality_status", "risk_gates", "exit_support", "reference_alert_levels", "abstention_reasons",
              "model_version", "training_cutoff_utc", "quantum_provenance"):
        assert k in d
    assert d["action"] == "CALL" and d["proposed_stake"] == 25.0
    assert d["expected_net_payoff_per_dollar"] == pytest.approx(0.9 * 0.85 - 0.1)
    assert d["exit_support"]["early_exit_supported"] is False
    assert d["reference_alert_levels"]["label"].startswith("ANALYTICAL ALERT")
    ss = d["signal_strength"]
    assert set(ss["contributions"]) == {"rsi", "macd", "fib", "volume"} and 0 <= ss["signal_strength_0_to_100"] <= 100


def test_no_volume_uses_documented_variant():
    b = fixtures.bars(n=600, seed=3, with_volume=False)
    d = decide(make_engine(), b)
    assert d["model_variant"] == "SS-1.0-NOVOL" and "NOVOL" in d["model_version"]
    assert d["signal_strength"]["methodology_version"] == "SS-1.0-NOVOL"
    assert str(d["signal_strength"]["contributions"]["volume"]).startswith("UNAVAILABLE")
    assert "actual volume unavailable" in d["data_quality_notes"]
    models = fit_models(fixtures.bars(n=6000, seed=5, with_volume=False), "SYN", RiskConfig(), horizons=(3,))
    assert set(models) == {(3, "SS-1.0-NOVOL")}


# ---- 7. accounting --------------------------------------------------------------------------
def test_expiration_and_payout_accounting():
    acct = Account(DataMode.REPLAY)
    w, l, p = contract(acct, Action.CALL), contract(acct, Action.PUT), contract(acct, Action.PUT)
    assert acct.cash == 10_000 - 75 and acct.reserved == 75 and acct.settled_equity == 10_000
    acct.settle(w.contract_id, 100.5, w.expiration_time)
    acct.settle(l.contract_id, 100.5, l.expiration_time)
    acct.settle(p.contract_id, 100.0, p.expiration_time)               # tie
    assert (w.outcome, l.outcome, p.outcome) == ("WIN", "LOSS", "TIE")
    assert w.booked_pnl == pytest.approx(21.25) and l.booked_pnl == -25 and p.booked_pnl == -25   # TieRule.LOSS
    assert acct.reserved == 0 and acct.cash == pytest.approx(10_000 + 21.25 - 50)
    r = Account(DataMode.REPLAY, RiskConfig(tie_rule=TieRule.REFUND))
    t = contract(r)
    r.settle(t.contract_id, 100.0, t.expiration_time)
    assert t.booked_pnl == 0 and r.cash == 10_000
    with pytest.raises(LedgerError):
        acct.settle(w.contract_id, 101, w.expiration_time)


def test_ledger_is_tamper_evident_and_modes_never_mix():
    acct = Account(DataMode.REPLAY)
    c = contract(acct)
    acct.settle(c.contract_id, 101, c.expiration_time)
    assert acct.verify_chain()
    with pytest.raises(TypeError):
        acct.records[1].data["stake"] = 1
    acct._records[1] = acct._records[1].__class__(**{**acct._records[1].__dict__, "kind": "EDITED"})
    assert not acct.verify_chain()
    paper = Account(DataMode.LOCAL_PAPER)
    bad = Contract("x", "d", "BTC-USD", Action.CALL, 25, 0.85, 100, c.entry_time, c.expiration_time,
                   DataMode.REPLAY, "g")
    with pytest.raises(LedgerError):
        paper.open_contract(bad)


# ---- 8. quantum -----------------------------------------------------------------------------
class FakeBackend:
    def __init__(self, name):
        self.name = name


class FakeService:
    def __init__(self, names, **kw):
        self.kw, self._b = kw, [FakeBackend(n) for n in names]

    def backends(self):
        return self._b

    def backend(self, name):
        return next(b for b in self._b if b.name == name)


def test_qpu_status_matches_hardware_evidence():
    assert QuantumConnector(QuantumConfig()).authenticate() == QuantumStatus.DISABLED
    fac = lambda **kw: FakeService(["ibm_alpha", "ibm_beta"], **kw)      # noqa: E731
    unselected = QuantumConnector(QuantumConfig(api_key="k" * 20), fac)
    assert unselected.authenticate() == QuantumStatus.AUTHENTICATED
    assert unselected.verify_backend() == QuantumStatus.AUTHENTICATED     # needs an explicit selection
    assert "ibm_alpha" in unselected.last_error
    missing = QuantumConnector(QuantumConfig(api_key="k" * 20, backend_name="ibm_sanfrancisco"), fac)
    missing.authenticate()
    assert missing.verify_backend() == QuantumStatus.FAILED
    ok = QuantumConnector(QuantumConfig(api_key="k" * 20, backend_name="ibm_beta"), fac)
    ok.authenticate()
    assert ok.verify_backend() == QuantumStatus.BACKEND_VERIFIED
    ev = ok.evidence()
    assert ev["hardware_jobs_completed"] == 0 and ev["status"] != "JOB_COMPLETED"
    assert "qpu_submission_enabled=false" in ev["submission_blockers"]
    with pytest.raises(PermissionError):
        ok.submit_kernel(np.zeros((1, 2)), np.zeros((1, 2)))
    budget_only = QuantumConnector(QuantumConfig(api_key="k" * 20, backend_name="ibm_beta",
                                                 qpu_submission_enabled=True), fac)
    budget_only.authenticate(), budget_only.verify_backend()
    assert budget_only.submission_blockers() == ["no compute budget (IBM_QUANTUM_MAX_QPU_SECONDS)"]


def test_simulator_kernel_marks_simulator_not_hardware():
    pytest.importorskip("qiskit")
    qc = QuantumConnector(QuantumConfig())
    x = np.array([[0.1, 0.2], [1.0, 2.0]])
    exact = qc.simulate_kernel(x, x)["kernel"]
    assert np.allclose(np.diag(exact), 1.0)
    shots = qc.simulate_kernel(x, x, shots=2000)
    assert np.allclose(shots["kernel"], exact, atol=0.06)
    assert len(shots["provenance"]["circuit_hash"]) == 64
    assert qc.status == QuantumStatus.SIMULATOR and qc.evidence()["hardware_jobs_completed"] == 0


def test_qpu_timeout_cannot_create_stale_signal(bars):
    def slow(_x):
        time.sleep(0.6)
        return {"CALL": 0.99, "PUT": 0.01}

    fallback = make_engine(quantum={3: AsyncQuantumScorer(slow, ValidationStatus.HISTORICALLY_VALIDATED)},
                           quantum_deadline_s=0.05)
    d = decide(fallback, bars)
    assert d["quantum_provenance"]["fresh"] is False and d["quantum_provenance"]["used_for_decision"] is False
    assert d["probability_source"] == "classical" and d["calibrated_p_win"] == 0.9
    no_fallback = make_engine(models=stub_models(status=ValidationStatus.UNVALIDATED),
                              quantum={3: AsyncQuantumScorer(slow, ValidationStatus.HISTORICALLY_VALIDATED)},
                              quantum_deadline_s=0.05)
    d2 = decide(no_fallback, bars)
    assert d2["action"] == "ABSTAIN" and any("quantum" in r for r in d2["abstention_reasons"])
    snap = json.dumps(d2, sort_keys=True, default=str)
    time.sleep(0.8)                                                      # late result arrives
    assert json.dumps(d2, sort_keys=True, default=str) == snap and len(no_fallback.decisions) == 1
    assert d2["quantum_provenance"]["confidence_bonus"] == 0.0


def test_unvalidated_quantum_never_used(bars):
    eng = make_engine(quantum={3: AsyncQuantumScorer(lambda x: {"CALL": 0.99, "PUT": 0.01})})
    d = decide(eng, bars)
    assert d["quantum_provenance"]["used_for_decision"] is False and d["calibrated_p_win"] == 0.9


# ---- 9. secrets -----------------------------------------------------------------------------
def test_credentials_never_written_to_logs_or_reports(bars, monkeypatch, caplog):
    key, crn = "APIKEY-very-secret-123456", "crn:v1:bluemix:public:quantum-computing:us-east:a/xyz::"
    monkeypatch.setenv("IBM_QUANTUM_API_KEY", key)
    monkeypatch.setenv("IBM_QUANTUM_INSTANCE_CRN", crn)
    report.install_log_redaction()

    def boom(**kw):
        raise RuntimeError(f"auth failed for token={kw['token']} instance={kw['instance']}")

    caplog.set_level(logging.DEBUG)
    qc = QuantumConnector(QuantumConfig.from_env(), boom)
    assert qc.authenticate() == QuantumStatus.FAILED
    logging.getLogger("paperdesk.test").info("leak attempt %s", key)
    eng = make_engine()
    decide(eng, bars)
    state = json.dumps(report.build_state([report.run_state(eng, "t", [])], qc.evidence(), None))
    blob = state + caplog.text + repr(qc.cfg) + str(qc.last_error)
    assert key not in blob and crn not in blob
    assert "[REDACTED]" in qc.last_error


# ---- 10. real pipeline on fixtures ----------------------------------------------------------
def test_random_walk_fixture_is_never_validated():
    b = fixtures.bars(n=60 * 24 * 6, seed=21, planted=0.0)
    for h in (3, 5, 15):
        hm = fit_horizon(b, "SYN", h, "SS-1.0", 0.75)
        assert all(m.status not in (ValidationStatus.HISTORICALLY_VALIDATED,) for m in hm.models.values())


def test_end_to_end_replay_on_planted_fixture():
    b = fixtures.bars(n=60 * 24 * 8, seed=13, planted=0.8, with_volume=False)
    cut = int(len(b) * 0.8)
    models = fit_models(b.iloc[:cut], "SYN", RiskConfig(), horizons=(3,))
    eng = Engine("SYN", "fixture", DataMode.REPLAY, models, payout, horizons=(3,))
    replay(eng, b, cut, cut + 600)
    acts = {d["action"] for d in eng.decisions}
    assert "ABSTAIN" in acts and acts & {"CALL", "PUT"}
    for d in eng.decisions:
        if d["action"] != "ABSTAIN":
            assert d["calibrated_p_win"] >= 0.75 and d["validation_status"] == "HISTORICALLY_VALIDATED"
            assert all(g["passed"] for g in d["risk_gates"])
    a = eng.account
    assert a.verify_chain() and a.cash + a.reserved == pytest.approx(a.settled_equity)
    assert a.cash + a.reserved == pytest.approx(10_000 + a.realized_pnl)
    st = report.run_state(eng, "t", [])
    assert any("QUOTE HISTORY MISSING" in w for w in st["warnings"])


def test_settlement_attributed_to_its_own_utc_day():
    acct = Account(DataMode.REPLAY)
    t = pd.Timestamp("2026-01-05 23:50", tz="UTC").to_pydatetime()
    acct.roll_session(t)
    late = contract(acct, t=t, h=5)                                    # expires 23:55, same day
    nxt = contract(acct, t=t, h=15)                                    # expires 00:05, next day
    acct.settle(late.contract_id, 99.0, late.expiration_time)
    assert acct.session_day == "2026-01-05" and acct.realized_today == -25.0
    acct.settle(nxt.contract_id, 101.0, nxt.expiration_time)
    assert acct.session_day == "2026-01-06" and acct.realized_today == pytest.approx(21.25)
    assert acct.session_start_equity == pytest.approx(10_000 - 25.0)
    assert not acct.roll_session(t)                                    # never rolls backwards
