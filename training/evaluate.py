"""Evaluate the best checkpoint once on the locked test split.

Writes accuracy, per-class precision/recall, a confusion-matrix PNG and every
misclassified path into ``docs/model_card.md`` (or ``--model-card``).

The test split is meant to be run once, after training is finished. Do not tune
on it and rerun; that would turn the model card into a validation report.

Usage::

    python -m training.evaluate \
      --checkpoint training/runs/classifier/best \
      --manifest data/manifest.csv \
      --data-dir data
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

from training.data import (
    ManifestImageDataset,
    ManifestRow,
    image_counts,
    read_manifest,
    rows_for,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_checkpoint_labels(checkpoint: Path, model: Any) -> list[str]:
    """Read labels saved by train.py, falling back to the model config."""
    labels_path = checkpoint / "labels.json"
    if labels_path.is_file():
        payload = json.loads(labels_path.read_text(encoding="utf-8"))
        labels = payload.get("labels") if isinstance(payload, dict) else None
        if isinstance(labels, list) and labels and all(isinstance(x, str) for x in labels):
            return labels
        raise SystemExit(f"{labels_path} does not contain a non-empty 'labels' list")

    id2label = getattr(model.config, "id2label", {}) or {}
    try:
        indices = sorted(id2label, key=lambda key: int(key))
        return [str(id2label[index]) for index in indices]
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"no labels.json in {checkpoint} and model config has no usable id2label"
        ) from exc


def _facts(checkpoint: Path) -> dict[str, Any]:
    path = checkpoint / "training_run.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _per_class_metrics(
    labels: Sequence[str], matrix: list[list[int]]
) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}
    for index, label in enumerate(labels):
        true_positive = matrix[index][index]
        predicted = sum(matrix[row][index] for row in range(len(labels)))
        actual = sum(matrix[index])
        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / actual if actual else 0.0
        metrics[label] = {
            "precision": precision,
            "recall": recall,
            "support": actual,
        }
    return metrics


def write_confusion_matrix(
    matrix: list[list[int]], labels: Sequence[str], path: str | Path
) -> Path:
    """Draw the confusion matrix as a PNG without a plotting dependency."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = len(labels)
    cell = 64
    left = 170
    top = 110
    width = left + cell * count + 24
    height = top + cell * count + 24
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    maximum = max((value for row in matrix for value in row), default=1) or 1
    for row_index, row in enumerate(matrix):
        for column_index, value in enumerate(row):
            x0 = left + column_index * cell
            y0 = top + row_index * cell
            intensity = int(245 - 150 * (value / maximum))
            fill = (200, 235, 200) if row_index == column_index else (intensity, intensity, 255)
            draw.rectangle([x0, y0, x0 + cell, y0 + cell], fill=fill, outline=(120, 120, 120))
            text = str(value)
            box = draw.textbbox((0, 0), text, font=font)
            draw.text(
                (x0 + (cell - (box[2] - box[0])) / 2,
                 y0 + (cell - (box[3] - box[1])) / 2),
                text,
                fill=(20, 20, 20),
                font=font,
            )

    for index, label in enumerate(labels):
        short = label[:18]
        draw.text((8, top + index * cell + cell // 2 - 5), short, fill=(20, 20, 20), font=font)
        draw.text((left + index * cell + 4, 92), short[:9], fill=(20, 20, 20), font=font)

    draw.text((left, 20), "Confusion matrix (rows=true, columns=predicted)",
              fill=(20, 20, 20), font=font)
    image.save(destination)
    return destination


def _relative_link(target: Path, from_file: Path) -> str:
    return Path(os.path.relpath(target, from_file.parent)).as_posix()


def write_model_card(
    *,
    path: Path,
    labels: Sequence[str],
    rows: Sequence[ManifestRow],
    matrix: list[list[int]],
    accuracy: float,
    metrics: dict[str, dict[str, float | int]],
    misclassified: list[dict[str, Any]],
    checkpoint: Path,
    model_id: str,
    device: str,
    confusion_path: Path,
    torch_version: str,
    transformers_version: str,
) -> Path:
    """Write the model card and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = image_counts(rows)
    test_rows = rows_for(rows, "test")
    test_sessions = sorted({row.session for row in test_rows})
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = sum(matrix[row][column] for row in range(len(labels)) for column in range(len(labels)))
    correct = sum(matrix[index][index] for index in range(len(labels)))

    lines = [
        "# Model card — Phase 6 custom image classifier",
        "",
        f"Generated: **{generated}**",
        "",
        f"- Base model: `{model_id}`",
        f"- Checkpoint: `{checkpoint}`",
        f"- Evaluation device: `{device}`",
        f"- Test images: **{total}** from {len(test_sessions)} session(s)",
        f"- Overall accuracy: **{accuracy:.4f}** ({correct}/{total})",
        f"- Software: torch `{torch_version}`, transformers `{transformers_version}`",
        "",
        "## Dataset size",
        "",
        "Counts come from the split manifest used for this evaluation.",
        "",
        "| Class | Train | Val | Test | Total |",
        "|---|---:|---:|---:|---:|",
    ]
    for label in labels:
        counter = counts.get(label, Counter())
        lines.append(
            f"| {label} | {counter.get('train', 0)} | {counter.get('val', 0)} | "
            f"{counter.get('test', 0)} | {sum(counter.values())} |"
        )
    train_total = sum(counts.get(label, Counter()).get("train", 0) for label in labels)
    val_total = sum(counts.get(label, Counter()).get("val", 0) for label in labels)
    lines.append(f"| **total** | {train_total} | {val_total} | {len(test_rows)} | {train_total + val_total + len(test_rows)} |")

    lines += [
        "",
        "## Locked test metrics",
        "",
        f"![Confusion matrix]({_relative_link(confusion_path, path)})",
        "",
        "| Class | Precision | Recall | Support |",
        "|---|---:|---:|---:|",
    ]
    for label in labels:
        metric = metrics[label]
        lines.append(
            f"| {label} | {metric['precision']:.4f} | {metric['recall']:.4f} | "
            f"{metric['support']} |"
        )

    lines += ["", "## Misclassified test files", ""]
    if misclassified:
        lines += [
            "| File | True label | Predicted | Score |",
            "|---|---|---|---:|",
        ]
        for item in misclassified:
            lines.append(
                f"| `{item['path']}` | {item['true']} | {item['predicted']} | "
                f"{item['score']:.4f} |"
            )
    else:
        lines.append("No test file was misclassified.")

    lines += [
        "",
        "## Limitations",
        "",
        "- **These results apply only to this dataset.** The numbers describe the",
        "  images in this manifest, captured with this camera, lighting and part",
        "  set. They are not a claim of accuracy on other parts, factories or",
        "  cameras.",
        "- The test split is session-disjoint from train/val, but it is still a",
        "  small, locally collected sample; the confidence intervals are wide.",
        "- Class balance, label noise and near-duplicate frames were not audited",
        "  beyond the split-integrity check.",
        "- This card is generated by `training/evaluate.py`; it is not a",
        "  production validation or a safety certification.",
        "",
        f"<!-- generated by training/evaluate.py on {generated} -->",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_evaluation(
    *,
    checkpoint: str | Path,
    manifest: str | Path,
    data_dir: str | Path,
    model_card: str | Path,
    confusion_matrix: str | Path,
    batch_size: int = 32,
) -> Path:
    """Evaluate the checkpoint on the test split and write the model card."""
    try:
        import torch
        from transformers import AutoImageProcessor, AutoModelForImageClassification
        import transformers
    except ImportError as exc:
        raise SystemExit(
            "evaluation needs torch and transformers: "
            "pip install -r requirements-training.txt"
        ) from exc

    checkpoint_path = Path(checkpoint)
    if not checkpoint_path.is_dir():
        raise SystemExit(f"checkpoint not found: {checkpoint_path}")
    rows = read_manifest(manifest)
    test_rows = rows_for(rows, "test")
    if not test_rows:
        raise SystemExit("manifest has no test rows; cannot produce a model card")

    processor = AutoImageProcessor.from_pretrained(checkpoint_path)
    model = AutoModelForImageClassification.from_pretrained(checkpoint_path)
    labels = load_checkpoint_labels(checkpoint_path, model)
    if len(labels) != int(model.config.num_labels):
        raise SystemExit(
            f"checkpoint has {model.config.num_labels} output labels but labels.json "
            f"lists {len(labels)}"
        )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    mps = getattr(torch.backends, "mps", None)
    if device == "cpu" and mps is not None and mps.is_available():
        device = "mps"
    model.to(device).eval()

    dataset = ManifestImageDataset(
        test_rows, data_dir=data_dir, labels=labels, processor=processor
    )
    from torch.utils.data import DataLoader

    loader = DataLoader(
        dataset, batch_size=batch_size, shuffle=False,
        num_workers=0, pin_memory=(device == "cuda"),
    )

    matrix = [[0 for _ in labels] for _ in labels]
    misclassified: list[dict[str, Any]] = []
    index = 0
    with torch.no_grad():
        for pixel_values, true_labels in loader:
            pixel_values = pixel_values.to(device)
            logits = model(pixel_values=pixel_values).logits
            probabilities = torch.softmax(logits, dim=-1)
            scores, predictions = probabilities.max(dim=-1)
            for prediction, score, true_label in zip(
                predictions.tolist(), scores.tolist(), true_labels.tolist()
            ):
                matrix[true_label][prediction] += 1
                row = test_rows[index]
                index += 1
                if prediction != true_label:
                    misclassified.append(
                        {
                            "path": row.path,
                            "true": labels[true_label],
                            "predicted": labels[prediction],
                            "score": float(score),
                        }
                    )

    total = sum(sum(row) for row in matrix)
    correct = sum(matrix[i][i] for i in range(len(labels)))
    accuracy = correct / total if total else 0.0
    metrics = _per_class_metrics(labels, matrix)

    facts = _facts(checkpoint_path)
    model_id = str(facts.get("model_id") or getattr(model.config, "_name_or_path", "unknown"))
    confusion_path = write_confusion_matrix(matrix, labels, confusion_matrix)
    card_path = write_model_card(
        path=Path(model_card),
        labels=labels,
        rows=rows,
        matrix=matrix,
        accuracy=accuracy,
        metrics=metrics,
        misclassified=misclassified,
        checkpoint=checkpoint_path,
        model_id=model_id,
        device=device,
        confusion_path=confusion_path,
        torch_version=torch.__version__,
        transformers_version=transformers.__version__,
    )
    print(f"accuracy={accuracy:.4f} ({correct}/{total})")
    for label in labels:
        print(
            f"{label}: precision={metrics[label]['precision']:.4f} "
            f"recall={metrics[label]['recall']:.4f} support={metrics[label]['support']}"
        )
    print(f"wrote {card_path} and {confusion_path}")
    return card_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the locked test split once.")
    parser.add_argument("--checkpoint", required=True, help="best checkpoint directory")
    parser.add_argument("--manifest", default="data/manifest.csv", help="split manifest CSV")
    parser.add_argument("--data-dir", default="data", help="dataset root")
    parser.add_argument("--model-card", default="docs/model_card.md", help="markdown output")
    parser.add_argument(
        "--confusion-matrix", default="docs/confusion_matrix.png",
        help="confusion matrix PNG output",
    )
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        print("--batch-size must be at least 1")
        return 2
    try:
        run_evaluation(
            checkpoint=args.checkpoint,
            manifest=args.manifest,
            data_dir=args.data_dir,
            model_card=args.model_card,
            confusion_matrix=args.confusion_matrix,
            batch_size=args.batch_size,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"evaluation failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
