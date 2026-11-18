"""Single Provider interface for Phase 3."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ProviderResult:
    ok: bool
    label: str | None = None
    latency_ms: float = 0.0
    error: str | None = None
    provider_name: str = ""
    status_code: int | None = None
    task_id: str | None = None
    # Confidence of the winning label when the provider reports one. Used by
    # the Phase 5b actuation policy; None means "not reported".
    score: float | None = None


class Provider(Protocol):
    name: str
    endpoint: str

    def health(self) -> bool:
        ...

    def predict(self, jpeg_bytes: bytes, task_context: Any = None) -> ProviderResult:
        ...
