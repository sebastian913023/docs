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
