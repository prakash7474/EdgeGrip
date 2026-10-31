"""Provider-independent cloud inference client for the Phase 1 prototype.

``send_image_to_cloud`` owns the pipeline: task id, timing, response validation
and error capture. A provider class owns the provider-specific HTTP details, so
Phase 3 can add AWS, Azure or Google Cloud by adding a provider rather than
rewriting the pipeline.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import requests

from config import ConfigurationError, Settings, load_settings
from image_loader import ImageLoadError, LoadedImage, load_image
from label_matching import labels_match
from request_log import append_result

USER_AGENT = "robot-cloud-ai-phase1/1.0"
MAX_ERROR_BODY_CHARS = 300
MAX_ERROR_MESSAGE_CHARS = 500


class CloudInferenceError(RuntimeError):
    """A structured failure raised inside the inference pipeline."""

    def __init__(
        self, message: str, *, kind: str = "unknown", http_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


@dataclass
class InferenceResult:
    """Structured outcome of one inference attempt (success or failure)."""

    task_id: str
    image_filename: str
    image_path: str
    request_time_utc: str
    response_time_utc: str = ""
    latency_ms: float = 0.0
    predicted_label: str = ""
    confidence: float | None = None
    status: str = "error"
    http_status: int | None = None
    error_kind: str = ""
    error_message: str = ""
    model_id: str = ""
    test_case: str = ""
    expected_label: str = ""
    prediction_correct: bool | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    def as_row(self) -> dict[str, str]:
        """Flatten the result into the CSV column set."""

        return {
            "task_id": self.task_id,
            "image_filename": self.image_filename,
            "image_path": self.image_path,
            "request_time_utc": self.request_time_utc,
            "response_time_utc": self.response_time_utc,
            "latency_ms": f"{self.latency_ms:.2f}",
            "predicted_label": self.predicted_label,
            "confidence": "" if self.confidence is None else f"{self.confidence:.6f}",
            "status": self.status,
            "http_status": "" if self.http_status is None else str(self.http_status),
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "model_id": self.model_id,
            "test_case": self.test_case,
            "expected_label": self.expected_label,
            "prediction_correct": (
                "" if self.prediction_correct is None else str(self.prediction_correct)
            ),
        }


@runtime_checkable
class InferenceProvider(Protocol):
    """Minimal contract every cloud provider adapter must satisfy."""

    name: str

    def classify(self, image: LoadedImage, *, timeout: tuple[float, float]) -> Any:
        """Return the provider's parsed JSON payload or raise."""
        ...


