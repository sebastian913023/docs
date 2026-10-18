"""CLI: demo | quantum-status | quantum-benchmark | serve"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import logging
import sys
from pathlib import Path

from . import benchmark, fixtures, report
from .config import DataMode, RiskConfig
from .engine import Engine, PayoutQuote, fit_models, replay
from .quantum import QuantumConfig, QuantumConnector

DASH = Path(__file__).resolve().parent.parent / "dashboard"
FIXTURE_PAYOUT = 0.85


def fixture_payout(_inst, _h, t):
    return PayoutQuote(FIXTURE_PAYOUT, t, "FIXTURE_ASSUMPTION (no platform quote)")


def replay_run(label: str, bars, cfg: RiskConfig, fit_frac: float = 0.75) -> dict:
    cut = int(len(bars) * fit_frac)
    models = fit_models(bars.iloc[:cut], "SYNTH-USD", cfg)
    eng = Engine("SYNTH-USD", f"synthetic fixture: {label}", DataMode.REPLAY, models, fixture_payout, cfg)
    replay(eng, bars, cut)
    notes = [f"SYNTHETIC FIXTURE ({label}). Mechanics demonstration only; not market evidence.",
             f"Models fit on bars before {bars.index[cut]}; replay is held out from training and calibration."]
    return report.run_state(eng, label, notes)


def connector() -> QuantumConnector:
    qc = QuantumConnector(QuantumConfig.from_env(RiskConfig().qpu_submission_enabled))
    if qc.cfg.api_key:
        qc.authenticate()
        qc.verify_backend()
    return qc


def cmd_demo(a):
    cfg = RiskConfig()
    runs = [replay_run("planted momentum signal", fixtures.bars(seed=7, planted=0.8), cfg),
            replay_run("pure random walk", fixtures.bars(seed=11, planted=0.0), cfg),
            replay_run("planted signal, no volume", fixtures.bars(seed=13, planted=0.8, with_volume=False), cfg)]
    qc = connector()
    bench = benchmark.run(fixtures.bars(seed=7, planted=0.8), qc, horizon=5) if not a.no_quantum else None
    state = report.build_state(runs, qc.evidence(), bench)
    out = Path(a.out)
    out.write_text(json.dumps(state, indent=1, default=str))
    for r in runs:
        acc = r["account"]
        print(f"[{r['data_mode']}] {r['label']}: decisions={r['decision_counts']} "
              f"equity={acc['settled_equity']:.2f} realized={acc['realized_pnl']:.2f} halts={list(r['risk']['halts'])}")
    print(f"quantum status: {state['quantum']['status']}; wrote {out}")


def cmd_qstatus(_a):
    print(json.dumps(report.build_state([], connector().evidence(), None)["quantum"], indent=1))


def cmd_qbench(a):
    qc = connector()
    print(json.dumps(report.build_state([], None, benchmark.run(
        fixtures.bars(seed=a.seed, planted=a.planted), qc, a.horizon, shots=a.shots))["quantum_benchmark"],
        indent=1, default=str))


def cmd_serve(a):
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DASH))
    with http.server.ThreadingHTTPServer(("127.0.0.1", a.port), h) as srv:
        print(f"open http://127.0.0.1:{a.port}/paper.html")
        srv.serve_forever()


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    report.install_log_redaction()
    ap = argparse.ArgumentParser(prog="paperdesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo"); d.add_argument("--out", default=str(DASH / "state.json"))
    d.add_argument("--no-quantum", action="store_true")
    sub.add_parser("quantum-status")
    q = sub.add_parser("quantum-benchmark"); q.add_argument("--horizon", type=int, default=5)
    q.add_argument("--seed", type=int, default=7); q.add_argument("--planted", type=float, default=0.8)
    q.add_argument("--shots", type=int, default=None)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    {"demo": cmd_demo, "quantum-status": cmd_qstatus, "quantum-benchmark": cmd_qbench, "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
