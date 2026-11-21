"""Real inference server for object detection or image classification.

``TASK`` selects the Hugging Face pipeline:

* ``detection`` (default) — object detection, unchanged from Phase 1.
* ``classification`` — one image classifier; below the configured threshold
  the prediction becomes ``"unknown"``.

Loads the model once at startup and warms it up before serving. Device is
auto-detected (CUDA if available, else CPU). Configurable via environment
variables. The classification path does not import torch at module
import time, so tests can inject a tiny fake pipeline without downloading
weights.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image

LOG = logging.getLogger("real_server")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGES_DIR = PROJECT_ROOT / "images"

DEFAULT_MODEL_ID = "PekingU/rtdetr_r18vd"
DEFAULT_DETECTION_MODEL_ID = DEFAULT_MODEL_ID
DEFAULT_CLASSIFICATION_MODEL_ID = "microsoft/resnet-18"
DEFAULT_TASK = "detection"
VALID_TASKS = ("detection", "classification")
MAX_BODY_BYTES = 8 * 1024 * 1024


def _get_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        return "cpu"
    except Exception:
        return "cpu"


def configured_task() -> str:
    """Return ``TASK``, defaulting to detection and rejecting unknown values."""
    task = os.getenv("TASK", DEFAULT_TASK).strip().lower() or DEFAULT_TASK
    if task not in VALID_TASKS:
        raise ValueError(
            f"TASK must be one of {', '.join(VALID_TASKS)}; got {task!r}"
        )
    return task


def _decode_image(image_bytes: bytes) -> Image.Image:
    """Decode raw JPEG/PNG bytes into an RGB PIL image."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


