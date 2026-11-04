"""Real inference server using HuggingFace transformers object-detection.

Loads model once at startup, does warm-up inference before serving. Device
auto-detected (CUDA if available else CPU). Configurable via environment variables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from PIL import Image

LOG = logging.getLogger("real_server")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGES_DIR = PROJECT_ROOT / "images"

DEFAULT_MODEL_ID = "PekingU/rtdetr_r18vd"
MAX_BODY_BYTES = 8 * 1024 * 1024


def _get_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        return "cpu"
    except Exception:
        return "cpu"


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
        # decode image
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode image")
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(img_rgb)
        
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


def create_app() -> FastAPI:
    app = FastAPI(title="Robotic Hand real inference server", version="1.0.0")
    model = RealModel()
    # warm up before serving
    model.warmup()
    app.state.model = model

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
        m = app.state.model
        return {
            "status": "ok",
            "model": m.model_id,
            "device": m.device,
            "threshold": m.threshold,
        }

    @app.post("/predict")
    async def predict(request: Request) -> JSONResponse:
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            result, _ = app.state.model.predict(body)
            return JSONResponse(status_code=200, content=result)
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})

    return app


app = create_app()
