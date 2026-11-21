"""Split a captured dataset by session, never by random file.

Expected layout (produced by ``tools/capture_dataset.py``)::

    data/<class>/<session>_<n>.jpg

Every image from one session goes to exactly one of ``train``, ``val`` or
``test``. Session assignment is deterministic for a given ``--seed``, so
rerunning the command reproduces the same manifest. The test split is written
once and is not used by ``training/train.py``; ``training/evaluate.py`` reads it
once to produce the model card.

The command fails, rather than writing a partial manifest, when:

* a filename does not match ``<session>_<n>.<ext>``;
* a file would appear in more than one split, or twice in one split;
* a session would appear in more than one split;
* a class has fewer than ``--min-per-class`` images;
* a class/split has fewer than ``--min-per-split`` images;
* a class has fewer than three sessions, so disjoint train/val/test splits are
  impossible.

Usage::

    python tools/split_dataset.py --data-dir data --manifest data/manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

SPLITS = ("train", "val", "test")
SPLIT_ORDER = {name: index for index, name in enumerate(SPLITS)}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MANIFEST_COLUMNS = ("path", "label", "session", "split")


class SplitError(ValueError):
    """Raised when the dataset cannot be split safely."""


@dataclass(frozen=True)
class DatasetItem:
    """One captured image discovered on disk."""

    path: Path  # relative to the data directory
    label: str
    session: str


@dataclass(frozen=True)
class ManifestRow:
    """One manifest row. ``path`` is relative to the data directory."""

    path: str
    label: str
    session: str
    split: str

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in MANIFEST_COLUMNS}


def parse_filename(stem: str) -> tuple[str, int] | None:
    """Return ``(session, index)`` for ``<session>_<n>``, else None."""
    session, separator, index = stem.rpartition("_")
    if not separator or not session or not index.isdigit():
        return None
    number = int(index)
    if number < 1:
        return None
    return session, number


def discover_items(data_dir: str | Path) -> list[DatasetItem]:
    """Find class images under ``data_dir`` and validate their names."""
    root = Path(data_dir)
    if not root.is_dir():
        raise SplitError(f"data directory does not exist: {root}")

    items: list[DatasetItem] = []
    for class_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for path in sorted(class_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            parsed = parse_filename(path.stem)
            if parsed is None:
                raise SplitError(
                    f"{path}: expected <session>_<n>{path.suffix}, for example "
                    "session01_1.jpg"
                )
            session, _ = parsed
            items.append(
                DatasetItem(path=path.relative_to(root), label=class_dir.name, session=session)
            )

    if not items:
        raise SplitError(f"no images found under {root}")
    return items


def assign_sessions(
    items: Iterable[DatasetItem],
    *,
    seed: int = 42,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
) -> dict[tuple[str, str], str]:
    """Assign each ``(class, session)`` group to one split.

    Ratios apply to sessions, not files. At least one session per class is
    reserved for each split; three sessions per class is therefore the minimum.
    """
    if not 0.0 < val_ratio < 1.0:
        raise SplitError("--val-ratio must be between 0 and 1")
    if not 0.0 < test_ratio < 1.0:
        raise SplitError("--test-ratio must be between 0 and 1")
    if val_ratio + test_ratio >= 1.0:
        raise SplitError("--val-ratio + --test-ratio must be less than 1")

    sessions_by_class: dict[str, set[str]] = defaultdict(set)
    for item in items:
        sessions_by_class[item.label].add(item.session)

    assignment: dict[tuple[str, str], str] = {}
    for label in sorted(sessions_by_class):
        sessions = sorted(sessions_by_class[label])
        if len(sessions) < len(SPLITS):
            raise SplitError(
                f"class {label!r} has {len(sessions)} session(s); need at least "
                f"{len(SPLITS)} so train/val/test can be disjoint by session"
            )

        rng = random.Random(f"{seed}:{label}")
        rng.shuffle(sessions)
        count = len(sessions)
        n_val = max(1, round(count * val_ratio))
        n_test = max(1, round(count * test_ratio))
        while count - n_val - n_test < 1:
            if n_val >= n_test and n_val > 1:
                n_val -= 1
            elif n_test > 1:
                n_test -= 1
            else:  # pragma: no cover - unreachable because count >= 3
                raise SplitError(f"cannot allocate sessions for class {label!r}")
        n_train = count - n_val - n_test

        for position, session in enumerate(sessions):
            if position < n_train:
                split = "train"
            elif position < n_train + n_val:
                split = "val"
            else:
                split = "test"
            assignment[(label, session)] = split
    return assignment


def validate_manifest(
    rows: Sequence[ManifestRow],
    *,
    min_per_class: int = 3,
    min_per_split: int = 1,
) -> None:
    """Raise :class:`SplitError` when the split is not safe to train on."""
    if min_per_class < 1:
        raise SplitError("--min-per-class must be at least 1")
    if min_per_split < 1:
        raise SplitError("--min-per-split must be at least 1")
    if not rows:
        raise SplitError("manifest has no rows")

    paths: dict[str, list[str]] = defaultdict(list)
    session_splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    class_totals: Counter[str] = Counter()
    class_split_totals: Counter[tuple[str, str]] = Counter()

    for row in rows:
        if row.split not in SPLITS:
            raise SplitError(f"{row.path}: unknown split {row.split!r}")
        paths[row.path].append(row.split)
        session_splits[(row.label, row.session)].add(row.split)
        class_totals[row.label] += 1
        class_split_totals[(row.label, row.split)] += 1

    for path, split_names in paths.items():
        if len(split_names) > 1 and len(set(split_names)) > 1:
            raise SplitError(
                f"{path} appears in more than one split: "
                f"{', '.join(sorted(set(split_names)))}"
            )
        if len(split_names) > 1:
            raise SplitError(f"{path} appears more than once in the manifest")

    for (label, session), split_names in session_splits.items():
        if len(split_names) > 1:
            raise SplitError(
                f"session {session!r} of class {label!r} appears in more than one split"
            )

    for label, total in sorted(class_totals.items()):
        if total < min_per_class:
            raise SplitError(
                f"class {label!r} has {total} image(s); minimum is {min_per_class}"
            )
        for split in SPLITS:
            count = class_split_totals[(label, split)]
            if count < min_per_split:
                raise SplitError(
                    f"class {label!r} has {count} image(s) in {split}; "
                    f"minimum per split is {min_per_split}. Collect more sessions."
                )


def build_manifest(
    items: Iterable[DatasetItem],
    *,
    seed: int = 42,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
    min_per_class: int = 3,
    min_per_split: int = 1,
) -> list[ManifestRow]:
    """Return validated manifest rows for ``items``."""
    items = list(items)
    assignment = assign_sessions(
        items, seed=seed, val_ratio=val_ratio, test_ratio=test_ratio
    )
    rows = [
        ManifestRow(
            path=item.path.as_posix(),
            label=item.label,
            session=item.session,
            split=assignment[(item.label, item.session)],
        )
        for item in items
    ]
    validate_manifest(rows, min_per_class=min_per_class, min_per_split=min_per_split)
    rows.sort(key=lambda row: (SPLIT_ORDER[row.split], row.label, row.path))
    return rows


def write_manifest(rows: Sequence[ManifestRow], path: str | Path) -> Path:
    """Write ``rows`` to ``path`` atomically and return the path."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())
    temporary.replace(destination)
    return destination


