"""Execution boundary. This build has NO live order adapter and NO platform demo adapter.

`submit_live_order` always raises, whatever the config says, so no real-money order can be sent.
"""
from __future__ import annotations

from .config import DataMode, RiskConfig


class LiveExecutionForbidden(RuntimeError):
    pass


class PlatformDemoUnavailable(RuntimeError):
    pass


CAPABILITY = {
    "live_order_execution": "NOT_IMPLEMENTED (by design; paper only)",
    "platform_demo": "BLOCKED: no verified official demo integration exists in this build",
    "local_paper": "AVAILABLE with an authorized data feed you supply",
    "replay": "AVAILABLE",
}


def submit_live_order(*_args, **_kwargs):
    raise LiveExecutionForbidden("paperdesk cannot send real orders; no live adapter exists")


def assert_paper_only(cfg: RiskConfig, mode: DataMode) -> None:
    if cfg.live_order_execution_enabled:
        raise LiveExecutionForbidden("live_order_execution_enabled=True is refused by this paper-only build")
    if mode == DataMode.PLATFORM_DEMO:
        raise PlatformDemoUnavailable(CAPABILITY["platform_demo"])