class HuggingFaceProvider:
    """Adapter for the Hugging Face Inference Providers router (hf-inference).

    Contract per the HF Inference Providers docs: POST raw image bytes to
    ``/hf-inference/models/{model_id}`` with a Bearer token, receive a JSON
    array of ``{"label": str, "score": float}`` objects.
    """

    name = "huggingface"

    def __init__(
        self,
        *,
        api_token: str,
        endpoint_url: str,
        model_id: str,
        session: requests.Session | None = None,
    ) -> None:
        if not endpoint_url.startswith(("https://", "http://")):
            raise ConfigurationError("The inference endpoint must be an http(s) URL")
        self._api_token = api_token
        self.endpoint_url = endpoint_url
        self.model_id = model_id
        self._session = session or requests.Session()

    @classmethod
    def from_settings(
        cls, settings: Settings, session: requests.Session | None = None
    ) -> "HuggingFaceProvider":
        return cls(
            api_token=settings.api_token,
            endpoint_url=settings.endpoint_url,
            model_id=settings.model_id,
            session=session,
        )

    def classify(self, image: LoadedImage, *, timeout: tuple[float, float]) -> Any:
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": image.content_type,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        response = self._session.post(
            self.endpoint_url,
            headers=headers,
            data=image.data,
            timeout=timeout,
        )
        status = response.status_code
        if status in (401, 403):
            raise CloudInferenceError(
                "Authentication or authorization failed. Check HF_API_TOKEN and that "
                "the token has Inference Providers permission for this model.",
                kind="auth",
                http_status=status,
            )
        if status == 429:
            raise CloudInferenceError(
                "Rate limited by the inference provider.", kind="rate_limited", http_status=status
            )
        if status == 503:
            raise CloudInferenceError(
                "Model unavailable or still loading on the provider.",
                kind="unavailable",
                http_status=status,
            )
        if status >= 400:
            raise CloudInferenceError(
                f"Provider returned HTTP {status}: {_body_snippet(response)}",
                kind="http",
                http_status=status,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise CloudInferenceError(
                "Provider response was not valid JSON.", kind="invalid_json", http_status=status
            ) from exc


def new_task_id() -> str:
    """Return a unique task identifier for one inference attempt."""

    return f"task-{uuid.uuid4()}"


def validate_prediction(payload: Any) -> tuple[str, float | None]:
    """Validate the provider payload and return the best (label, score).

    Accepts the documented Hugging Face shape (a non-empty array of
    ``{"label", "score"}``) and tolerates a single bare object. Raises
    ``CloudInferenceError`` with kind ``invalid_response`` for anything else.
    """

    if isinstance(payload, dict):
        if "error" in payload:
            raise CloudInferenceError(
                f"Provider returned an error payload: {payload.get('error')}",
                kind="api_error",
            )
        payload = [payload]

    if not isinstance(payload, list) or not payload:
        raise CloudInferenceError(
            "Expected a non-empty JSON array of {label, score} objects.",
            kind="invalid_response",
        )

    candidates: list[tuple[str, float | None]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise CloudInferenceError(
                f"Prediction item {index} is not an object.", kind="invalid_response"
            )
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise CloudInferenceError(
                f"Prediction item {index} has no usable 'label'.", kind="invalid_response"
            )
        score = item.get("score")
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise CloudInferenceError(
                    f"Prediction item {index} has a non-numeric 'score'.",
                    kind="invalid_response",
                )
            if not 0.0 <= score <= 1.0:
                raise CloudInferenceError(
                    f"Prediction item {index} has an out-of-range 'score': {score}",
                    kind="invalid_response",
                )
            score = float(score)
        candidates.append((label.strip(), score))

    scored = [(label, score) for label, score in candidates if score is not None]
    best_label, best_score = max(scored, key=lambda item: item[1]) if scored else candidates[0]
    return best_label, (None if best_score is None else round(best_score, 6))


def send_image_to_cloud(
    image_path: str | Path,
    task_id: str | None = None,
    *,
    settings: Settings | None = None,
    provider: InferenceProvider | None = None,
    timeout: tuple[float, float] | None = None,
    test_case: str = "",
    expected_label: str | None = None,
    log: bool = True,
) -> InferenceResult:
    """Run one image inference request, log it, and return a structured result.

    Failures are captured rather than raised so a batch can log the failure and
    carry on with the next request. The latency value is the complete
    client-side HTTP round trip measured with ``time.perf_counter()``.

    Args:
        image_path: Local image file to send.
        task_id: Optional pre-generated id; a UUID-based id is created if None.
        settings: Resolved settings; loaded and validated when omitted.
        provider: Provider adapter; the Hugging Face adapter is built from
            settings when omitted. Tests inject fake providers here.
        timeout: (connect, read) timeout pair overriding the configured values.
        test_case: Label recorded in the log, e.g. ``"TC1.1"``.
        expected_label: When supplied, the prediction is compared with it and
            ``prediction_correct`` is recorded (TC1.3).
        log: Append one CSV record to ``settings.log_path`` (default True).
    """

    resolved_task_id = task_id or new_task_id()
    result = InferenceResult(
        task_id=resolved_task_id,
        image_filename=Path(image_path).name,
        image_path=str(image_path),
        request_time_utc=_utc_now(),
        test_case=test_case,
    )
    log_target: Path | None = None

    try:
        resolved = settings or load_settings()
        result.model_id = resolved.model_id
        log_target = resolved.log_path if log else None
        active_provider = provider or HuggingFaceProvider.from_settings(resolved)
        image = load_image(image_path)
        result.image_filename = image.filename
        request_timeout = timeout or resolved.inference_timeout

        started = time.perf_counter()
        try:
            payload = active_provider.classify(image, timeout=request_timeout)
        finally:
            result.latency_ms = round((time.perf_counter() - started) * 1000, 2)

        label, confidence = validate_prediction(payload)
        result.predicted_label = label
        result.confidence = confidence
        result.status = "success"
    except CloudInferenceError as exc:
        _apply_error(result, exc.kind, str(exc), exc.http_status)
    except ConfigurationError as exc:
        _apply_error(result, "config", str(exc), None)
    except ImageLoadError as exc:
        _apply_error(result, "image", str(exc), None)
    except requests.exceptions.Timeout as exc:
        _apply_error(result, "timeout", f"Request timed out: {exc}", None)
    except requests.exceptions.ConnectionError as exc:
        _apply_error(result, "connection", f"Connection failed: {exc}", None)
    except requests.exceptions.RequestException as exc:
        _apply_error(result, "http", f"HTTP request failed: {exc}", None)
    except Exception as exc:  # defensive: one bad request must never crash a batch
        _apply_error(result, "unexpected", f"{type(exc).__name__}: {exc}", None)
    finally:
        result.response_time_utc = _utc_now()

    if expected_label is not None:
        result.expected_label = expected_label
        result.prediction_correct = result.succeeded and labels_match(
            result.predicted_label, expected_label
        )

    if log_target is not None:
        append_result(result, log_target)

    return result


def _apply_error(
    result: InferenceResult, kind: str, message: str, http_status: int | None
) -> None:
    result.status = "error"
    result.error_kind = kind
    result.error_message = message[:MAX_ERROR_MESSAGE_CHARS]
    result.http_status = http_status
    result.predicted_label = ""
    result.confidence = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _body_snippet(response: requests.Response) -> str:
    try:
        text = " ".join((response.text or "").split())
    except Exception:  # pragma: no cover - defensive
        return ""
    if len(text) > MAX_ERROR_BODY_CHARS:
        return text[:MAX_ERROR_BODY_CHARS] + "..."
    return text
