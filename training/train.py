"""Fine-tune a pretrained image classifier from a session-split manifest.

Install the training dependencies first::

    pip install -r requirements-training.txt

Full run (GPU recommended; also works on CPU)::

    python -m training.train --config config/train.classification.json

CPU smoke test (one epoch, tiny subset, two train batches)::

    python -m training.train --config config/train.classification.json --smoke

The script never touches the test split. ``training/evaluate.py`` runs the
locked test set once after training. No metric is reported here unless the
script actually ran; the per-epoch numbers go to ``<output_dir>/metrics.csv``.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from training.data import (
    ManifestImageDataset,
    labels_from,
    read_manifest,
    rows_for,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "manifest": "data/manifest.csv",
    "data_dir": "data",
    "output_dir": "training/runs/classifier",
    "model_id": "microsoft/resnet-18",
    "epochs": 8,
    "batch_size": 32,
    "learning_rate": 0.0001,
    "weight_decay": 0.01,
    "early_stopping_patience": 3,
    "metric": "val_loss",
    "seed": 42,
    "num_workers": 0,
    "random_hflip": True,
    "use_cpu": False,
    "max_train_samples": 0,
    "max_val_samples": 0,
    "max_train_batches": 0,
    "max_val_batches": 0,
}

REQUIRED_KEYS = {"manifest", "data_dir", "output_dir", "model_id"}
ALLOWED_KEYS = set(DEFAULTS)


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a JSON training config and fill defaults."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"config not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"config {source} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"config {source} must be a JSON object")

    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"config has unknown keys: {', '.join(sorted(unknown))}")
    missing = REQUIRED_KEYS - set(raw)
    if missing:
        raise ValueError(f"config is missing keys: {', '.join(sorted(missing))}")

    config = {**DEFAULTS, **raw}
    for key in ("epochs", "batch_size", "early_stopping_patience", "seed",
                "num_workers", "max_train_samples", "max_val_samples",
                "max_train_batches", "max_val_batches"):
        if not isinstance(config[key], int) or config[key] < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if config["epochs"] < 1:
        raise ValueError("epochs must be at least 1")
    if config["batch_size"] < 1:
        raise ValueError("batch_size must be at least 1")
    if config["learning_rate"] <= 0:
        raise ValueError("learning_rate must be positive")
    if config["metric"] not in ("val_loss", "val_accuracy"):
        raise ValueError("metric must be 'val_loss' or 'val_accuracy'")
    return config


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _select_device(torch, use_cpu: bool) -> str:
    if use_cpu:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def _make_loader(torch, dataset, *, batch_size: int, shuffle: bool,
                 num_workers: int, device: str):
    from torch.utils.data import DataLoader

    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        pin_memory=(device == "cuda"),
    )


def _run_epoch(torch, model, loader, criterion, device: str, *, optimizer=None,
               max_batches: int = 0) -> tuple[float, float]:
    """Run one train or eval epoch and return (mean loss, accuracy)."""
    training = optimizer is not None
    model.train(training)
    total_loss = 0.0
    total_correct = 0
    total_items = 0

    with torch.set_grad_enabled(training):
        for batch_index, (pixel_values, labels) in enumerate(loader):
            if max_batches and batch_index >= max_batches:
                break
            pixel_values = pixel_values.to(device)
            labels = labels.to(device)
            if training:
                optimizer.zero_grad()
            logits = model(pixel_values=pixel_values).logits
            loss = criterion(logits, labels)
            if training:
                loss.backward()
                optimizer.step()
            total_items += int(labels.numel())
            total_loss += float(loss.item()) * int(labels.numel())
            total_correct += int((logits.argmax(dim=-1) == labels).sum().item())

    if total_items == 0:
        raise RuntimeError("epoch processed no images; check the manifest and limits")
    return total_loss / total_items, total_correct / total_items


def _save_checkpoint(model, processor, labels: list[str], config: dict[str, Any],
                     best_epoch: int, best_value: float, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(destination)
    processor.save_pretrained(destination)
    (destination / "labels.json").write_text(
        json.dumps({"labels": labels}, indent=2) + "\n", encoding="utf-8"
    )
    metadata = {
        "model_id": config["model_id"],
        "labels": labels,
        "best_epoch": best_epoch,
        "best_metric": config["metric"],
        "best_value": best_value,
        "effective_config": config,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (destination / "training_run.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )


def run_training(config: dict[str, Any]) -> Path:
    """Train and return the best-checkpoint directory."""
    try:
        import torch
        from transformers import AutoImageProcessor, AutoModelForImageClassification
    except ImportError as exc:
        raise SystemExit(
            "training needs torch and transformers: "
            "pip install -r requirements-training.txt"
        ) from exc

    manifest_path = _resolve(config["manifest"])
    data_dir = _resolve(config["data_dir"])
    output_dir = _resolve(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = read_manifest(manifest_path)
    labels = labels_from(rows)
    train_rows = rows_for(rows, "train")
    val_rows = rows_for(rows, "val")
    if not train_rows or not val_rows:
        raise SystemExit("manifest needs non-empty train and val splits")

    rng = random.Random(config["seed"])
    if config["max_train_samples"]:
        train_rows = list(train_rows)
        rng.shuffle(train_rows)
        train_rows = train_rows[: config["max_train_samples"]]
    if config["max_val_samples"]:
        val_rows = list(val_rows)
        rng.shuffle(val_rows)
        val_rows = val_rows[: config["max_val_samples"]]

    device = _select_device(torch, bool(config["use_cpu"]))
    torch.manual_seed(config["seed"])
    if device == "cuda":
        torch.cuda.manual_seed_all(config["seed"])

    print(f"device={device} model={config['model_id']} classes={len(labels)}")
    print(f"train={len(train_rows)} val={len(val_rows)} test=locked")
    print(f"output={output_dir}")

    processor = AutoImageProcessor.from_pretrained(config["model_id"])
    train_dataset = ManifestImageDataset(
        train_rows,
        data_dir=data_dir,
        labels=labels,
        processor=processor,
        train=True,
        random_hflip=bool(config["random_hflip"]),
        seed=config["seed"],
    )
    val_dataset = ManifestImageDataset(
        val_rows, data_dir=data_dir, labels=labels, processor=processor
    )
    train_loader = _make_loader(
        torch, train_dataset, batch_size=config["batch_size"], shuffle=True,
        num_workers=config["num_workers"], device=device,
    )
    val_loader = _make_loader(
        torch, val_dataset, batch_size=config["batch_size"], shuffle=False,
        num_workers=config["num_workers"], device=device,
    )

    label_to_id = {label: index for index, label in enumerate(labels)}
    id_to_label = {index: label for label, index in label_to_id.items()}
    model = AutoModelForImageClassification.from_pretrained(
        config["model_id"],
        num_labels=len(labels),
        id2label=id_to_label,
        label2id=label_to_id,
        ignore_mismatched_sizes=True,
    ).to(device)

    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=float(config["learning_rate"]),
        weight_decay=float(config["weight_decay"]),
    )

    metrics_path = output_dir / "metrics.csv"
    metrics_path.write_text(
        "epoch,train_loss,train_accuracy,val_loss,val_accuracy,epoch_seconds\n",
        encoding="utf-8",
    )

    best_dir = output_dir / "best"
    best_value = float("-inf") if config["metric"] == "val_accuracy" else float("inf")
    best_epoch = 0
    stale_epochs = 0
    started = time.monotonic()

    for epoch in range(1, config["epochs"] + 1):
        epoch_started = time.monotonic()
        train_loss, train_accuracy = _run_epoch(
            torch, model, train_loader, criterion, device,
            optimizer=optimizer, max_batches=config["max_train_batches"],
        )
        val_loss, val_accuracy = _run_epoch(
            torch, model, val_loader, criterion, device,
            max_batches=config["max_val_batches"],
        )
        seconds = time.monotonic() - epoch_started
        with metrics_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{epoch},{train_loss:.6f},{train_accuracy:.6f},"
                f"{val_loss:.6f},{val_accuracy:.6f},{seconds:.3f}\n"
            )

        if config["metric"] == "val_accuracy":
            improved = val_accuracy > best_value + 1e-9
            current = val_accuracy
        else:
            improved = val_loss < best_value - 1e-9
            current = val_loss
        marker = ""
        if improved:
            best_value = current
            best_epoch = epoch
            stale_epochs = 0
            _save_checkpoint(model, processor, labels, config, best_epoch,
                             best_value, best_dir)
            marker = " *best*"
        else:
            stale_epochs += 1

        print(
            f"epoch {epoch}/{config['epochs']} "
            f"train_loss={train_loss:.4f} train_acc={train_accuracy:.4f} "
            f"val_loss={val_loss:.4f} val_acc={val_accuracy:.4f} "
            f"({seconds:.1f}s){marker}"
        )

        patience = config["early_stopping_patience"]
        if patience and stale_epochs >= patience:
            print(f"early stop: no {config['metric']} improvement for {patience} epoch(s)")
            break

    if best_epoch == 0:  # pragma: no cover - epoch 1 always improves over inf
        raise RuntimeError("training finished without a checkpoint")

    elapsed = time.monotonic() - started
    print(
        f"best epoch {best_epoch} by {config['metric']}={best_value:.6f} "
        f"after {elapsed:.1f}s\ncheckpoint: {best_dir}"
    )
    return best_dir


def smoke_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a CPU-friendly smoke-test config over a tiny subset."""
    return {
        **config,
        "epochs": 1,
        "batch_size": min(int(config["batch_size"]), 8),
        "max_train_samples": min(int(config["max_train_samples"]) or 32, 32),
        "max_val_samples": min(int(config["max_val_samples"]) or 16, 16),
        "max_train_batches": 2,
        "max_val_batches": 1,
        "num_workers": 0,
        "use_cpu": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fine-tune a small image classifier.")
    parser.add_argument(
        "--config", default="config/train.classification.json",
        help="JSON training config (default: config/train.classification.json)",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="CPU smoke test: one epoch, 32 train / 16 val images, two batches",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(f"config error: {exc}")
        return 2
    if args.smoke:
        config = smoke_config(config)
        print("smoke mode: one epoch on a tiny CPU subset; not a model-quality run")
    try:
        run_training(config)
    except (ValueError, RuntimeError) as exc:
        print(f"training failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
