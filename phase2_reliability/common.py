"""Common utilities for Phase 2: message format, image preparation, IDs."""

from __future__ import annotations

import base64
import io
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


@dataclass
class Message:
    message_id: str
    created_at: str
    payload: str  # base64 JPEG
    # Phase 4 task context (backward compatible)
    attempt: int = 1
    frame_timestamp: str = ""
    deadline: float = 0.0

    @property
    def task_id(self) -> str:
        return self.message_id

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | bytes) -> "Message":
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        obj = json.loads(data)
        return cls(
            message_id=obj["message_id"],
            created_at=obj["created_at"],
            payload=obj["payload"],
            attempt=int(obj.get("attempt", 1) or 1),
            frame_timestamp=str(obj.get("frame_timestamp", "") or ""),
            deadline=float(obj.get("deadline", 0.0) or 0.0),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_message_id() -> str:
    return str(uuid.uuid4())


def prepare_jpeg_under_size(jpeg_bytes: bytes, max_kb: float = 100.0) -> bytes:
    """Resize/compress JPEG to be under ~max_kb. Works on bytes."""
    if len(jpeg_bytes) <= max_kb * 1024:
        return jpeg_bytes

    # Try to decode and re-encode with lower quality / smaller size
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        # Try PIL
        try:
            im = Image.open(io.BytesIO(jpeg_bytes))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=70, optimize=True)
            data = out.getvalue()
            if len(data) <= max_kb * 1024:
                return data
        except Exception:
            pass
        return jpeg_bytes

    h, w = img.shape[:2]
    quality = 85
    scale = 1.0
    for _ in range(20):  # try iterations
        if scale < 1.0:
            new_w = int(w * scale)
            new_h = int(h * scale)
            resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        else:
            resized = img
        ok, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            break
        data = buf.tobytes()
        if len(data) <= max_kb * 1024:
            return data
        if quality > 40:
            quality -= 10
            continue
        scale *= 0.9
        quality = 75
    return data if 'data' in locals() else jpeg_bytes


def jpeg_to_base64(jpeg_bytes: bytes) -> str:
    return base64.b64encode(jpeg_bytes).decode("ascii")


def base64_to_jpeg(b64: str) -> bytes:
    return base64.b64decode(b64)
