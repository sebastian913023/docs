"""CLI: fetch | backtest | train | signal | selftest"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd

from . import backtest, data, features, kalshi, model
from .config import ASSETS, Config
from .signal import decide
from .synth import make_bars


def _print_report(rep: dict) -> None:
    pm, tr = rep["probability"], rep["trading"]
    print("\n== Probability quality (out-of-sample) ==")
    for k, v in pm.items():
        print(f"  {k:22s} {v:.4f}" if isinstance(v, float) else f"  {k:22s} {v}")
    print("\n== Calibration ==")
    print(rep["calibration"].round(3).to_string(index=False))
    print(f"\n== Trading (market prices: {rep['market_source']}) ==")
    for k, v in tr.items():
        print(f"  {k:26s} {v}")
    if rep["market_source"] != "real":
        print("\nNOTE: no real Kalshi prices supplied. The simulated market is random-walk-efficient,\n"
              "so this measures information beyond a pure random walk, NOT real Kalshi P&L.\n"
              "Supply --market-csv with real decision-time asks for a true result.")


def cmd_fetch(a, cfg):
    df = data.fetch_candles(a.asset, a.start, a.end, cfg)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out)
    print(f"saved {len(df)} bars to {a.out}")


def cmd_backtest(a, cfg):
    bars = data.load_csv(a.bars)
    tbl = features.window_table(bars, a.minute, cfg.vol_span)
    p = model.walk_forward(tbl, cfg.train_min_windows, cfg.retrain_every)
    mk = kalshi.load_market_csv(a.market_csv) if a.market_csv else None
    _print_report(backtest.run(tbl, p, cfg, mk))


def cmd_train(a, cfg):
    bars = data.load_csv(a.bars)
    tbl = features.window_table(bars, a.minute, cfg.vol_span)
    mdl = model.fit(tbl)
    joblib.dump({"model": mdl, "minute": a.minute, "trained_rows": len(tbl)}, a.out)
    print(f"trained on {len(tbl)} windows -> {a.out}")


def live_once(asset: str, art: dict, cfg: Config, series: str | None):
    bars = data.recent_candles(asset, 420, cfg)
    now = pd.Timestamp.now(tz="UTC").floor("1min")
    bars = bars[bars.index < now]                       # closed bars only
    t0 = now.floor(f"{cfg.window_min}min")
    m = int((now - t0).total_seconds() // 60)
    if m < 1:
        return None
    bf = features.bar_features(bars, cfg.vol_span)
    import numpy as np
    pos0 = np.array([bars.index.get_loc(t0)])
    X = features.assemble(bf, pos0, m)
    p = float(art["model"].predict_proba(X[features.FEATURES].to_numpy())[:, 1][0])
    q = kalshi.current_window_quote(asset, t0, cfg, series)
    return decide(asset, t0.isoformat(), m, p, q and q["yes_ask"], q and q["no_ask"], cfg, q and q["ticker"])


def cmd_signal(a, cfg):
    art = joblib.load(a.model)
    while True:
        sig = live_once(a.asset, art, cfg, a.series)
        if sig:
            line = json.dumps(sig.dict())
            print(line, flush=True)
            if a.out:
                with open(a.out, "a") as f:
                    f.write(line + "\n")
        if not a.loop:
            break
        time.sleep(30)


def cmd_selftest(a, cfg):
    for strength in (0.0, a.strength):
        print(f"\n######## synthetic data, planted signal strength = {strength} ########")
        bars = make_bars(60 * 24 * 90, seed=3, signal_strength=strength)
        tbl = features.window_table(bars, a.minute, cfg.vol_span)
        p = model.walk_forward(tbl, cfg.train_min_windows, cfg.retrain_every)
        _print_report(backtest.run(tbl, p, cfg))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kalshi15")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("--asset", default="BTC", choices=list(ASSETS))
    f.add_argument("--start", required=True); f.add_argument("--end", required=True); f.add_argument("--out", required=True)
    b = sub.add_parser("backtest"); b.add_argument("--bars", required=True); b.add_argument("--minute", type=int, default=5)
    b.add_argument("--market-csv")
    t = sub.add_parser("train"); t.add_argument("--bars", required=True); t.add_argument("--minute", type=int, default=5)
    t.add_argument("--out", default="model.joblib")
    s = sub.add_parser("signal"); s.add_argument("--asset", default="BTC", choices=list(ASSETS))
    s.add_argument("--model", default="model.joblib"); s.add_argument("--series"); s.add_argument("--out")
    s.add_argument("--loop", action="store_true")
    st = sub.add_parser("selftest"); st.add_argument("--minute", type=int, default=5); st.add_argument("--strength", type=float, default=0.15)
    a = ap.parse_args(argv)
    {"fetch": cmd_fetch, "backtest": cmd_backtest, "train": cmd_train,
     "signal": cmd_signal, "selftest": cmd_selftest}[a.cmd](a, Config())


if __name__ == "__main__":
    sys.exit(main())
