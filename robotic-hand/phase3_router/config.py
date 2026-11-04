"""Configuration for Phase 3 router."""
from __future__ import annotations

import os


def get_recheck_interval_sec() -> float:
    try:
        return float(os.getenv("ROUTER_RECHECK_INTERVAL_SEC", "5.0"))
    except Exception:
        return 5.0


def get_latency_window_size() -> int:
    try:
        return int(os.getenv("ROUTER_LATENCY_WINDOW", "10"))
    except Exception:
        return 10
