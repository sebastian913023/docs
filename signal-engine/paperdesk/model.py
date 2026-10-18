"""Per-horizon directional models with out-of-sample calibration and validation status.

For every (instrument, horizon, direction, variant) a logistic model is trained on the earliest
chronological split, an isotonic calibrator is fit on the next split, and validation statistics are
computed on a third split that neither saw. Purge gaps of `horizon` bars separate splits so labels
never overlap. Settlement definition: strike = close of the decision bar; settlement = close of the
bar ending at expiration; CALL wins if settlement > strike, PUT wins if settlement < strike;
a tie is resolved by RiskConfig.tie_rule (default LOSS for both directions).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .config import Action, ValidationStatus
from .indicators import components

FEATURES = {"SS-1.0": ["rsi_c", "macd_c", "fib_c", "mom5", "volume_c"],
            "SS-1.0-NOVOL": ["rsi_c", "macd_c", "fib_c", "mom5"]}
MODEL_FAMILY = "LR-ISO-1.0"
SPLITS = (0.45, 0.20, 0.35)            # train, calibrate, validate (fractions of the fit range)
MIN_VALID = 400
MIN_GATE = 30
MAX_ECE = 0.05
UNC_BIN = 0.05
UNC_MIN_N = 20


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    a = z * np.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (float((c - a) / d), float((c + a) / d))


def labeled_table(bars: pd.DataFrame, horizon: int, variant: str) -> pd.DataFrame:
    comp = components(bars)
    cols = FEATURES[variant]
    t = comp[cols].copy()
    close = bars["close"]
    fut = close.shift(-horizon)
    contiguous = (bars.index.to_series().shift(-horizon) - bars.index.to_series()) == pd.Timedelta(minutes=horizon)
    t["y_call"] = (fut > close).astype(float)
    t["y_put"] = (fut < close).astype(float)
    t.loc[fut.isna() | ~contiguous, ["y_call", "y_put"]] = np.nan
    return t.dropna()


def _ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    e = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            e += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(e)


@dataclass
class DirectionalModel:
    direction: Action
    base: object
    calibrator: IsotonicRegression
    validation: dict
    status: ValidationStatus
    unc_table: dict = field(default_factory=dict)     # bin_lo -> (n, k)
    forward: list = field(default_factory=list)       # [(p, won)] settled LOCAL_PAPER only

    def p_win(self, X: np.ndarray) -> np.ndarray:
        raw = self.base.predict_proba(X)[:, 1]
        return np.clip(self.calibrator.predict(raw), 0.0, 1.0)

    def uncertainty(self, p: float) -> dict | None:
        lo = float(np.floor(p / UNC_BIN) * UNC_BIN)
        n, k = self.unc_table.get(round(lo, 2), (0, 0))
        if n < UNC_MIN_N:
            return None
        a, b = wilson(k, n)
        return {"estimator": "Wilson 95% interval of the observed validation win rate in the "
                             f"calibrated-probability bin [{lo:.2f}, {lo + UNC_BIN:.2f})",
                "interval": [round(a, 4), round(b, 4)], "bin_n": n,
                "limitations": "assumes the validation period is representative and outcomes are "
                               "independent; overlapping contracts and regime change are not modelled; "
                               "on synthetic fixtures it says nothing about real markets"}


def fit_direction(t: pd.DataFrame, variant: str, direction: Action, horizon: int, threshold: float) -> DirectionalModel:
    cols = FEATURES[variant]
    ycol = "y_call" if direction == Action.CALL else "y_put"
    n = len(t)
    a, b = int(n * SPLITS[0]), int(n * (SPLITS[0] + SPLITS[1]))
    tr, ca, va = t.iloc[:a - horizon], t.iloc[a:b - horizon], t.iloc[b:]
    base = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=1000))
    base.fit(tr[cols].to_numpy(), tr[ycol].to_numpy())
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(base.predict_proba(ca[cols].to_numpy())[:, 1], ca[ycol].to_numpy())
    dm = DirectionalModel(direction, base, iso, {}, ValidationStatus.UNVALIDATED)
    p = dm.p_win(va[cols].to_numpy())
    y = va[ycol].to_numpy()
    for lo in np.arange(0, 1, UNC_BIN):
        m = (p >= lo) & (p < lo + UNC_BIN)
        dm.unc_table[round(float(lo), 2)] = (int(m.sum()), int(y[m].sum()))
    gate = p >= threshold
    ng, kg = int(gate.sum()), int(y[gate].sum())
    ci = wilson(kg, ng)
    base_brier = float(np.mean((y.mean() - y) ** 2)) if len(y) else float("nan")
    brier = float(np.mean((p - y) ** 2)) if len(y) else float("nan")
    v = {"n_validation": int(len(y)), "brier": brier, "brier_base_rate": base_brier, "ece": _ece(p, y) if len(y) else None,
         "gate_threshold": threshold, "gate_n": ng, "gate_mean_predicted": float(p[gate].mean()) if ng else None,
         "gate_observed_win_rate": float(kg / ng) if ng else None, "gate_wilson95": list(ci) if ng else None,
         "validation_start": str(va.index[0]) if len(va) else None,
         "validation_end": str(va.index[-1]) if len(va) else None,
         "reliability": [{"bin": round(float(lo), 2), "n": nn, "observed": (kk / nn) if nn else None}
                         for lo, (nn, kk) in dm.unc_table.items() if nn]}
    reasons = []
    if len(y) < MIN_VALID:
        status = ValidationStatus.INSUFFICIENT_DATA
        reasons.append(f"validation n {len(y)} < {MIN_VALID}")
    elif ng < MIN_GATE:
        status = ValidationStatus.INSUFFICIENT_DATA
        reasons.append(f"only {ng} validation samples at p_win >= {threshold}")
    else:
        status = ValidationStatus.HISTORICALLY_VALIDATED
        if not brier < base_brier:
            status = ValidationStatus.REJECTED
            reasons.append("Brier not better than base rate")
        if v["ece"] > MAX_ECE:
            status = ValidationStatus.REJECTED
            reasons.append(f"ECE {v['ece']:.3f} > {MAX_ECE}")
        if ci[1] < v["gate_mean_predicted"]:
            status = ValidationStatus.REJECTED
            reasons.append("gated predictions overconfident (Wilson upper bound below mean predicted)")
    v["status_reasons"] = reasons
    dm.validation, dm.status = v, status
    return dm


@dataclass
class HorizonModel:
    instrument: str
    horizon: int
    variant: str
    models: dict                 # Action -> DirectionalModel
    training_cutoff_utc: str

    @property
    def model_version(self) -> str:
        return f"{MODEL_FAMILY}/{self.variant}/h{self.horizon}"

    @property
    def settlement_definition(self) -> str:
        return (f"{self.horizon}m binary: strike=decision-bar close, settle=close of bar ending at expiry; "
                "CALL wins if settle>strike, PUT wins if settle<strike")


def fit_horizon(bars: pd.DataFrame, instrument: str, horizon: int, variant: str, threshold: float) -> HorizonModel:
    t = labeled_table(bars, horizon, variant)
    models = {d: fit_direction(t, variant, d, horizon, threshold) for d in (Action.CALL, Action.PUT)}
    cutoff = t.index[int(len(t) * (SPLITS[0] + SPLITS[1])) - horizon - 1]
    return HorizonModel(instrument, horizon, variant, models, str(cutoff))


def promote_forward(dm: DirectionalModel, threshold: float, margin: float = 0.05) -> ValidationStatus:
    """Forward-paper promotion uses only settled LOCAL_PAPER outcomes (never REPLAY)."""
    if dm.status != ValidationStatus.HISTORICALLY_VALIDATED:
        return dm.status
    gated = [(p, w) for p, w in dm.forward if p >= threshold]
    if len(gated) < MIN_GATE:
        return dm.status
    k = sum(w for _, w in gated)
    lo, hi = wilson(k, len(gated))
    mean_p = float(np.mean([p for p, _ in gated]))
    fb = float(np.mean([(p - w) ** 2 for p, w in dm.forward]))
    if lo <= mean_p <= hi and fb <= dm.validation["brier"] + margin:
        dm.status = ValidationStatus.FORWARD_PAPER_VALIDATED
    return dm.status
