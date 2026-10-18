"""IBM Quantum research integration (Qiskit 2.x + qiskit-ibm-runtime, checked against 0.50).

Configuration (environment or secret manager only; never written to logs or reports):
    IBM_QUANTUM_API_KEY        -> QiskitRuntimeService(token=...)
    IBM_QUANTUM_INSTANCE_CRN   -> QiskitRuntimeService(instance=...)   (CRN or service name)
    IBM_QUANTUM_BACKEND        -> service.backend(name), only after it appears in service.backends()
    IBM_QUANTUM_MAX_QPU_SECONDS-> explicit compute budget; 0/unset keeps QPU submission blocked
Channel: "ibm_quantum_platform" (the legacy "ibm_quantum" channel is retired).

Status is derived from evidence, never assumed:
    DISABLED          no credentials (local simulator still available for research)
    SIMULATOR         a kernel was computed locally (statevector / StatevectorSampler)
    AUTHENTICATED     QiskitRuntimeService accepted the credentials and listed backends
    BACKEND_VERIFIED  the configured backend name is in that accessible list
    JOB_SUBMITTED     a Sampler job id exists for that backend
    JOB_COMPLETED     that job reached DONE and returned results
    FAILED            the last step raised; see `last_error`
A successful login is NOT evidence that a circuit ran on hardware.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .config import QuantumStatus, ValidationStatus, USABLE_VALIDATION

log = logging.getLogger("paperdesk.quantum")
CHANNEL = "ibm_quantum_platform"
QUANTUM_FEATURES = ["rsi_c", "macd_c", "mom5"]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class QuantumConfig:
    api_key: str | None = field(default=None, repr=False)
    instance_crn: str | None = field(default=None, repr=False)
    backend_name: str | None = None
    max_qpu_seconds: float = 0.0
    qpu_submission_enabled: bool = False
    shots: int = 1000
    optimization_level: int = 1
    seed_transpiler: int = 1234

    @classmethod
    def from_env(cls, qpu_submission_enabled: bool = False) -> "QuantumConfig":
        return cls(api_key=os.getenv("IBM_QUANTUM_API_KEY") or None,
                   instance_crn=os.getenv("IBM_QUANTUM_INSTANCE_CRN") or None,
                   backend_name=os.getenv("IBM_QUANTUM_BACKEND") or None,
                   max_qpu_seconds=float(os.getenv("IBM_QUANTUM_MAX_QPU_SECONDS") or 0),
                   qpu_submission_enabled=qpu_submission_enabled)

    def public(self) -> dict:
        return {"api_key_configured": bool(self.api_key), "instance_configured": bool(self.instance_crn),
                "backend_name": self.backend_name, "max_qpu_seconds": self.max_qpu_seconds,
                "qpu_submission_enabled": self.qpu_submission_enabled, "channel": CHANNEL}


# ---- fidelity kernel ------------------------------------------------------------------------
def feature_map(n: int, reps: int = 2):
    from qiskit.circuit.library import zz_feature_map
    return zz_feature_map(n, reps=reps)


def scale(X: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    return np.pi * np.clip((X - lo) / np.where(hi > lo, hi - lo, 1.0), 0, 1)


def kernel_statevector(XA: np.ndarray, XB: np.ndarray, reps: int = 2) -> np.ndarray:
    """Exact fidelity kernel K_ij = |<phi(a_i)|phi(b_j)>|^2 on a local statevector simulator."""
    from qiskit.quantum_info import Statevector
    fm = feature_map(XA.shape[1], reps)
    sa = np.array([Statevector(fm.assign_parameters(x)).data for x in XA])
    sb = np.array([Statevector(fm.assign_parameters(x)).data for x in XB])
    return np.abs(sa.conj() @ sb.T) ** 2


def kernel_circuits(XA: np.ndarray, XB: np.ndarray, reps: int = 2) -> list:
    """Compute-uncompute circuits; P(|0...0>) estimates each kernel entry."""
    fm = feature_map(XA.shape[1], reps)
    out = []
    for a in XA:
        for b in XB:
            qc = fm.assign_parameters(a).compose(fm.assign_parameters(b).inverse())
            qc.measure_all()
            out.append(qc)
    return out


def circuit_hash(circuits: list) -> str:
    from qiskit import qasm3
    h = hashlib.sha256()
    for qc in circuits:
        h.update(qasm3.dumps(qc).encode())
    return h.hexdigest()


def zero_prob(pub_result, n_qubits: int, shots: int) -> float:
    counts = pub_result.data.meas.get_counts()
    return counts.get("0" * n_qubits, 0) / shots


# ---- IBM connector --------------------------------------------------------------------------
class QuantumConnector:
    def __init__(self, cfg: QuantumConfig, service_factory=None):
        self.cfg = cfg
        self._factory = service_factory
        self.service = None
        self.backend = None
        self.status = QuantumStatus.DISABLED
        self.accessible_backends: list[str] = []
        self.last_error: str | None = None
        self.experiments: list[dict] = []
        self._lock = threading.Lock()

    def _fail(self, where: str, e: Exception) -> None:
        self.status = QuantumStatus.FAILED
        msg = str(e)
        for secret in (self.cfg.api_key, self.cfg.instance_crn):
            if secret:
                msg = msg.replace(secret, "[REDACTED]")
        self.last_error = f"{where}: {type(e).__name__}: {msg}"
        log.warning("quantum %s failed: %s", where, type(e).__name__)

    def authenticate(self) -> QuantumStatus:
        if not self.cfg.api_key:
            self.status = QuantumStatus.DISABLED
            self.last_error = "IBM_QUANTUM_API_KEY not set"
            return self.status
        try:
            if self._factory:
                self.service = self._factory(channel=CHANNEL, token=self.cfg.api_key, instance=self.cfg.instance_crn)
            else:
                from qiskit_ibm_runtime import QiskitRuntimeService
                self.service = QiskitRuntimeService(channel=CHANNEL, token=self.cfg.api_key,
                                                    instance=self.cfg.instance_crn)
            self.accessible_backends = sorted(b.name for b in self.service.backends())
            self.status = QuantumStatus.AUTHENTICATED
        except Exception as e:  # noqa: BLE001 - surfaced as FAILED status
            self._fail("authenticate", e)
        return self.status

    def verify_backend(self) -> QuantumStatus:
        if self.status != QuantumStatus.AUTHENTICATED:
            return self.status
        name = self.cfg.backend_name
        if not name:
            self.last_error = ("no IBM_QUANTUM_BACKEND selected; choose one of: "
                               + ", ".join(self.accessible_backends))
            return self.status
        if name not in self.accessible_backends:
            self.status = QuantumStatus.FAILED
            self.last_error = f"backend {name!r} is not accessible to this account/instance"
            return self.status
        try:
            self.backend = self.service.backend(name)
            self.status = QuantumStatus.BACKEND_VERIFIED
        except Exception as e:  # noqa: BLE001
            self._fail("verify_backend", e)
        return self.status

    def submission_blockers(self) -> list[str]:
        b = []
        if not self.cfg.qpu_submission_enabled:
            b.append("qpu_submission_enabled=false")
        if self.cfg.max_qpu_seconds <= 0:
            b.append("no compute budget (IBM_QUANTUM_MAX_QPU_SECONDS)")
        if self.status not in (QuantumStatus.BACKEND_VERIFIED, QuantumStatus.JOB_COMPLETED):
            b.append(f"backend not verified (status {self.status.value})")
        return b

    def submit_kernel(self, XA: np.ndarray, XB: np.ndarray, reps: int = 2) -> dict:
        """Submit a kernel job asynchronously. Returns the provenance record (updated in place)."""
        blockers = self.submission_blockers()
        if blockers:
            raise PermissionError("QPU submission blocked: " + "; ".join(blockers))
        from qiskit.transpiler import generate_preset_pass_manager
        from qiskit_ibm_runtime import SamplerV2
        circuits = kernel_circuits(XA, XB, reps)
        pm = generate_preset_pass_manager(optimization_level=self.cfg.optimization_level, backend=self.backend,
                                          seed_transpiler=self.cfg.seed_transpiler)
        isa = pm.run(circuits)
        rec = {"kind": "hardware", "backend_name": self.backend.name, "job_id": None, "circuit_hash": circuit_hash(circuits),
               "n_circuits": len(circuits), "shots": self.cfg.shots, "submitted_utc": None, "completed_utc": None,
               "transpilation": {"optimization_level": self.cfg.optimization_level,
                                 "seed_transpiler": self.cfg.seed_transpiler, "pass_manager": "generate_preset_pass_manager"},
               "usage_estimate": None, "usage_seconds": None, "budget_seconds": self.cfg.max_qpu_seconds,
               "output_provenance": None, "status": None, "kernel": None, "shape": [len(XA), len(XB)]}
        job = SamplerV2(mode=self.backend).run(isa, shots=self.cfg.shots)
        rec["job_id"] = job.job_id()
        rec["submitted_utc"] = _utc()
        try:
            rec["usage_estimate"] = job.usage_estimation
        except Exception:  # noqa: BLE001 - optional field
            rec["usage_estimate"] = "unavailable"
        with self._lock:
            self.status = QuantumStatus.JOB_SUBMITTED
            rec["status"] = QuantumStatus.JOB_SUBMITTED.value
            self.experiments.append(rec)
        threading.Thread(target=self._wait, args=(job, rec, XA.shape[1]), daemon=True).start()
        return rec

    def _wait(self, job, rec: dict, n_qubits: int) -> None:
        try:
            res = job.result()
            k = np.array([zero_prob(r, n_qubits, self.cfg.shots) for r in res]).reshape(rec["shape"])
            with self._lock:
                rec["kernel"] = k.tolist()
                rec["completed_utc"] = _utc()
                try:
                    rec["usage_seconds"] = job.usage()
                except Exception:  # noqa: BLE001
                    rec["usage_seconds"] = "unavailable"
                rec["output_provenance"] = f"IBM Quantum job {rec['job_id']} on {rec['backend_name']}"
                rec["status"] = QuantumStatus.JOB_COMPLETED.value
                self.status = QuantumStatus.JOB_COMPLETED
        except Exception as e:  # noqa: BLE001
            with self._lock:
                rec["status"] = QuantumStatus.FAILED.value
                self._fail("job", e)

    def simulate_kernel(self, XA: np.ndarray, XB: np.ndarray, reps: int = 2, shots: int | None = None) -> dict:
        """Local simulator kernel with the same circuits. Sets SIMULATOR only when no hardware evidence exists."""
        t0 = _utc()
        if shots:
            from qiskit.primitives import StatevectorSampler
            circuits = kernel_circuits(XA, XB, reps)
            res = StatevectorSampler(seed=7).run(circuits, shots=shots).result()
            k = np.array([zero_prob(r, XA.shape[1], shots) for r in res]).reshape(len(XA), len(XB))
            chash = circuit_hash(circuits)
        else:
            k, chash = kernel_statevector(XA, XB, reps), None
        rec = {"kind": "simulator", "backend_name": "local StatevectorSampler" if shots else "local Statevector (exact)",
               "job_id": None, "circuit_hash": chash, "shots": shots, "submitted_utc": t0, "completed_utc": _utc(),
               "transpilation": None, "usage_estimate": 0, "output_provenance": "local simulation, not hardware",
               "status": QuantumStatus.SIMULATOR.value, "shape": [len(XA), len(XB)]}
        with self._lock:
            self.experiments.append(rec)
            if self.status == QuantumStatus.DISABLED:
                self.status = QuantumStatus.SIMULATOR
        return {"kernel": k, "provenance": rec}

    def evidence(self) -> dict:
        hw = [e for e in self.experiments if e["kind"] == "hardware"]
        done = [e for e in hw if e["status"] == QuantumStatus.JOB_COMPLETED.value and e["job_id"] and e["kernel"]]
        return {"status": self.status.value, "config": self.cfg.public(), "accessible_backends": self.accessible_backends,
                "last_error": self.last_error, "hardware_jobs": [{k: v for k, v in e.items() if k != "kernel"} for e in hw],
                "hardware_jobs_completed": len(done), "simulator_runs": len(self.experiments) - len(hw),
                "submission_blockers": self.submission_blockers(),
                "note": "AUTHENTICATED is a login only; hardware execution requires a JOB_COMPLETED record with a job id."}


# ---- async scoring with deadline ------------------------------------------------------------
@dataclass
class QuantumScore:
    fresh: bool
    validated: bool
    p_by_dir: dict | None
    reason: str
    requested_utc: str
    validation_status: str
    backend: str

    def provenance(self) -> dict:
        return {"fresh": self.fresh, "validated": self.validated, "reason": self.reason,
                "requested_utc": self.requested_utc, "validation_status": self.validation_status,
                "backend": self.backend, "confidence_bonus": 0.0}


class AsyncQuantumScorer:
    """Runs a quantum scoring function off the decision thread. Results that miss the deadline are
    discarded; a late result is never attached to an earlier decision."""

    def __init__(self, score_fn, validation_status: ValidationStatus = ValidationStatus.UNVALIDATED,
                 backend: str = "simulator"):
        self.score_fn = score_fn
        self.validation_status = validation_status
        self.backend = backend
        self._pool = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="qscore")
        self.discarded = 0

    def score_with_deadline(self, x: np.ndarray, now: datetime, deadline_s: float) -> QuantumScore:
        validated = self.validation_status in USABLE_VALIDATION
        fut = self._pool.submit(self.score_fn, x)
        t0 = time.monotonic()
        try:
            p = fut.result(timeout=deadline_s)
            ok = time.monotonic() - t0 <= deadline_s
            reason = "completed within deadline" if ok else "completed after deadline; discarded"
        except cf.TimeoutError:
            fut.cancel()
            self.discarded += 1
            return QuantumScore(False, validated, None, f"timeout after {deadline_s}s; result discarded",
                                now.isoformat(), self.validation_status.value, self.backend)
        except Exception as e:  # noqa: BLE001
            return QuantumScore(False, validated, None, f"error {type(e).__name__}", now.isoformat(),
                                self.validation_status.value, self.backend)
        if not validated:
            reason += "; quantum model not separately validated, not used"
        return QuantumScore(ok, validated, p if ok else None, reason, now.isoformat(),
                            self.validation_status.value, self.backend)