def summary(rows: Sequence[ManifestRow]) -> str:
    """Return a human-readable class/split count table."""
    counts: Counter[tuple[str, str]] = Counter((row.label, row.split) for row in rows)
    labels = sorted({row.label for row in rows})
    width = max([len(label) for label in labels] + [5])
    lines = [f"{'class':<{width}}  " + "  ".join(f"{split:>5}" for split in SPLITS) + "  total"]
    for label in labels:
        numbers = [counts[(label, split)] for split in SPLITS]
        lines.append(
            f"{label:<{width}}  " + "  ".join(f"{number:>5}" for number in numbers)
            + f"  {sum(numbers):>5}"
        )
    totals = [sum(counts[(label, split)] for label in labels) for split in SPLITS]
    lines.append(
        f"{'total':<{width}}  " + "  ".join(f"{number:>5}" for number in totals)
        + f"  {sum(totals):>5}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split captured images by session into train/val/test."
    )
    parser.add_argument("--data-dir", default="data", help="dataset root (default: data)")
    parser.add_argument(
        "--manifest", default=None,
        help="manifest CSV path (default: <data-dir>/manifest.csv)",
    )
    parser.add_argument("--seed", type=int, default=42, help="session shuffle seed (default 42)")
    parser.add_argument("--val-ratio", type=float, default=0.15, help="fraction of sessions for val")
    parser.add_argument("--test-ratio", type=float, default=0.15, help="fraction of sessions for test")
    parser.add_argument("--min-per-class", type=int, default=3, help="minimum images per class")
    parser.add_argument(
        "--min-per-split", type=int, default=1,
        help="minimum images per class in each split",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    manifest_path = Path(args.manifest) if args.manifest else data_dir / "manifest.csv"
    try:
        items = discover_items(data_dir)
        rows = build_manifest(
            items,
            seed=args.seed,
            val_ratio=args.val_ratio,
            test_ratio=args.test_ratio,
            min_per_class=args.min_per_class,
            min_per_split=args.min_per_split,
        )
        write_manifest(rows, manifest_path)
    except SplitError as exc:
        print(f"split failed: {exc}", file=sys.stderr)
        return 1

    print(summary(rows))
    print(f"\nwrote {len(rows)} rows to {manifest_path}")
    print(
        "Test rows are locked: train with train/val, then run "
        "training/evaluate.py once on test."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
