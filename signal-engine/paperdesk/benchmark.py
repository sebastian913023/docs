"""Small fidelity-kernel experiment benchmarked against classical models on the SAME chronological split.

The quantum kernel gets no confidence bonus; its numbers are reported side by side and it is not
wired into live signals unless it is separately validated.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score
from sklearn.svm import SVC

from .model import labeled_table
from .quantum import QUANTUM_FEATURES, QuantumConnector, scale


def _platt(train_scores: np.ndarray, ytr: np.ndarray, test_scores: np.ndarray) -> np.ndarray:
    """Identical Platt scaling for both SVCs, fit on train decision values only."""
    lr = LogisticRegression().fit(train_scores.reshape(-1, 1), ytr)
    return lr.predict_proba(test_scores.reshape(-1, 1))[:, 1]


def split(bars: pd.DataFrame, horizon: int, n_train: int, n_test: int, stride: int):
    t = labeled_table(bars, horizon, "SS-1.0-NOVOL").iloc[::stride]   # stride >= horizon: no label overlap
    tr, te = t.iloc[:n_train], t.iloc[n_train + 1:n_train + 1 + n_test]    # one purge row between
    return tr, te


def run(bars: pd.DataFrame, connector: QuantumConnector, horizon: int = 5, n_train: int = 120,
        n_test: int = 80, stride: int | None = None, shots: int | None = None) -> dict:
    stride = stride or max(horizon, 5)
    tr, te = split(bars, horizon, n_train, n_test, stride)
    Xtr, Xte = tr[QUANTUM_FEATURES].to_numpy(), te[QUANTUM_FEATURES].to_numpy()
    ytr, yte = tr["y_call"].to_numpy().astype(int), te["y_call"].to_numpy().astype(int)
    lo, hi = Xtr.min(axis=0), Xtr.max(axis=0)                          # scaler fit on train only
    Str, Ste = scale(Xtr, lo, hi), scale(Xte, lo, hi)
    out = {"horizon": horizon, "features": QUANTUM_FEATURES, "n_train": len(tr), "n_test": len(te),
           "train_range": [str(tr.index[0]), str(tr.index[-1])], "test_range": [str(te.index[0]), str(te.index[-1])],
           "stride_minutes": stride, "models": {}}

    def score(name, p, extra=None):
        out["models"][name] = {"accuracy": float(accuracy_score(yte, p >= 0.5)),
                               "auc": float(roc_auc_score(yte, p)) if len(set(yte)) > 1 else None,
                               "brier": float(brier_score_loss(yte, p)), **(extra or {})}

    lr = LogisticRegression(max_iter=1000).fit(Str, ytr)
    score("classical_logistic", lr.predict_proba(Ste)[:, 1])
    rbf = SVC(kernel="rbf").fit(Str, ytr)
    score("classical_rbf_svc", _platt(rbf.decision_function(Str), ytr, rbf.decision_function(Ste)))
    ktr = connector.simulate_kernel(Str, Str, shots=shots)
    kte = connector.simulate_kernel(Ste, Str, shots=shots)
    q = SVC(kernel="precomputed").fit(ktr["kernel"], ytr)
    score("quantum_fidelity_kernel_svc", _platt(q.decision_function(ktr["kernel"]), ytr, q.decision_function(kte["kernel"])),
          {"provenance": kte["provenance"]})
    out["base_rate_test"] = float(yte.mean())
    out["confidence_bonus_for_quantum"] = 0.0
    out["verdict"] = ("Research only. Small-sample comparison on the identical chronological split; "
                      "not a validation and not evidence of any market edge.")
    return out
