"""Optimized multi-provider router."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List

from phase3_router.providers.base import Provider, ProviderResult

LOG = logging.getLogger("phase3.router")


@dataclass
class RouterResult:
    ok: bool
    label: str | None
    provider: str
    latency_ms: float
    attempts: int
    errors: List[str]
    failover_time_ms: float | None = None


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_sec: float = 10.0):
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self.failures = 0
        self.state = "closed"  # closed/open/half_open
        self.opened_at = 0.0
        self.lock = threading.Lock()

    def allow_request(self) -> bool:
        with self.lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.opened_at >= self.cooldown_sec:
                    self.state = "half_open"
                    return True
                return False
            if self.state == "half_open":
                return True
            return True

    def record_success(self):
        with self.lock:
            self.failures = 0
            self.state = "closed"

    def record_failure(self):
        with self.lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = time.time()


class Router:
    def __init__(self, providers: List[Provider], health_check: bool = True,
                 recheck_interval_sec: float = 5.0, window_size: int = 10,
                 failure_threshold: int = 3, cooldown_sec: float = 10.0,
                 ewma_alpha: float = 0.3):
        if not providers:
            raise ValueError("No providers configured")
        self.providers = providers
        self.health_check = health_check
        self.recheck_interval_sec = float(recheck_interval_sec)
        self.window_size = int(window_size)
        self.ewma_alpha = float(ewma_alpha)
        self._healthy_cache: Dict[str, bool] = {p.name: True for p in providers}
        self._last_check: Dict[str, float] = {p.name: 0.0 for p in providers}
        self._ewma: Dict[str, float] = {p.name: float('inf') for p in providers}
        self._cb: Dict[str, CircuitBreaker] = {p.name: CircuitBreaker(failure_threshold, cooldown_sec) for p in providers}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        if self.health_check:
            self._start_health_thread()

    def _start_health_thread(self):
        t = threading.Thread(target=self._health_loop, daemon=True)
        t.start()
        self._health_thread = t

    def _health_loop(self):
        while not self._stop.is_set():
            for p in self.providers:
                try:
                    ok = bool(p.health())
                except Exception:
                    ok = False
                with self._lock:
                    self._healthy_cache[p.name] = ok
                    self._last_check[p.name] = time.time()
            time.sleep(self.recheck_interval_sec)

    def is_healthy(self, provider: Provider) -> bool:
        if not self.health_check:
            return True
        with self._lock:
            cached = self._healthy_cache.get(provider.name, True)
        return cached

    def _order_providers(self):
        healthy_list = []
        unhealthy_list = []
        for idx, p in enumerate(self.providers):
            if self.is_healthy(p) and self._cb.get(p.name).allow_request():
                healthy_list.append((idx, p))
            else:
                unhealthy_list.append((idx, p))
        healthy_list_sorted = sorted(healthy_list, key=lambda x: (self._ewma.get(x[1].name, float('inf')), x[0]))
        unhealthy_list_sorted = sorted(unhealthy_list, key=lambda x: x[0])
        return [p for _, p in healthy_list_sorted] + [p for _, p in unhealthy_list_sorted]

    def predict(self, jpeg_bytes: bytes) -> RouterResult:
        errors = []
        total_start = time.perf_counter()
        attempts = 0
        failover_start = None
        failover_time_ms = None
        ordered = self._order_providers()
        for idx, provider in enumerate(ordered):
            if self.health_check and not self.is_healthy(provider):
                errors.append(f"{provider.name}:unhealthy")
                continue
            cb = self._cb.get(provider.name)
            if cb and not cb.allow_request():
                errors.append(f"{provider.name}:circuit_open")
                continue
            attempts += 1
            if attempts >= 2 and failover_start is None:
                failover_start = time.perf_counter()
            try:
                res = provider.predict(jpeg_bytes)
                if res.ok:
                    if failover_start is not None:
                        failover_time_ms = round((time.perf_counter() - failover_start) * 1000.0, 3)
                    latency_ms = round((time.perf_counter() - total_start) * 1000.0, 3)
                    if cb:
                        cb.record_success()
                    with self._lock:
                        old = self._ewma.get(provider.name, latency_ms)
                        self._ewma[provider.name] = self.ewma_alpha * latency_ms + (1 - self.ewma_alpha) * old
                    return RouterResult(
                        ok=True,
                        label=res.label,
                        provider=res.provider_name or provider.name,
                        latency_ms=latency_ms,
                        attempts=attempts,
                        errors=errors,
                        failover_time_ms=failover_time_ms,
                    )
                else:
                    errors.append(f"{provider.name}:{res.error or 'failed'}")
                    if cb:
                        cb.record_failure()
                    continue
            except Exception as e:
                errors.append(f"{provider.name}:{e}")
                if cb:
                    cb.record_failure()
                continue
        latency_ms = round((time.perf_counter() - total_start) * 1000.0, 3)
        return RouterResult(ok=False, label=None, provider="none", latency_ms=latency_ms, attempts=attempts, errors=errors, failover_time_ms=failover_time_ms)
