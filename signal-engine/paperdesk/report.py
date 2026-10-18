"""Dashboard state builder and secret redaction. Each run is reported under its own data mode; runs are
never merged into a combined performance curve."""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict

from . import execution
from .engine import Engine
from .ledger import contract_dict

SECRET_ENV = ("IBM_QUANTUM_API_KEY", "IBM_QUANTUM_INSTANCE_CRN", "PAPERDESK_RESET_TOKEN",
              "KALSHI_API_KEY_ID", "TWELVE_DATA_API_KEY")


def _secrets() -> list[str]:
    return [v for k in SECRET_ENV if (v := os.getenv(k)) and len(v) >= 4]


def redact_text(s: str) -> str:
    for v in _secrets():
        s = s.replace(v, "[REDACTED]")
    return s


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def install_log_redaction() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())


def performance(engine: Engine) -> dict:
    by_h: dict = defaultdict(lambda: {"settled": 0, "wins": 0, "losses": 0, "ties": 0, "booked_pnl": 0.0, "staked": 0.0})
    for cid, c in engine.account.contracts.items():
        if c.status == "OPEN":
            continue
        h = engine.contract_meta.get(cid, {}).get("horizon", "?")
        for key in (str(h), "combined"):
            b = by_h[key]
            b["settled"] += 1
            b["wins"] += c.outcome == "WIN"
            b["losses"] += c.outcome == "LOSS"
            b["ties"] += c.outcome == "TIE"
            b["booked_pnl"] += c.booked_pnl or 0.0
            b["staked"] += c.stake
    for b in by_h.values():
        b["hit_rate"] = b["wins"] / b["settled"] if b["settled"] else None
        b["return_on_stake"] = b["booked_pnl"] / b["staked"] if b["staked"] else None
    return dict(by_h)


def run_state(engine: Engine, label: str, notes: list[str]) -> dict:
    acct = engine.account
    latest = {}
    for d in engine.decisions:
        latest[d["horizon_minutes"]] = d
    counts = defaultdict(lambda: defaultdict(int))
    for d in engine.decisions:
        counts[d["horizon_minutes"]][d["action"]] += 1
    open_c = [contract_dict(c) | {"worst_case_loss": c.worst_case_loss} for c in acct.open_contracts]
    calib = {}
    for (h, v), hm in engine.models.items():
        for direction, dm in hm.models.items():
            calib[f"{hm.model_version}/{direction.value}"] = {"status": dm.status.value, **dm.validation,
                                                             "forward_settled": len(dm.forward)}
    payout_sources = {d["payout_available_at_decision"]["source"] for d in engine.decisions
                      if d["payout_available_at_decision"]}
    warnings = list(notes)
    if not payout_sources or all(s.startswith("FIXTURE") for s in payout_sources):
        warnings.append("PLATFORM QUOTE HISTORY MISSING: payouts are fixture assumptions, not platform quotes.")
    return {
        "label": label, "data_mode": acct.data_mode.value, "instrument": engine.instrument,
        "data_source": engine.data_source, "account": acct.summary(), "risk": engine.risk.status(),
        "latest_by_horizon": {str(k): v for k, v in sorted(latest.items())},
        "decision_counts": {str(k): dict(v) for k, v in counts.items()},
        "recent_decisions": engine.decisions[-30:],
        "open_contracts": open_c,
        "alerts": [a.dict() for a in engine.alerts.values() if acct.contracts[a.contract_id].status == "OPEN"],
        "alert_events": [r.dict() for r in acct.records if r.kind == "ALERT"][-30:],
        "exit_capability": engine.exit_capability.dict(),
        "equity_curve": acct.equity_curve,
        "performance": performance(engine),
        "calibration": calib,
        "warnings": warnings,
    }


def build_state(runs: list[dict], quantum: dict | None, benchmark: dict | None) -> dict:
    hist = [r for r in runs if r["data_mode"] == "REPLAY"]
    fwd = [r for r in runs if r["data_mode"] == "LOCAL_PAPER"]
    state = {
        "schema": "paperdesk.state/1",
        "capability_status": execution.CAPABILITY,
        "honesty": ["Paper trading only; no real orders can be sent.",
                    "Results under different data modes are never combined.",
                    "Synthetic fixtures demonstrate mechanics only and are not evidence of any edge.",
                    "The 0.75 probability gate is a model gate, not a promised win rate."],
        "historical_runs": hist, "forward_paper_runs": fwd,
        "forward_paper_note": None if fwd else "No forward paper results: no authorized live data feed was run.",
        "quantum": quantum, "quantum_benchmark": benchmark,
    }
    return json.loads(redact_text(json.dumps(state, default=str)))
