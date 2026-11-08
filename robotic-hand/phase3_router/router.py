"""Optimized multi-provider router."""
from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from phase3_router.providers.base import Provider, ProviderResult

LOG = logging.getLogger("phase3.router")

DEFAULT_MIN_ATTEMPT_TIMEOUT_SEC = 0.2


@dataclass
class RouterResult:
    ok: bool
    label: str | None
    provider: str
    latency_ms: float
    attempts: int
    errors: List[str]
    failover_time_ms: float | None = None
    task_id: str | None = None
    decision: str | None = None
    attempt_timeouts_ms: List[float] | None = None


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


def _accepts_kwarg(fn: Any, name: str) -> bool:
    """Return True when ``fn`` can be called with keyword ``name``.

    Used instead of wrapping calls in ``except TypeError``: that pattern also
    catches TypeError raised *inside* the provider (for example by a bad
    string format) and silently retries the same slow provider.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class Router:
    def __init__(self, providers: List[Provider], health_check: bool = True,
                 recheck_interval_sec: float = 5.0, window_size: int = 10,
                 failure_threshold: int = 3, cooldown_sec: float = 10.0,
                 ewma_alpha: float = 0.3, conflict_store: Any = None,
                 min_attempt_timeout_sec: float = DEFAULT_MIN_ATTEMPT_TIMEOUT_SEC):
        if not providers:
            raise ValueError("No providers configured")
        self.providers = providers
        self.health_check = health_check
        self.recheck_interval_sec = float(recheck_interval_sec)
        self.window_size = int(window_size)
        self.ewma_alpha = float(ewma_alpha)
        self.conflict_store = conflict_store
        self.min_attempt_timeout_sec = float(min_attempt_timeout_sec)
        self._healthy_cache: Dict[str, bool] = {p.name: True for p in providers}
        self._last_check: Dict[str, float] = {p.name: 0.0 for p in providers}
        self._ewma: Dict[str, float] = {p.name: float('inf') for p in providers}
        self._cb: Dict[str, CircuitBreaker] = {p.name: CircuitBreaker(failure_threshold, cooldown_sec) for p in providers}
        self._ctx_ok: Dict[str, bool] = {}
        self._timeout_ok: Dict[str, bool] = {}
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

    def _attempt_timeout(self, deadline: float, remaining_providers: int) -> float | None:
        """Divide the remaining deadline across the providers still to try.

        The floor keeps an almost-expired deadline from producing a timeout so
        small that even a healthy local provider cannot answer.
        """
        if not deadline or remaining_providers <= 0:
            return None
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        return max(self.min_attempt_timeout_sec, remaining / remaining_providers)

    def _supports_context(self, provider: Provider) -> bool:
        cached = self._ctx_ok.get(provider.name)
        if cached is None:
            cached = _accepts_kwarg(provider.predict, "task_context")
            self._ctx_ok[provider.name] = cached
        return cached

    def _supports_timeout(self, provider: Provider) -> bool:
        cached = self._timeout_ok.get(provider.name)
        if cached is None:
            cached = _accepts_kwarg(provider.predict, "timeout")
            self._timeout_ok[provider.name] = cached
        return cached

    def _call_provider(self, provider: Provider, jpeg_bytes: bytes, task_context: Any,
                       attempt_timeout: float | None) -> ProviderResult:
        kwargs: dict[str, Any] = {}
        if task_context is not None and self._supports_context(provider):
            kwargs["task_context"] = task_context
        if attempt_timeout is not None and self._supports_timeout(provider):
            kwargs["timeout"] = attempt_timeout
        return provider.predict(jpeg_bytes, **kwargs)

    def predict(self, jpeg_bytes: bytes, task_context: Any = None) -> RouterResult:
        errors: List[str] = []
        total_start = time.perf_counter()
        attempts = 0
        failover_start = None
        failover_time_ms = None
        attempt_timeouts_ms: List[float] = []
        ordered = self._order_providers()
        req_task_id = None
        deadline = 0.0
        if task_context is not None:
            if isinstance(task_context, dict):
                req_task_id = task_context.get("task_id") or task_context.get("message_id")
                deadline = float(task_context.get("deadline", 0.0) or 0.0)
            else:
                req_task_id = getattr(task_context, "task_id", None)
                deadline = float(getattr(task_context, "deadline", 0.0) or 0.0)
        if deadline and time.time() >= deadline:
            return RouterResult(
                ok=False, label=None, provider="none",
                latency_ms=round((time.perf_counter() - total_start) * 1000.0, 3),
                attempts=0, errors=["deadline_passed"], failover_time_ms=None,
                task_id=str(req_task_id) if req_task_id is not None else None,
                decision="safe_default", attempt_timeouts_ms=[],
            )
        for idx, provider in enumerate(ordered):
            if self.health_check and not self.is_healthy(provider):
                errors.append(f"{provider.name}:unhealthy")
                continue
            cb = self._cb.get(provider.name)
            if cb and not cb.allow_request():
                errors.append(f"{provider.name}:circuit_open")
                continue
            attempt_timeout = self._attempt_timeout(deadline, len(ordered) - idx)
            if attempt_timeout is not None:
                if deadline and time.time() >= deadline:
                    errors.append("deadline_passed_before_attempt")
                    break
                attempt_timeouts_ms.append(round(attempt_timeout * 1000.0, 3))
            attempts += 1
            if attempts >= 2 and failover_start is None:
                failover_start = time.perf_counter()
            try:
                res = self._call_provider(provider, jpeg_bytes, task_context, attempt_timeout)
                if res.ok:
                    # Phase 4: reject responses whose task_id does not match the request
                    if req_task_id is not None:
                        echoed = getattr(res, "task_id", None)
                        if echoed is not None and str(echoed) != str(req_task_id):
                            errors.append(f"{provider.name}:mismatch_task_id")
                            if self.conflict_store is not None:
                                try:
                                    self.conflict_store.record_discard(str(req_task_id), provider.name, "mismatch")
                                except Exception:
                                    LOG.warning("Failed to record mismatch discard for %s", req_task_id)
                            if cb:
                                cb.record_failure()
                            continue
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
                        task_id=str(req_task_id) if req_task_id is not None else getattr(res, "task_id", None),
                        decision=res.label,
                        attempt_timeouts_ms=attempt_timeouts_ms,
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
        return RouterResult(ok=False, label=None, provider="none", latency_ms=latency_ms,
                            attempts=attempts, errors=errors, failover_time_ms=failover_time_ms,
                            task_id=str(req_task_id) if req_task_id is not None else None,
                            attempt_timeouts_ms=attempt_timeouts_ms)