class RealModel:
    def __init__(self):
        self.model_id = os.getenv("MODEL_ID", DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
        self.threshold = float(os.getenv("DETECTION_THRESHOLD", "0.5"))
        self.device = _get_device()
        self.pipe = None
        self._load()

    def _load(self):
        try:
            from transformers import pipeline

            LOG.info(f"Loading model {self.model_id} on {self.device}...")
            self.pipe = pipeline("object-detection", model=self.model_id, device=0 if self.device == "cuda" else -1)
            LOG.info("Model loaded")
        except Exception as e:
            LOG.error(f"Failed to load model: {e}")
            raise

    def warmup(self):
        try:
            # create dummy image
            dummy = np.zeros((224, 224, 3), dtype=np.uint8)
            dummy = cv2.cvtColor(dummy, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(dummy)
            _ = self.pipe(pil)
            LOG.info("Warmup complete")
        except Exception as e:
            LOG.warning(f"Warmup failed: {e}")

    def predict(self, image_bytes: bytes) -> tuple[dict[str, Any], float]:
        pil = _decode_image(image_bytes)

        start = time.perf_counter()
        results = self.pipe(pil)
        server_inference_ms = round((time.perf_counter() - start) * 1000.0, 3)
        
        detections = []
        for r in results:
            score = float(r.get("score", 0))
            if score < self.threshold:
                continue
            label = r.get("label", "unknown")
            box = r.get("box", {})
            detections.append({
                "label": label,
                "score": score,
                "box": box,
            })
        
        if detections:
            # top by score
            top = max(detections, key=lambda x: x["score"])
            prediction = top["label"]
        else:
            prediction = "none"
        
        response = {
            "prediction": prediction,
            "label": prediction,  # for compatibility with existing client
            "detections": detections,
            "server_inference_ms": server_inference_ms,
            "model": self.model_id,
            "device": self.device,
        }
        if detections:
            response["confidence"] = top["score"]
        return response, server_inference_ms


def _top_classification(results: Any) -> tuple[str, float]:
    """Return the top ``(label, score)`` from a transformers pipeline result."""
    if isinstance(results, dict):
        results = [results]
    if isinstance(results, list) and results and isinstance(results[0], list):
        results = results[0]
    if not isinstance(results, list) or not results:
        return "unknown", 0.0

    best = max(results, key=lambda item: float(item.get("score", 0.0)))
    label = str(best.get("label", "")).strip()
    score = float(best.get("score", 0.0))
    if not label:
        return "unknown", score
    return label, score


def build_classification_response(
    results: Any,
    *,
    threshold: float,
    model_id: str,
    device: str,
    server_inference_ms: float,
) -> dict[str, Any]:
    """Build the classification contract, turning a low top score into unknown."""
    label, score = _top_classification(results)
    prediction = label if score >= threshold else "unknown"
    return {
        "prediction": prediction,
        "label": prediction,
        "score": score,
        "confidence": score,
        "above_threshold": score >= threshold,
        "threshold": threshold,
        "task": "classification",
        "server_inference_ms": server_inference_ms,
        "model": model_id,
        "device": device,
    }


class ClassificationModel:
    """One pretrained image classifier behind the same ``predict`` interface."""

    def __init__(self, pipe=None, *, model_id: str | None = None,
                 device: str | None = None, threshold: float | None = None):
        self.task = "classification"
        self.model_id = (
            model_id or os.getenv("MODEL_ID", "").strip()
            or DEFAULT_CLASSIFICATION_MODEL_ID
        )
        self.device = device or _get_device()
        if threshold is None:
            threshold = float(os.getenv("CLASSIFICATION_THRESHOLD", "0.5"))
        self.threshold = float(threshold)
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("CLASSIFICATION_THRESHOLD must be between 0 and 1")
        self.pipe = pipe
        if self.pipe is None:
            self._load()

    def _load(self):
        try:
            from transformers import pipeline

            LOG.info("Loading classification model %s on %s...", self.model_id, self.device)
            self.pipe = pipeline(
                "image-classification",
                model=self.model_id,
                device=0 if self.device == "cuda" else -1,
            )
            LOG.info("Model loaded")
        except Exception as exc:
            LOG.error("Failed to load model: %s", exc)
            raise

    def warmup(self):
        try:
            self.pipe(_decode_image(_dummy_jpeg()))
            LOG.info("Warmup complete")
        except Exception as exc:
            LOG.warning("Warmup failed: %s", exc)

    def predict(self, image_bytes: bytes) -> tuple[dict[str, Any], float]:
        pil = _decode_image(image_bytes)
        start = time.perf_counter()
        results = self.pipe(pil)
        server_inference_ms = round((time.perf_counter() - start) * 1000.0, 3)
        response = build_classification_response(
            results,
            threshold=self.threshold,
            model_id=self.model_id,
            device=self.device,
            server_inference_ms=server_inference_ms,
        )
        return response, server_inference_ms


def _dummy_jpeg() -> bytes:
    """Return a tiny blank JPEG for classification warm-up."""
    dummy = np.zeros((224, 224, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", dummy)
    if not ok:  # pragma: no cover - OpenCV always encodes this array
        raise RuntimeError("Could not build warmup image")
    return encoded.tobytes()


def build_model():
    """Build the model selected by ``TASK`` (detection is the default)."""
    if configured_task() == "classification":
        return ClassificationModel()
    return RealModel()


def _api_token() -> str:
    """Return the configured token, or an empty string when authentication is off."""
    return os.getenv("API_TOKEN", "").strip()


def _token_matches(provided: str) -> bool:
    """Constant-time comparison against ``API_TOKEN``.

    An unset ``API_TOKEN`` never authorizes a request: the server fails closed
    with 503 instead of silently accepting an empty header.
    """
    expected = _api_token()
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


def create_app(model: Any | None = None) -> FastAPI:
    """Build the real inference application.

    Args:
        model: Optional pre-built model. Tests inject a stub classifier so they
            can check auth, thresholds and task-id echo without downloading
            torch weights.
    """
    app = FastAPI(title="Robotic Hand real inference server", version="1.1.0")
    if model is None:
        model = build_model()
        # Warm up before serving so the first real request is not the slowest.
        model.warmup()
    app.state.model = model
    if _api_token():
        LOG.info("API token authentication enabled for /predict")
    else:
        LOG.warning("API_TOKEN is not set; /predict will return 503 until it is configured")

    async def _read_body(request: Request) -> bytes | JSONResponse:
        body = await request.body()
        if not body:
            return JSONResponse(
                status_code=400,
                content={"detail": "Empty request body; expected raw JPEG bytes."},
            )
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Body exceeds {MAX_BODY_BYTES} bytes."},
            )
        arr = np.frombuffer(body, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return JSONResponse(
                status_code=400,
                content={"detail": "Body could not be decoded as an image."},
            )
        return body

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness probe. Deliberately exposes only model and device."""
        m = app.state.model
        return {
            "model": m.model_id,
            "device": m.device,
        }

    @app.post("/predict")
    async def predict(request: Request) -> JSONResponse:
        """Run inference on a raw JPEG. Requires the ``X-API-Token`` header."""
        if not _api_token():
            # Fail closed: an unconfigured server cannot authenticate anyone.
            return JSONResponse(
                status_code=503,
                content={"detail": "Server is not configured with API_TOKEN."},
            )
        if not _token_matches(request.headers.get("X-API-Token", "")):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "X-API-Token"},
            )
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        task_id = (request.headers.get("X-Task-Id") or "").strip()
        try:
            result, _ = app.state.model.predict(body)
            if task_id:
                result["task_id"] = task_id
            return JSONResponse(
                status_code=200,
                content=result,
                headers={"X-Task-Id": task_id} if task_id else None,
            )
        except Exception as e:
            # Do not echo request headers or the token in error responses.
            LOG.exception("Inference failed")
            return JSONResponse(status_code=500, content={"detail": str(e)})

    return app
