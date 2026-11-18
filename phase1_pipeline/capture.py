"""Frame capture for the edge client.

A real camera is used when one is available; otherwise the pipeline falls back
to JPEGs in ``images/`` so the whole system can be built and tested on a laptop.
Sample files are sent byte-for-byte, which lets the mock server recognise them
and keeps tests deterministic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

LOG = logging.getLogger("capture")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
JPEG_QUALITY = 90
CAMERA_WARMUP_FRAMES = 3


class CaptureError(RuntimeError):
    """Raised when neither a camera nor a usable sample image is available."""


@dataclass(frozen=True)
class Frame:
    """One camera frame ready to send.

    Attributes:
        name: Filename for sample frames, or ``camera:<index>`` for live ones.
        jpeg_bytes: JPEG-encoded payload.
        source: ``"camera"`` or ``"sample"``.
        width: Frame width in pixels.
        height: Frame height in pixels.
    """

    name: str
    jpeg_bytes: bytes
    source: str
    width: int
    height: int

    @property
    def size_kb(self) -> float:
        """Return the encoded payload size in kilobytes."""
        return len(self.jpeg_bytes) / 1024.0


def list_sample_images(images_dir: str | Path) -> list[Path]:
    """Return the usable sample images in ``images_dir``, sorted by name."""
    folder = Path(images_dir)
    if not folder.is_dir():
        return []
    return sorted(
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


def load_sample_frame(path: str | Path) -> Frame:
    """Load a JPEG/PNG file as a :class:`Frame` without re-encoding it.

    Raises:
        CaptureError: If the file is missing or cannot be decoded.
    """

    image_path = Path(path)
    try:
        data = image_path.read_bytes()
    except OSError as exc:
        raise CaptureError(f"Could not read sample image {image_path}: {exc}") from exc

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise CaptureError(f"{image_path} is not a decodable image")
    height, width = image.shape[:2]
    return Frame(
        name=image_path.name,
        jpeg_bytes=data,
        source="sample",
        width=int(width),
        height=int(height),
    )


def encode_jpeg(image: np.ndarray, quality: int = JPEG_QUALITY) -> bytes:
    """JPEG-encode an OpenCV BGR image."""
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok or buffer is None:
        raise CaptureError("OpenCV could not encode the frame as JPEG")
    return buffer.tobytes()


def camera_frame(camera_index: int = 0, quality: int = JPEG_QUALITY) -> Frame | None:
    """Grab one frame from a camera, or return None if none is available.

    The device is opened, read and released per call; Phase 1 has no streaming
    requirement and this keeps camera state from leaking between tests.
    """

    capture = cv2.VideoCapture(camera_index)
    if not capture.isOpened():
        capture.release()
        LOG.warning("Camera %d is not available; falling back to sample images", camera_index)
        return None
    try:
        image = None
        for _ in range(CAMERA_WARMUP_FRAMES):
            ok, candidate = capture.read()
            if ok and candidate is not None:
                image = candidate
            else:
                break
        if image is None:
            LOG.warning("Camera %d opened but returned no frame; using sample images", camera_index)
            return None
        height, width = image.shape[:2]
        return Frame(
            name=f"camera:{camera_index}",
            jpeg_bytes=encode_jpeg(image, quality),
            source="camera",
            width=int(width),
            height=int(height),
        )
    finally:
        capture.release()


class FrameGrabber:
    """Yield frames from the camera, cycling sample images as a fallback.

    The camera is probed once. If it is missing, the grabber logs a single
    warning and serves ``images/`` files round-robin, so a long benchmark run
    does not re-open the device on every request.
    """

    def __init__(
        self,
        images_dir: str | Path,
        *,
        camera_enabled: bool = True,
        camera_index: int = 0,
    ) -> None:
        self._samples = list_sample_images(images_dir)
        self._images_dir = Path(images_dir)
        self._camera_index = camera_index
        self._cursor = 0
        self._camera_checked = False
        self._camera_available = camera_enabled
        if not self._samples:
            LOG.warning("No sample images in %s", self._images_dir)

    @property
    def samples(self) -> list[Path]:
        """Return the sample images seen at construction time."""
        return list(self._samples)

    def next_frame(self) -> Frame:
        """Return the next frame, raising :class:`CaptureError` if none exists."""
        if not self._camera_checked:
            self._camera_checked = True
            if self._camera_available:
                first = camera_frame(self._camera_index)
                if first is not None:
                    return first
                self._camera_available = False
        elif self._camera_available:
            frame = camera_frame(self._camera_index)
            if frame is not None:
                return frame
            LOG.warning("Camera stopped supplying frames; using sample images")
            self._camera_available = False

        if not self._samples:
            raise CaptureError(
                f"No camera and no sample images in {self._images_dir}. "
                "Add JPG/PNG files or connect a camera."
            )

        path = self._samples[self._cursor % len(self._samples)]
        self._cursor += 1
        return load_sample_frame(path)

    def frame_for(self, name: str) -> Frame:
        """Load one specific sample image by filename."""
        path = self._images_dir / name
        if not path.is_file():
            raise CaptureError(f"Sample image not found: {path}")
        return load_sample_frame(path)

    def pin_sample(self, name: str) -> None:
        """Send the same sample image for every request.

        Raises:
            CaptureError: If the named sample does not exist.
        """
        self.frame_for(name)
        self._samples = [self._images_dir / name]
        self._cursor = 0
        self._camera_available = False
        self._camera_checked = True


def capture_once(
    images_dir: str | Path,
    *,
    camera_enabled: bool = True,
    camera_index: int = 0,
) -> Frame:
    """Convenience wrapper: return one frame from the camera or samples."""
    return FrameGrabber(
        images_dir, camera_enabled=camera_enabled, camera_index=camera_index
    ).next_frame()


if __name__ == "__main__":  # Small manual sanity check.
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    grabber = FrameGrabber(Path(__file__).resolve().parent.parent / "images")
    frame = grabber.next_frame()
    print(f"{frame.name} {frame.width}x{frame.height} {frame.size_kb:.1f} KB ({frame.source})")
