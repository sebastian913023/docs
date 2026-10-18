# kalshi15: signal engine for Kalshi 15-minute crypto UP/DOWN markets

Outputs **UP / DOWN / NO-TRADE** per 15-minute window. **Signals only. It never places orders.**

## What it does
- Market: Kalshi `KX*15M` series. YES wins if the settlement reference (CF Benchmarks BRTI average) at window close is >= the reference at window open. The engine proxies both with Coinbase 1m closes.
- Core probability: a random-walk model `Phi(z)` with `z = ln(S_t/K) / (sigma_1m * sqrt(minutes_left))`, then a regularised **logistic model** adds vol-scaled momentum, RSI, order-flow proxy, vol regime, time of day and previous-window return.
- Decision: trade only if `p - ask - fee >= min_edge` (default $0.04), size with fractional Kelly (0.25x), hard-capped at 2% of bankroll. Valid only 3-10 minutes after open.
- Validation: strict walk-forward (retrain on past only), Brier/log-loss/AUC vs the random-walk baseline, calibration table, fee-adjusted P&L with bootstrap CI and t-stat. The verdict is `EDGE_SUPPORTED` only if the 95% CI of P&L per contract is above 0 **and** t > 2.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && pytest
python -m kalshi15.cli selftest        # synthetic proof: no false edge on random walk, finds planted edge
```
Secrets (never commit): `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` (PEM file path). Public market data may not need them.

## Real-data workflow (run on your machine)
```bash
python -m kalshi15.cli fetch --asset BTC --start 2026-01-01 --end 2026-10-01 --out data/btc_1m.csv
python -m kalshi15.cli backtest --bars data/btc_1m.csv --minute 5
# True P&L needs real decision-time Kalshi asks: CSV columns window_open,yes_ask,no_ask (dollars)
python -m kalshi15.cli backtest --bars data/btc_1m.csv --minute 5 --market-csv data/kalshi_asks.csv
python -m kalshi15.cli train --bars data/btc_1m.csv --minute 5 --out model.joblib
python -m kalshi15.cli signal --asset BTC --model model.joblib --out signals.jsonl --loop
```
Open `dashboard/index.html` and load `signals.jsonl`.

## Read this before risking money
- No model can guarantee winning signals. Kalshi prices already embed most public information; the backtest exists to tell you honestly whether an edge survives fees.
- Without `--market-csv`, the backtest assumes a random-walk-efficient market. That tests added information, **not** real Kalshi P&L.
- Unverified assumptions to confirm against docs.kalshi.com: base URL, series tickers (ETH/SOL/XRP), price field format, fee formula (`ceil(0.07*P*(1-P))`), and exact BRTI settlement and tie rules.
- Start with paper tracking of live signals before real stakes.

---

# paperdesk: paper trading for 3-, 5- and 15-minute CALL / PUT / ABSTAIN signals

**Paper only.** The package has no live order adapter. `execution.submit_live_order` always raises, and
an engine built with `live_order_execution_enabled=True` refuses to start.

## Capability status (honest)
| Capability | Status |
|---|---|
| `REPLAY` (historical simulation) | Available. The demo runs on **synthetic fixtures** that show how the system works; they are not market evidence. |
| `LOCAL_PAPER` (local simulation on an authorized feed) | Framework available. No live feed is wired in this build, so there are no forward paper results yet. |
| `PLATFORM_DEMO` | **Blocked.** No verified official demo integration exists, and the engine refuses this mode. |
| Platform payout quotes | **Missing.** The demo uses a fixed fixture payout of 0.85. The dashboard shows a warning. |
| Early contract exits | **Unsupported** (`early_exit_supported=false`). Only analytical reference alerts are shown. |
| IBM Quantum hardware | **Not run.** The fidelity kernel runs on a local simulator. QPU submission stays disabled until a backend is verified and a compute budget is set. |

## Run it
```bash
pip install -e ".[dev,quantum]"
python -m pytest                               # acceptance tests
python -m paperdesk.cli demo                   # writes dashboard/state.json (3 replay runs + quantum benchmark)
python -m paperdesk.cli serve                  # open http://127.0.0.1:8765/paper.html
python -m paperdesk.cli quantum-status         # IBM status derived from evidence
python -m paperdesk.cli quantum-benchmark --shots 1000
```

## Risk defaults (`paperdesk/config.py: RiskConfig`)
These are conservative starting points for research. They are not proven or optimal parameters.

| Field | Default | Denominator |
|---|---|---|
| `initial_virtual_balance` | 10000 | — |
| `stake_fraction_of_available_equity` | 0.0025 | available cash (excludes reserved stake) |
| `maximum_total_open_stake_fraction` | 0.01 | settled equity = cash + reserved stake |
| `maximum_simultaneous_contracts` | 3 | — |
| `daily_loss_halt_fraction` | 0.02 | session-start settled equity (first event of the UTC day) |
| `peak_to_trough_drawdown_halt_fraction` | 0.05 | high-water mark of settled equity |
| `daily_profit_pause_fraction` | 0.02 | session-start settled equity |
| `minimum_calibrated_win_probability` | 0.75 | a model gate, not a promised win rate |
| `maximum_correlated_group_stake_fraction` | 0.01 | settled equity (added by this build; it never loosens the total cap) |
| `live_order_execution_enabled` / `qpu_submission_enabled` | false / false | — |

The pre-trade checks count the worst-case loss of every open contract (its full stake) plus the proposed stake.
The checks and the stake reservation run under one lock, so concurrent entries cannot exceed the caps.
Stakes are always a fixed fraction of cash. There is no martingale, no loss-recovery sizing, and no stake escalation.

## Three control layers
- **Account-level** controls block **new entries only**. They cover daily loss, drawdown, the daily profit pause, total and correlated open stake, the kill switch, data-quality halts and model-drift halts. Daily loss, drawdown, kill switch, data and drift halts latch. Clearing one needs the `PAPERDESK_RESET_TOKEN` env value. The profit pause clears at the next UTC day. Open contracts always run to their own settlement.
- **Contract-level exits** are allowed only when all five `ExitCapability` checks are verified. The model covers stale quotes, latency and venue rejection. A failed exit leaves the contract open. Break-even means the recoverable value after costs is at least the stake.
- **Reference alerts** show stop, target and break-even levels labelled `ANALYTICAL ALERT — NOT AN EXECUTABLE CONTRACT EXIT`. They never change booked P&L. Hypothetical exit P&L is kept in a separate field.

## Signal strength vs probability
- `signal_strength_0_to_100` (version `SS-1.0`) is `100*|Σ w_i c_i / Σ w_i|`. It combines RSI, MACD, Fibonacci and volume components (weights .3/.3/.2/.2, formula in `indicators.py`). It is not a probability and cannot pass any gate.
- If actual volume is missing, the engine uses variant `SS-1.0-NOVOL`. That variant has its own trained and validated model, and the volume contribution is shown as `UNAVAILABLE`.
- `calibrated_p_win` comes from a logistic model plus an isotonic calibrator, fitted per horizon and direction. Training, calibration and validation use chronological splits with purge gaps. The status is `HISTORICALLY_VALIDATED` only when the validation set passes all of these checks:
  - n ≥ 400
  - at least 30 samples at or above the gate
  - Brier score better than the base rate
  - ECE ≤ 0.05
  - gated predictions are not overconfident (by the Wilson interval)
- `FORWARD_PAPER_VALIDATED` needs settled `LOCAL_PAPER` outcomes. Replay outcomes never count toward it.
- `probability_uncertainty` is the Wilson 95% interval of the observed validation win rate in the same probability bin. It is `null` when that bin has fewer than 20 samples.
- If no validated probability exists, the decision is ABSTAIN. A random-walk fixture is never validated (see the tests).

## IBM Quantum
These settings come from the environment (or a secret manager) only:

| Variable | Maps to |
|---|---|
| `IBM_QUANTUM_API_KEY` | `token` |
| `IBM_QUANTUM_INSTANCE_CRN` | `instance` |
| `IBM_QUANTUM_BACKEND` | `service.backend(name)`, used only after the name appears in `service.backends()` |
| `IBM_QUANTUM_MAX_QPU_SECONDS` | the compute budget |

The channel is `ibm_quantum_platform`. The code was checked against qiskit 2.6 and qiskit-ibm-runtime 0.50.
The status reflects evidence, step by step: `DISABLED → SIMULATOR → AUTHENTICATED → BACKEND_VERIFIED → JOB_SUBMITTED → JOB_COMPLETED` (or `FAILED`).
A successful login is not evidence that anything ran on hardware.

Each hardware job record keeps:
- backend
- job id
- circuit SHA-256 (QASM3)
- shots
- timestamps
- transpiler settings
- usage estimate and actual usage
- output provenance

QPU scoring runs asynchronously with a deadline. A late result is discarded. The engine then falls back to a separately validated classical model, or ABSTAINs if there is none.
Quantum participation never adds a confidence bonus.
