"""Cloud inference client for the Phase 1 pipeline.

One request = one raw JPEG POST with a timeout. The round trip is measured
client-side with ``time.perf_counter`` and failures are returned as structured
results instead of exceptions, so one bad request never stops a benchmark run
and every attempt can still be logged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import requests

ERROR_TIMEOUT = "timeout"
ERROR_CONNECTION = "connection"
ERROR_HTTP = "http"
ERROR_REQUEST = "request"
ERROR_INVALID_RESPONSE = "invalid_response"


class SupportsPredict(Protocol):
    """Anything that can classify JPEG bytes (the mock server, a provider)."""

    def predict(self, jpeg_bytes: bytes, image_name: str = ...) -> "InferenceResult":
        ...


@dataclass(frozen=True)
class Prediction:
    """A usable classification returned by the endpoint."""

    label: str
    confidence: float | None = None
    model: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InferenceResult:
    """Outcome of one request, successful or not.

    Attributes:
        ok: True only when a usable prediction arrived.
        status_code: HTTP status, or None if no response was received.
        latency_ms: Measured round trip in milliseconds (always set).
        prediction: Parsed prediction when ``ok`` is True.
        error_kind: One of the ``ERROR_*`` constants when ``ok`` is False.
        error_message: Short, secret-free description of a failure.
        sent_at: UTC ISO-8601 timestamp of when the request started.
        image_name: Human-readable name of the frame that was sent.
    """

    ok: bool
    status_code: int | None
    latency_ms: float
    prediction: Prediction | None
    error_kind: str | None
    error_message: str | None
    sent_at: str
    image_name: str = "frame"

    @property
    def label(self) -> str | None:
        """Return the predicted label, if any."""
        return self.prediction.label if self.prediction else None

    @property
    def confidence(self) -> float | None:
        """Return the predicted confidence, if any."""
        return self.prediction.confidence if self.prediction else None

    def within_target(self, target_ms: float) -> bool:
        """Return True when this successful response met the latency target."""
        return self.ok and self.latency_ms <= target_ms


def _parse_prediction(payload: Any) -> Prediction | None:
    """Parse a response body into a :class:`Prediction`.

    Accepts the mock server shape ``{"label": str, "confidence": float, ...}``
    and the list shape used by hosted classifiers:
    ``[{"label": str, "score": float}, ...]``. Returns None if neither fits.
    """

    if isinstance(payload, list):
        if not payload:
            return None
        payload = payload[0]
    if not isinstance(payload, dict):
        return None

    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        return None

    score: Any = payload.get("confidence")
    if score is None:
        score = payload.get("score")
    try:
        confidence = float(score) if score is not None else None
    except (TypeError, ValueError):
        confidence = None

    model = payload.get("model")
    return Prediction(
        label=label.strip(),
        confidence=confidence,
        model=str(model) if model is not None else None,
        extra={k: v for k, v in payload.items() if k not in {"label", "score", "confidence", "model"}},
    )


class CloudClient:
    """POST JPEG frames to an inference endpoint and measure the round trip.

    Args:
        endpoint: Full URL, e.g. ``http://127.0.0.1:8000/predict``.
        timeout_seconds: Timeout applied to connect and read phases.
        api_key: Optional bearer token. Never logged.
        session: Optional pre-built ``requests.Session`` (useful in tests).
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 10.0,
        api_key: str = "",
        session: requests.Session | None = None,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must start with http:// or https://")
        self.endpoint = endpoint
        self.timeout_seconds = float(timeout_seconds)
        self._api_key = api_key
        self._session = session or requests.Session()
        self._owns_session = session is None

    def __repr__(self) -> str:
        return f"CloudClient(endpoint={self.endpoint!r}, timeout={self.timeout_seconds})"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "image/jpeg", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def predict(self, jpeg_bytes: bytes, image_name: str = "frame") -> InferenceResult:
        """Send one JPEG and return a structured result.

        Network problems, timeouts, HTTP errors and malformed responses are all
        caught and reported; this method does not raise for them.
        """

        started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if not jpeg_bytes:
            return InferenceResult(
                ok=False,
                status_code=None,
                latency_ms=0.0,
                prediction=None,
                error_kind=ERROR_REQUEST,
                error_message="Empty image payload; nothing was sent.",
                sent_at=started_at,
                image_name=image_name,
            )

        start = time.perf_counter()
        try:
            response = self._session.post(
                self.endpoint,
                data=jpeg_bytes,
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
        except requests.Timeout:
            return self._failure(started_at, start, ERROR_TIMEOUT,
                                 f"No response within {self.timeout_seconds:.3f} s", image_name)
        except requests.ConnectionError as exc:
            return self._failure(started_at, start, ERROR_CONNECTION,
                                 f"Could not reach endpoint: {exc.__class__.__name__}", image_name)
        except requests.RequestException as exc:
            return self._failure(started_at, start, ERROR_REQUEST,
                                 f"Request failed: {exc.__class__.__name__}", image_name)

        latency_ms = _elapsed_ms(start)

        if response.status_code >= 400:
            detail = " ".join((response.text or "").split())[:200]
            return InferenceResult(
                ok=False,
                status_code=response.status_code,
                latency_ms=latency_ms,
                prediction=None,
                error_kind=ERROR_HTTP,
                error_message=f"HTTP {response.status_code}: {detail}".strip(),
                sent_at=started_at,
                image_name=image_name,
            )

        try:
            payload = response.json()
        except ValueError:
            return InferenceResult(
                ok=False,
                status_code=response.status_code,
                latency_ms=latency_ms,
                prediction=None,
                error_kind=ERROR_INVALID_RESPONSE,
                error_message="Response body was not valid JSON.",
                sent_at=started_at,
                image_name=image_name,
            )

        prediction = _parse_prediction(payload)
        if prediction is None:
            return InferenceResult(
                ok=False,
                status_code=response.status_code,
                latency_ms=latency_ms,
                prediction=None,
                error_kind=ERROR_INVALID_RESPONSE,
                error_message="Response JSON had no usable 'label' field.",
                sent_at=started_at,
                image_name=image_name,
            )

        return InferenceResult(
            ok=True,
            status_code=response.status_code,
            latency_ms=latency_ms,
            prediction=prediction,
            error_kind=None,
            error_message=None,
            sent_at=started_at,
            image_name=image_name,
        )

    def _failure(
        self, sent_at: str, start: float, kind: str, message: str, image_name: str
    ) -> InferenceResult:
        return InferenceResult(
            ok=False,
            status_code=None,
            latency_ms=_elapsed_ms(start),
            prediction=None,
            error_kind=kind,
            error_message=message,
            sent_at=sent_at,
            image_name=image_name,
        )

    def close(self) -> None:
        """Close the underlying HTTP session if this client created it."""
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "CloudClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


def send_frame(client: CloudClient, frame: Any) -> InferenceResult:
    """Send a :class:`~phase1_pipeline.capture.Frame` through ``client``."""
    return client.predict(frame.jpeg_bytes, image_name=frame.name)
