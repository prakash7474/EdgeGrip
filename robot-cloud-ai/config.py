"""Configuration for the Phase 1 cloud inference prototype.

Every provider setting is resolved here so the application logic never reads
environment variables directly. The API token comes from ``.env`` (or from the
real process environment, which wins) and is deliberately excluded from logs,
CSV records and error messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# Real environment variables take precedence over the .env file, which makes it
# easy to override the token for a single PowerShell session.
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_MODEL_ID = "google/vit-base-patch16-224"
DEFAULT_ROUTER_BASE_URL = "https://router.huggingface.co/hf-inference/models"

# Accepted names for the Hugging Face token, in priority order.
_TOKEN_VARIABLES = ("HF_API_TOKEN", "HF_TOKEN", "HUGGINGFACE_API_TOKEN")


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one run of the prototype."""

    api_token: str
    model_id: str
    endpoint_url: str
    connect_timeout: float
    read_timeout: float
    images_dir: Path
    log_path: Path
    latency_target_ms: float

    @property
    def inference_timeout(self) -> tuple[float, float]:
        """Return the (connect, read) timeout pair passed to requests."""
        return (self.connect_timeout, self.read_timeout)


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero, got {value}")
    return value


def _endpoint_for(model_id: str) -> str:
    override = os.getenv("HF_ENDPOINT_URL", "").strip()
    if override:
        if not override.startswith(("https://", "http://")):
            raise ConfigurationError("HF_ENDPOINT_URL must start with http:// or https://")
        return override
    return f"{DEFAULT_ROUTER_BASE_URL}/{model_id}"


def _resolve_token() -> str:
    for name in _TOKEN_VARIABLES:
        candidate = os.getenv(name, "").strip()
        if candidate:
            return candidate
    return ""


def load_settings(*, require_token: bool = True) -> Settings:
    """Validate and return the configuration for this run.

    Args:
        require_token: when True (the default) a missing API token raises
            ``ConfigurationError``. Reporting-only paths and the network
            failure test pass False because they never authenticate.
    """

    token = _resolve_token()
    if require_token and not token:
        raise ConfigurationError(
            "No API token found. Copy .env.example to .env and set "
            "HF_API_TOKEN to a Hugging Face token with Inference Providers "
            "permission."
        )

    model_id = os.getenv("HF_MODEL_ID", DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID

    return Settings(
        api_token=token,
        model_id=model_id,
        endpoint_url=_endpoint_for(model_id),
        connect_timeout=_positive_float("HTTP_CONNECT_TIMEOUT_SECONDS", 5.0),
        read_timeout=_positive_float("HTTP_READ_TIMEOUT_SECONDS", 30.0),
        images_dir=Path(os.getenv("IMAGES_DIR") or PROJECT_ROOT / "images"),
        log_path=Path(os.getenv("RESULTS_LOG_PATH") or PROJECT_ROOT / "logs" / "results.csv"),
        latency_target_ms=_positive_float("LATENCY_TARGET_MS", 5000.0),
    )
