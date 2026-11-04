"""Deterministic, offline stand-in for a cloud inference endpoint.

The mock server exists so every Phase 1 test and benchmark runs with no
network access and no provider credentials. It is intentionally simple:

* ``POST /predict`` accepts raw JPEG bytes (``Content-Type: image/jpeg``).
* The SHA-256 of the request body is looked up in a table built from
  ``images/labels.json``; known sample images therefore always return their
  expected label. Unknown but valid images get a stable label derived from the
  same digest, so two identical requests never disagree.
* ``POST /predict/slow?delay_ms=...`` returns the same response after sleeping;
  tests use it to exercise the client's timeout path.

This is a mock. Its labels come from a fixture manifest, not from a model.
Run it with ``python -m mock_server``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

LOG = logging.getLogger("mock_server")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGES_DIR = PROJECT_ROOT / "images"
LABELS_FILENAME = "labels.json"

MODEL_ID = "mock-vit-b16"
FALLBACK_LABELS = ("bolt", "bracket", "gear", "nut", "washer", "bearing")
MAX_BODY_BYTES = 8 * 1024 * 1024


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


class KnownImageIndex:
    """Map sample-image digests to the labels declared in ``labels.json``."""

    def __init__(self, images_dir: Path = DEFAULT_IMAGES_DIR) -> None:
        self.images_dir = Path(images_dir)
        self._labels_by_digest: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        manifest_path = self.images_dir / LABELS_FILENAME
        if not manifest_path.is_file():
            LOG.warning("No %s in %s; every request will get a fallback label",
                        LABELS_FILENAME, self.images_dir)
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("Could not read %s: %s", manifest_path, exc)
            return

        for name, entry in (manifest.get("images") or {}).items():
            path = self.images_dir / name
            label = entry.get("expected_label") if isinstance(entry, dict) else None
            if not label or not path.is_file():
                continue
            self._labels_by_digest[sha256_hex(path.read_bytes())] = str(label)
        LOG.info("Indexed %d known sample image(s) from %s",
                 len(self._labels_by_digest), manifest_path)

    @property
    def count(self) -> int:
        """Number of known sample images."""
        return len(self._labels_by_digest)

    def label_for(self, digest: str) -> str | None:
        """Return the declared label for a digest, or None if unknown."""
        return self._labels_by_digest.get(digest)


def _confidence(digest: str, known: bool) -> float:
    """Return a deterministic, plausibly-looking confidence score."""
    fraction = int(digest[:8], 16) / 0xFFFFFFFF
    value = (0.90 + fraction * 0.09) if known else (0.50 + fraction * 0.45)
    return round(value, 4)


def _fallback_label(digest: str) -> str:
    """Return a stable label for an image the manifest does not contain."""
    return FALLBACK_LABELS[int(digest[:8], 16) % len(FALLBACK_LABELS)]


def _inspect(body: bytes) -> tuple[int, int] | None:
    """Decode the body as an image and return (width, height), or None."""
    if not body:
        return None
    buffer = np.frombuffer(body, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    return int(width), int(height)


def build_response(body: bytes, index: KnownImageIndex) -> dict[str, Any]:
    """Build the deterministic response for one request body."""
    digest = sha256_hex(body)
    known_label = index.label_for(digest)
    label = known_label or _fallback_label(digest)
    width, height = _inspect(body) or (0, 0)
    return {
        "label": label,
        "confidence": _confidence(digest, known_label is not None),
        "model": MODEL_ID,
        "known_image": known_label is not None,
        "image_sha256": digest,
        "image_width": width,
        "image_height": height,
        "mock": True,
    }


def create_app(images_dir: Path | None = None) -> FastAPI:
    """Create the mock inference application.

    Args:
        images_dir: Folder containing ``labels.json`` and the sample images.
            Defaults to the project ``images/`` directory.
    """

    index = KnownImageIndex(Path(images_dir) if images_dir else DEFAULT_IMAGES_DIR)
    app = FastAPI(title="Robotic Hand mock inference server", version="1.0.0")
    app.state.index = index

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
        img_size = _inspect(body)
        if img_size is None:
            return JSONResponse(
                status_code=400,
                content={"detail": "Body could not be decoded as an image."},
            )
        return body

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness probe used by tests and the README workflow."""
        return {"status": "ok", "model": MODEL_ID, "known_images": index.count}

    @app.post("/predict")
    async def predict(request: Request) -> JSONResponse:
        """Classify one raw JPEG and return a deterministic label."""
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        return JSONResponse(status_code=200, content=build_response(body, index))

    @app.post("/predict/slow")
    async def predict_slow(
        request: Request,
        delay_ms: int = Query(0, ge=0, le=30000, description="Artificial delay"),
    ) -> JSONResponse:
        """Same as /predict, but sleeps first so clients can hit a timeout."""
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        await asyncio.sleep(delay_ms / 1000.0)
        return JSONResponse(status_code=200, content=build_response(body, index))

    return app


app = create_app()
