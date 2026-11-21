"""Capture a custom image-classification dataset from a webcam.

Usage::

    python tools/capture_dataset.py bolt session01
    python tools/capture_dataset.py nut session02 --max-images 40

Images are written to ``data/<class>/<session>_<n>.jpg``. Each capture gets a
new sequence number, so an existing session can be resumed without overwriting
a photo. The terminal prints the live count for every class after each capture.

The ``data/`` directory is gitignored: photos are captured locally and are not
committed. This tool needs a webcam and a person to place the parts; it is not
exercised by the automated test suite.

Controls in the preview window:

* ``SPACE`` capture one frame
* ``q`` / ``ESC`` quit
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

IMAGE_SUFFIXES = (".jpg", ".jpeg")


def safe_component(value: str, kind: str) -> str:
    """Return a filesystem-safe class or session name.

    Spaces and punctuation become underscores, and a name that would resolve to
    ``.``/``..`` is rejected. Lower-casing keeps class directories consistent on
    case-insensitive filesystems.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    cleaned = cleaned.lower()
    if not cleaned:
        raise ValueError(f"{kind} must contain at least one letter or digit")
    if cleaned in {".", ".."}:
        raise ValueError(f"{kind} is not a usable directory name: {value!r}")
    return cleaned


def class_counts(root: Path) -> dict[str, int]:
    """Return the number of captured JPEGs per class under ``root``."""
    root = Path(root)
    counts: dict[str, int] = {}
    if not root.is_dir():
        return counts
    for class_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        count = sum(
            1
            for path in class_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
        counts[class_dir.name] = count
    return counts


def format_counts(counts: dict[str, int]) -> str:
    """Render per-class counts for the terminal."""
    if not counts:
        return "(no images yet)"
    return "  ".join(f"{name}={count}" for name, count in sorted(counts.items()))


def next_image_path(root: Path, class_name: str, session_id: str) -> Path:
    """Return the next free ``data/<class>/<session>_<n>.jpg`` path."""
    class_dir = Path(root) / class_name
    class_dir.mkdir(parents=True, exist_ok=True)
    highest = 0
    prefix = f"{session_id}_"
    for path in class_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        stem = path.stem
        if not stem.startswith(prefix):
            continue
        suffix = stem[len(prefix):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return class_dir / f"{session_id}_{highest + 1}.jpg"


def capture(
    class_name: str,
    session_id: str,
    *,
    root: Path = Path("data"),
    camera_index: int = 0,
    max_images: int = 0,
    interval_seconds: float = 0.0,
    quality: int = 95,
) -> int:
    """Run the interactive capture loop. Returns a process exit code."""
    try:
        import cv2
    except ImportError:  # pragma: no cover - dependency is in requirements.txt
        print("capture_dataset needs opencv-python; install requirements.txt", file=sys.stderr)
        return 1

    try:
        class_name = safe_component(class_name, "class name")
        session_id = safe_component(session_id, "session id")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not 0 < quality <= 100:
        print("error: --quality must be between 1 and 100", file=sys.stderr)
        return 2

    root = Path(root)
    camera = cv2.VideoCapture(camera_index)
    if not camera.isOpened():
        camera.release()
        print(
            f"error: could not open camera index {camera_index}. "
            "This tool needs a webcam; no photos were written.",
            file=sys.stderr,
        )
        return 1

    saved = 0
    print(f"class={class_name} session={session_id} output={root / class_name}")
    print(f"counts: {format_counts(class_counts(root))}")
    print("SPACE = capture, q/ESC = quit")
    last_capture = 0.0
    try:
        while True:
            ok, frame = camera.read()
            if not ok or frame is None:
                print("error: camera stopped returning frames", file=sys.stderr)
                return 1

            counts = class_counts(root)
            preview = frame.copy()
            cv2.putText(
                preview,
                f"{class_name} | {session_id} | saved {saved}",
                (10, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
            cv2.putText(
                preview,
                format_counts(counts)[:110],
                (10, 56),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
            cv2.imshow("capture_dataset", preview)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key != 32:
                continue
            now = time.monotonic()
            if interval_seconds and now - last_capture < interval_seconds:
                continue

            path = next_image_path(root, class_name, session_id)
            if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, quality]):
                print(f"error: could not write {path}", file=sys.stderr)
                return 1
            last_capture = now
            saved += 1
            counts = class_counts(root)
            print(f"saved {path}  |  counts: {format_counts(counts)}", flush=True)
            if max_images and saved >= max_images:
                print(f"reached --max-images {max_images}; stopping")
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()

    print(f"session finished: {saved} new image(s) in {root / class_name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a per-class image dataset from a webcam."
    )
    parser.add_argument("class_name", help="class label, e.g. bolt")
    parser.add_argument("session_id", help="capture session, e.g. session01")
    parser.add_argument("--output-dir", default="data", help="dataset root (default: data)")
    parser.add_argument("--camera-index", type=int, default=0, help="camera index (default 0)")
    parser.add_argument(
        "--max-images", type=int, default=0,
        help="stop after this many new images in this run (0 = no limit)",
    )
    parser.add_argument(
        "--interval", type=float, default=0.0,
        help="minimum seconds between captures (default 0)",
    )
    parser.add_argument("--quality", type=int, default=95, help="JPEG quality 1-100 (default 95)")
    args = parser.parse_args(argv)

    return capture(
        args.class_name,
        args.session_id,
        root=Path(args.output_dir),
        camera_index=args.camera_index,
        max_images=args.max_images,
        interval_seconds=args.interval,
        quality=args.quality,
    )


if __name__ == "__main__":
    raise SystemExit(main())
