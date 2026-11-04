"""Configuration for the Phase 1 edge -> cloud inference pipeline.

All environment access is centralised here so the rest of the code never reads
``os.environ`` directly. Secrets stay in the environment (or a gitignored
``.env``) and are excluded from logs, CSV records and error messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The real process environment wins over .env, so a single shell can override
# values without editing files.
load_dotenv(PROJECT_ROOT / ".env")

SUPPORTED_MODES = ("mock", "real")
DEFAULT_MOCK_ENDPOINT = "http://127.0.0.1:8000/predict"
DEFAULT_MODE = "mock"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one run of the pipeline.

    Attributes:
        mode: ``"mock"`` (local mock server) or ``"real"`` (hosted provider).
        endpoint: Full URL the client POSTs JPEG bytes to.
        api_key: Provider credential. Empty in mock mode; never logged.
        timeout_seconds: Client-side timeout per request (connect + read).
        latency_target_ms: Prototype budget for one round trip.
        camera_enabled: Whether capture should try the physical camera first.
        camera_index: OpenCV device index for the camera.
        images_dir: Folder with fallback frames and ``labels.json``.
        log_path: CSV where the benchmark writes one row per request.
        measurements_path: Markdown file updated with measured latency.
    """

    mode: str
    endpoint: str
    api_key: str
    timeout_seconds: float
    latency_target_ms: float
    camera_enabled: bool
    camera_index: int
    images_dir: Path
    log_path: Path
    measurements_path: Path

    @property
    def is_mock(self) -> bool:
        """Return True when the local mock server is the target."""
        return self.mode == "mock"

    @property
    def redacted(self) -> dict[str, object]:
        """Return settings safe to log (no credential)."""
        return {
            "mode": self.mode,
            "endpoint": self.endpoint,
            "timeout_seconds": self.timeout_seconds,
            "latency_target_ms": self.latency_target_ms,
            "camera_enabled": self.camera_enabled,
            "camera_index": self.camera_index,
            "images_dir": str(self.images_dir),
            "log_path": str(self.log_path),
        }


def _text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _positive_float(name: str, default: float) -> float:
    raw = _text(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero, got {value}")
    return value


def _non_negative_int(name: str, default: int) -> int:
    raw = _text(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must be zero or greater, got {value}")
    return value


def _path(name: str, default: Path) -> Path:
    raw = _text(name)
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _boolean(name: str, default: bool) -> bool:
    raw = _text(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean, got {raw!r}")


def load_settings(*, require_api_key: bool | None = None) -> Settings:
    """Read, validate and return the settings for this run.

    Args:
        require_api_key: Force the API-key requirement on or off. When None
            (the default) a key is only required in ``real`` mode.

    Raises:
        ConfigurationError: If a value is missing or malformed.
    """

    mode = (_text("INFERENCE_MODE") or DEFAULT_MODE).lower()
    if mode not in SUPPORTED_MODES:
        raise ConfigurationError(
            f"INFERENCE_MODE must be one of {SUPPORTED_MODES}, got {mode!r}"
        )

    endpoint = _text("INFERENCE_ENDPOINT") or DEFAULT_MOCK_ENDPOINT
    if not endpoint.startswith(("http://", "https://")):
        raise ConfigurationError("INFERENCE_ENDPOINT must start with http:// or https://")

    api_key = _text("INFERENCE_API_KEY")
    needs_key = mode == "real" if require_api_key is None else require_api_key
    if needs_key and not api_key:
        raise ConfigurationError(
            "INFERENCE_MODE=real needs INFERENCE_API_KEY set in the environment "
            "or .env (copy .env.example and fill it in)."
        )

    return Settings(
        mode=mode,
        endpoint=endpoint,
        api_key=api_key,
        timeout_seconds=_positive_float("INFERENCE_TIMEOUT_SECONDS", 10.0),
        latency_target_ms=_positive_float("LATENCY_TARGET_MS", 5000.0),
        camera_enabled=_boolean("CAMERA_ENABLED", True),
        camera_index=_non_negative_int("CAMERA_INDEX", 0),
        images_dir=_path("SAMPLE_IMAGES_DIR", PROJECT_ROOT / "images"),
        log_path=_path("BENCHMARK_CSV_PATH", PROJECT_ROOT / "logs" / "latency.csv"),
        measurements_path=_path(
            "MEASUREMENTS_PATH", PROJECT_ROOT / "docs" / "measurements.md"
        ),
    )
