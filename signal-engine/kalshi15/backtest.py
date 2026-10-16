"""Out-of-sample evaluation: probability quality, then fee-adjusted trading simulation with a
significance gate. Nothing here is tuned on the evaluation rows."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from .config import Config
from .fees import taker_fee_vec


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    a = z * np.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return ((c - a) / d, (c + a) / d)


def prob_metrics(y: np.ndarray, p: np.ndarray, p0: np.ndarray) -> dict:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    p0 = np.clip(p0, 1e-6, 1 - 1e-6)
    return {
        "n": int(len(y)),
        "base_rate": float(y.mean()),
        "brier_model": float(brier_score_loss(y, p)),
        "brier_random_walk": float(brier_score_loss(y, p0)),
        "brier_coinflip": 0.25,
        "logloss_model": float(log_loss(y, p)),
        "logloss_random_walk": float(log_loss(y, p0)),
        "auc_model": float(roc_auc_score(y, p)),
        "auc_random_walk": float(roc_auc_score(y, p0)),
    }


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 10) -> pd.DataFrame:
    b = pd.cut(p, np.linspace(0, 1, bins + 1), include_lowest=True)
    df = pd.DataFrame({"y": y, "p": p, "bin": b})
    g = df.groupby("bin", observed=True).agg(n=("y", "size"), mean_p=("p", "mean"), win_rate=("y", "mean"))
    return g.reset_index(drop=True)


def attach_market(df: pd.DataFrame, cfg: Config, market: pd.DataFrame | None) -> pd.DataFrame:
    """Add yes_ask/no_ask. Real prices if supplied (index=window open UTC, cols yes_ask,no_ask in
    dollars); else an assumed random-walk-efficient market with `assumed_spread`."""
    df = df.copy()
    if market is not None:
        mk = market[["yes_ask", "no_ask"]]
        df = df.join(mk, how="inner")
        df["market_source"] = "real"
    else:
        h = cfg.assumed_spread / 2
        df["yes_ask"] = np.clip(df["p0"] + h, 0.01, 0.99)
        df["no_ask"] = np.clip(1 - df["p0"] + h, 0.01, 0.99)
        df["market_source"] = "assumed_random_walk"
    return df


def simulate(df: pd.DataFrame, cfg: Config, p_col: str = "p_model") -> pd.DataFrame:
    """Per-window trades: buy the side whose after-fee edge >= cfg.min_edge (best side wins ties)."""
    p = df[p_col].to_numpy()
    ya, na = df["yes_ask"].to_numpy(), df["no_ask"].to_numpy()
    e_yes = p - ya - taker_fee_vec(ya, cfg.fee_rate)
    e_no = (1 - p) - na - taker_fee_vec(na, cfg.fee_rate)
    conf_ok = np.abs(p - 0.5) >= cfg.min_confidence
    side = np.where((e_yes >= e_no) & (e_yes >= cfg.min_edge) & conf_ok, 1,
                    np.where((e_no > e_yes) & (e_no >= cfg.min_edge) & conf_ok, -1, 0))
    y = df["y"].to_numpy()
    price = np.where(side == 1, ya, na)
    win = np.where(side == 1, y == 1, y == 0)
    pnl = np.where(side == 0, 0.0, np.where(win, 1 - price, -price) - taker_fee_vec(price, cfg.fee_rate))
    out = df[["y"]].copy()
    out["side"], out["price"], out["pnl"], out["win"] = side, price, pnl, win
    return out[out["side"] != 0]


def trade_stats(t: pd.DataFrame, seed: int = 7, n_boot: int = 4000) -> dict:
    n = len(t)
    if n < 30:
        return {"trades": n, "verdict": "INSUFFICIENT_TRADES (<30)"}
    pnl = t["pnl"].to_numpy()
    mean, sd = pnl.mean(), pnl.std(ddof=1)
    tstat = mean / (sd / np.sqrt(n)) if sd > 0 else 0.0
    rng = np.random.default_rng(seed)
    boots = rng.choice(pnl, size=(n_boot, n), replace=True).mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    wlo, whi = wilson(int(t["win"].sum()), n)
    eq = pnl.cumsum()
    mdd = float((eq - np.maximum.accumulate(eq)).min())
    verdict = ("EDGE_SUPPORTED" if (lo > 0 and tstat > 2.0) else
               "NO_SIGNIFICANT_EDGE")
    return {"trades": n, "hit_rate": float(t["win"].mean()), "hit_rate_ci95": (float(wlo), float(whi)),
            "avg_pnl_per_contract": float(mean), "pnl_ci95": (float(lo), float(hi)),
            "total_pnl_per_contract": float(pnl.sum()), "t_stat": float(tstat),
            "max_drawdown_contracts": mdd, "verdict": verdict}


def run(table: pd.DataFrame, p_model: pd.Series, cfg: Config = Config(),
        market: pd.DataFrame | None = None) -> dict:
    df = table.join(p_model).dropna(subset=["p_model"])
    pm = prob_metrics(df["y"].to_numpy(), df["p_model"].to_numpy(), df["p0"].to_numpy())
    cal = calibration(df["y"].to_numpy(), df["p_model"].to_numpy())
    dm = attach_market(df, cfg, market)
    trades = simulate(dm, cfg)
    stats = trade_stats(trades)
    # Multiple-testing guard: report share of windows traded so sparse "lucky" subsets are visible.
    stats["share_of_windows_traded"] = float(len(trades) / max(len(dm), 1))
    return {"probability": pm, "calibration": cal, "trading": stats,
            "market_source": dm["market_source"].iloc[0] if len(dm) else "none", "trades": trades}
