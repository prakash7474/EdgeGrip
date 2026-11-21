"""Manifest loading and image preprocessing shared by train and evaluate."""

from __future__ import annotations

import csv
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

SPLITS = ("train", "val", "test")
MANIFEST_COLUMNS = ("path", "label", "session", "split")


@dataclass(frozen=True)
class ManifestRow:
    """One row of ``data/manifest.csv`` (path relative to the data directory)."""

    path: str
    label: str
    session: str
    split: str


def read_manifest(path: str | Path) -> list[ManifestRow]:
    """Read and validate a split manifest."""
    source = Path(path)
    if not source.is_file():
        raise ValueError(
            f"manifest not found: {source}. Run tools/split_dataset.py first."
        )
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(MANIFEST_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{source}: missing column(s) {', '.join(sorted(missing))}; "
                f"expected {', '.join(MANIFEST_COLUMNS)}"
            )
        rows = [
            ManifestRow(
                path=row["path"].strip(),
                label=row["label"].strip(),
                session=row["session"].strip(),
                split=row["split"].strip(),
            )
            for row in reader
        ]
    if not rows:
        raise ValueError(f"{source}: manifest has no data rows")
    for row in rows:
        if row.split not in SPLITS:
            raise ValueError(f"{source}: {row.path} has unknown split {row.split!r}")
        if not row.path or not row.label:
            raise ValueError(f"{source}: every row needs a path and a label")
    return rows


def rows_for(rows: Sequence[ManifestRow], split: str) -> list[ManifestRow]:
    """Return the rows for one split."""
    return [row for row in rows if row.split == split]


def labels_from(rows: Sequence[ManifestRow]) -> list[str]:
    """Return the sorted class names used by the model."""
    return sorted({row.label for row in rows})


def image_counts(rows: Sequence[ManifestRow]) -> dict[str, Counter[str]]:
    """Return per-class image counts for each split."""
    counts: dict[str, Counter[str]] = {}
    for row in rows:
        counts.setdefault(row.label, Counter())[row.split] += 1
    return counts


def resolve_image(data_dir: str | Path, row: ManifestRow) -> Path:
    """Return the image path for a manifest row."""
    return Path(data_dir) / row.path


class ManifestImageDataset:
    """Torch-compatible dataset backed by the manifest and a HF processor.

    ``torch`` is not imported here; the DataLoader provides the batching. The
    processor returns tensors, so each item is ``(pixel_values, label_index)``.
    """

    def __init__(
        self,
        rows: Sequence[ManifestRow],
        *,
        data_dir: str | Path,
        labels: Sequence[str],
        processor: Any,
        train: bool = False,
        random_hflip: bool = False,
        seed: int = 42,
    ) -> None:
        if not rows:
            raise ValueError("dataset split has no rows")
        self.rows = list(rows)
        self.data_dir = Path(data_dir)
        self.processor = processor
        self.train = train
        self.random_hflip = random_hflip and train
        self.rng = random.Random(seed)
        self.label_to_index = {label: index for index, label in enumerate(labels)}
        unknown = sorted({row.label for row in rows} - set(self.label_to_index))
        if unknown:
            raise ValueError(f"labels missing from label map: {', '.join(unknown)}")

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int):
        row = self.rows[index]
        path = resolve_image(self.data_dir, row)
        try:
            with Image.open(path) as image:
                image = image.convert("RGB")
                if self.random_hflip and self.rng.random() < 0.5:
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                pixel_values = self.processor(
                    images=image, return_tensors="pt"
                )["pixel_values"][0]
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not read image {path}: {exc}") from exc
        return pixel_values, self.label_to_index[row.label]
