"""Local image loading and validation with OpenCV.

Cloud endpoints do not all accept the same payload. This module owns the local
side of that contract: verify the file exists, decode it, and re-encode one
canonical JPEG that the selected provider accepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class ImageLoadError(RuntimeError):
    """Raised when an image is missing, corrupted or unreadable."""


@dataclass(frozen=True)
class LoadedImage:
    """A validated local image ready for upload."""

    path: Path
    filename: str
    width: int
    height: int
    content_type: str
    data: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def load_image(image_path: str | Path, *, jpeg_quality: int = 90) -> LoadedImage:
    """Read, validate and JPEG-encode an image.

    ``np.fromfile`` + ``cv2.imdecode`` is used instead of ``cv2.imread`` so
    paths containing non-ASCII characters work on Windows.
    """

    path = Path(image_path).expanduser()
    if not path.is_file():
        raise ImageLoadError(f"Image not found: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ImageLoadError(f"{path.name} has an unsupported extension (expected one of {supported})")

    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError as exc:
        raise ImageLoadError(f"Could not read {path.name}: {exc}") from exc

    if buffer.size == 0:
        raise ImageLoadError(f"{path.name} is empty")

    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise ImageLoadError(f"{path.name} is not a readable image (corrupted or unsupported)")

    height, width = image.shape[:2]
    if width < 1 or height < 1:
        raise ImageLoadError(f"{path.name} has invalid dimensions: {width}x{height}")

    ok, encoded = cv2.imencode(
        ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    )
    if not ok or encoded.size == 0:
        raise ImageLoadError(f"{path.name} could not be encoded as JPEG")

    return LoadedImage(
        path=path,
        filename=path.name,
        width=width,
        height=height,
        content_type="image/jpeg",
        data=encoded.tobytes(),
    )


def list_images(directory: str | Path) -> list[Path]:
    """Return supported image files in ``directory``, sorted by name."""

    folder = Path(directory)
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
