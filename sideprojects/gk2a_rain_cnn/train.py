"""Reproducible, retrospective next-hour heavy-rain CNN baseline.

Real training consumes dataset.py samples.csv. ``--smoke-test`` uses synthetic
arrays only to check optimization and checkpoint round trips, never rain skill.
PyTorch is imported only when a model or a training run is requested.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np

NUMERIC_FEATURES = (
    "temperature_c", "humidity_pct", "pressure_hpa", "wind_speed_m_s",
    "wind_direction_sin", "wind_direction_cos", "past_rain_1h_mm",
)
REQUIRED_COLUMNS = {
    "sample_id", "station_id", "issue_time_kst", "target_start_kst", "target_end_kst",
    "label", "rain_1h_mm", "split", "frame_paths", "frame_sha256", "frame_times_utc",
}
SPLITS = ("train", "val", "test")
MODEL_VERSION = "gk2a-ir112-cnn-next1h-v1"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Every sample timestamp must include its timezone")
    return result.astimezone(timezone.utc)


def numeric_value(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else math.nan
    except (TypeError, ValueError):
        return math.nan


def numeric_rows(rows):
    """Only read explicit at-issue features; target rainfall is never a fallback."""
    values = []
    for row in rows:
        angle = numeric_value(row.get("wind_direction_deg"))
        valid_angle = math.isfinite(angle) and 0 <= angle <= 360
        radians = math.radians(angle) if valid_angle else math.nan
        values.append([
            numeric_value(row.get("temperature_c")), numeric_value(row.get("humidity_pct")),
            numeric_value(row.get("pressure_hpa")), numeric_value(row.get("wind_speed_m_s")),
            math.sin(radians), math.cos(radians), numeric_value(row.get("past_rain_1h_mm")),
        ])
    return np.asarray(values, dtype=np.float32).reshape(len(rows), len(NUMERIC_FEATURES))


def fit_preprocessor(train_values):
    """Fit imputation and scaling on training rows, including missing indicators."""
    values = np.asarray(train_values, dtype=np.float64)
    if values.ndim != 2 or not len(values):
        raise ValueError("Numeric preprocessing requires a nonempty training matrix")
    medians = []
    for column in values.T:
        observed = column[np.isfinite(column)]
        medians.append(float(np.median(observed)) if len(observed) else 0.0)
    median = np.asarray(medians)
    filled = np.where(np.isfinite(values), values, median)
    mean, scale = filled.mean(axis=0), filled.std(axis=0)
    scale[scale < 1e-8] = 1.0
    return {"features": list(NUMERIC_FEATURES), "median": median.tolist(),
            "mean": mean.tolist(), "scale": scale.tolist(), "fit_split": "train"}


def transform_numeric(values, state):
    values = np.asarray(values, dtype=np.float64)
    missing = ~np.isfinite(values)
    filled = np.where(missing, np.asarray(state["median"]), values)
    scaled = (filled - np.asarray(state["mean"])) / np.asarray(state["scale"])
    return np.concatenate((scaled, missing.astype(np.float64)), axis=1).astype(np.float32)


def classification_metrics(labels, probabilities, threshold=0.5):
    """Grouped-score average precision handles ties without rank-order artifacts."""
    y, p = np.asarray(labels, dtype=np.int8), np.asarray(probabilities, dtype=np.float64)
    if y.ndim != 1 or p.shape != y.shape or not len(y) or not np.isin(y, [0, 1]).all():
        raise ValueError("Metrics require aligned, nonempty binary labels and probabilities")
    if not np.isfinite(p).all() or (p < 0).any() or (p > 1).any() or not 0 <= threshold <= 1:
        raise ValueError("Probabilities and decision threshold must be finite within [0, 1]")
    prediction = p >= threshold
    tp, fp = int((prediction & (y == 1)).sum()), int((prediction & (y == 0)).sum())
    fn, tn = int((~prediction & (y == 1)).sum()), int((~prediction & (y == 0)).sum())
    positives, negatives = int(y.sum()), int((y == 0).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / positives if positives else None
    f1 = 2 * precision * recall / (precision + recall) if recall is not None and precision + recall else (0.0 if positives else None)
    average_precision = None
    if positives and negatives:
        order = np.argsort(-p, kind="stable")
        sorted_p, sorted_y = p[order], y[order]
        ends = np.r_[np.flatnonzero(np.diff(sorted_p) != 0), len(y) - 1]
        cumulative = np.cumsum(sorted_y)[ends]
        recalls, precisions = cumulative / positives, cumulative / (ends + 1)
        average_precision = float(np.sum(np.diff(np.r_[0.0, recalls]) * precisions))
    return {"sample_count": len(y), "positive_count": positives, "negative_count": negatives,
            "prevalence": positives / len(y), "threshold": float(threshold),
            "average_precision": average_precision, "precision": precision, "recall": recall,
            "f1": f1, "brier": float(np.mean((p - y) ** 2)),
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
            "single_class": not (positives and negatives)}


def select_threshold(labels, probabilities):
    if len(set(map(int, labels))) != 2:
        raise ValueError("Decision threshold selection requires both validation classes")
    candidates = sorted(set([0.0, 1.0, *map(float, probabilities)]), reverse=True)
    best, score = 0.5, -1.0
    for candidate in candidates:
        result = classification_metrics(labels, probabilities, candidate)["f1"]
        if result > score:
            best, score = candidate, result
    return best


def read_samples(path, *, threshold_mm=30.0, min_class_samples=5):
    """Reject missing frames, mismatched labels and overlapping temporal splits."""
    path = Path(path).resolve()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError("samples.csv lacks required forecast, split or image lineage columns")
        rows = list(reader)
    if not rows:
        raise ValueError("No real forecast samples: collect and align observations first")
    if not math.isfinite(threshold_mm) or threshold_mm <= 0 or min_class_samples < 1:
        raise ValueError("Invalid rain threshold or minimum class size")
    seen, frame_digests = set(), {}
    for row in rows:
        if row["sample_id"] in seen:
            raise ValueError("Duplicate sample_id")
        seen.add(row["sample_id"])
        if str(row["station_id"]) != "108" or row["split"] not in SPLITS:
            raise ValueError("This baseline requires station 108 and train/val/test splits")
        issue = timestamp(row["issue_time_kst"])
        if issue.minute or issue.second or issue.microsecond:
            raise ValueError("Forecast origins must be exact hours; observations must not be rounded")
        if timestamp(row["target_start_kst"]) != issue or timestamp(row["target_end_kst"]) != issue + timedelta(hours=1):
            raise ValueError("Target must be the following one-hour interval (issue, issue + 1h]")
        rain, label = numeric_value(row["rain_1h_mm"]), int(row["label"])
        if not math.isfinite(rain) or rain < 0 or label not in (0, 1) or label != int(rain >= threshold_mm):
            raise ValueError("Rain label differs from the declared threshold or contains missing rainfall")
        paths, hashes, times = (json.loads(row[field]) for field in ("frame_paths", "frame_sha256", "frame_times_utc"))
        if not all(isinstance(value, list) and len(value) == 4 for value in (paths, hashes, times)):
            raise ValueError("Every sample requires exactly four ordered image observations")
        if [timestamp(value) for value in times] != [issue - timedelta(hours=offset) for offset in (3, 2, 1, 0)]:
            raise ValueError("Image history must be exactly t-3h, t-2h, t-1h and t")
        resolved = []
        for value, expected in zip(paths, hashes):
            frame = (path.parent / value).resolve()
            if not frame.is_file() or frame.suffix != ".npz" or len(str(expected)) != 64:
                raise ValueError("Image patch or its SHA-256 is missing")
            actual = frame_digests.setdefault(frame, digest(frame)) if frame not in frame_digests else frame_digests[frame]
            if actual != expected:
                raise ValueError(f"Image patch checksum differs: {frame.name}")
            resolved.append(str(frame))
        row.update(_issue=issue, _frames=resolved, _label=label)
    grouped = {name: [row for row in rows if row["split"] == name] for name in SPLITS}
    for name, group in grouped.items():
        counts = [sum(row["_label"] == label for row in group) for label in (0, 1)]
        if min(counts) < min_class_samples:
            raise ValueError(f"{name} has class counts {counts}; require at least {min_class_samples} each. No credible training/evaluation can be reported.")
    for earlier, later in zip(SPLITS, SPLITS[1:]):
        last_target = max(row["_issue"] + timedelta(hours=1) for row in grouped[earlier])
        first_history = min(row["_issue"] - timedelta(hours=3) for row in grouped[later])
        if last_target >= first_history:
            raise ValueError("Temporal splits overlap in image history or target intervals; rebuild with a purge gap")
    return grouped


@lru_cache(maxsize=256)
def image_patch(path):
    with np.load(path, allow_pickle=False) as archive:
        dn, valid = archive["dn"], archive["valid"]
    if dn.ndim != 2 or dn.dtype != np.uint16 or valid.shape != dn.shape or not np.isin(valid, [0, 1]).all():
        raise ValueError("Patch requires a uint16 DN plane and a matching binary validity mask")
    mask = valid.astype(bool)
    if (dn[mask] > 8191).any():
        raise ValueError("Valid pixels contain values outside the declared 13-bit DN range")
    value = np.where(mask, dn, 0).astype(np.float32) / 8191.0
    return value, mask.astype(np.float32)


def image_stack(paths):
    parts = [image_patch(str(path)) for path in paths]
    if len(parts) != 4 or len({part[0].shape for part in parts}) != 1:
        raise ValueError("Image history must have four patches on the same grid")
    return np.stack([part[0] for part in parts] + [part[1] for part in parts])


def make_model(*, use_numeric=False, numeric_size=2 * len(NUMERIC_FEATURES)):
    import torch
    from torch import nn

    class RainCNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.use_numeric = use_numeric
            self.image = nn.Sequential(
                nn.Conv2d(8, 16, 3, padding=1), nn.GroupNorm(4, 16), nn.SiLU(), nn.MaxPool2d(2),
                nn.Conv2d(16, 32, 3, padding=1), nn.GroupNorm(8, 32), nn.SiLU(), nn.MaxPool2d(2),
                nn.Conv2d(32, 64, 3, padding=1), nn.GroupNorm(8, 64), nn.SiLU(), nn.AdaptiveAvgPool2d(1),
            )
            self.numeric = nn.Sequential(nn.Linear(numeric_size, 16), nn.SiLU()) if use_numeric else None
            self.head = nn.Sequential(nn.Linear(80 if use_numeric else 64, 32), nn.SiLU(),
                                      nn.Dropout(0.1), nn.Linear(32, 1))

        def forward(self, image, numeric=None):
            features = self.image(image).flatten(1)
            if self.use_numeric:
                if numeric is None:
                    raise ValueError("Numeric-enabled model requires verified at-issue features")
                features = torch.cat((features, self.numeric(numeric)), dim=1)
            return self.head(features).squeeze(1)

    return RainCNN()


def seed_all(seed):
    import torch
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    torch.use_deterministic_algorithms(True)


def write_json(path, payload):
    temporary = Path(path).with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def train(args):
    import torch
    from torch.utils.data import DataLoader, Dataset

    if args.epochs < 1 or args.batch_size < 1 or args.learning_rate <= 0:
        raise ValueError("Invalid optimizer or training parameters")
    if args.min_class_samples < 5 and not args.pilot_run:
        raise ValueError("Small class counts require --pilot-run; they cannot support a general skill claim")
    samples_path = Path(args.samples).resolve()
    groups = read_samples(samples_path, threshold_mm=args.threshold_mm, min_class_samples=args.min_class_samples)
    seed_all(args.seed)
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("Output directory already exists; choose a new run directory")
    state = fit_preprocessor(numeric_rows(groups["train"])) if args.with_numeric else None
    if args.with_numeric and not np.isfinite(numeric_rows(groups["train"])).any():
        raise ValueError("No verified at-issue numeric inputs; use the image-only baseline")
    if args.with_numeric:
        for group in groups.values():
            for row in group:
                if np.isfinite(numeric_rows([row])).any():
                    availability = row.get("numeric_available_at_kst") or row.get("available_at_kst")
                    if not availability or timestamp(availability) > row["_issue"]:
                        raise ValueError("Numeric inputs need proven availability at or before the forecast origin")

    class Samples(Dataset):
        def __init__(self, rows):
            self.rows = rows
            self.numeric = transform_numeric(numeric_rows(rows), state) if state else np.zeros((len(rows), 0), dtype=np.float32)

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, index):
            row = self.rows[index]
            return (torch.from_numpy(image_stack(row["_frames"])),
                    torch.from_numpy(self.numeric[index]), torch.tensor(float(row["_label"])))

    device = torch.device(args.device)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA is unavailable")
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise ValueError("MPS is unavailable")
    model = make_model(use_numeric=args.with_numeric).to(device)
    train_labels = np.asarray([row["_label"] for row in groups["train"]])
    prevalence = float(train_labels.mean())
    pos_weight = float((train_labels == 0).sum() / (train_labels == 1).sum())
    criterion = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_weight, device=device))
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    generator = torch.Generator().manual_seed(args.seed)
    loaders = {name: DataLoader(Samples(group), batch_size=args.batch_size, shuffle=name == "train",
                               num_workers=0, generator=generator if name == "train" else None)
               for name, group in groups.items()}
    model_config = {"use_numeric": args.with_numeric, "numeric_size": 2 * len(NUMERIC_FEATURES)}
    output.mkdir(parents=True)
    config = {"model_version": MODEL_VERSION, "task": "station108_next1h_rain_ge_threshold",
              "threshold_mm": args.threshold_mm, "lead_time_hours": 1, "history_hours": 4,
              "image_units": "packed DN divided by 8191, with four validity-mask channels",
              "model_config": model_config, "samples_csv": str(samples_path), "samples_sha256": digest(samples_path),
              "numeric_preprocessor": state, "seed": args.seed, "device": args.device,
              "epochs": args.epochs, "batch_size": args.batch_size, "learning_rate": args.learning_rate,
              "pos_weight_from_train": pos_weight, "train_prevalence": prevalence,
              "time_policy": "retrospective_observation_time; not operational delivery validation",
              "numeric_policy": "verified availability only" if args.with_numeric else "disabled; publication availability unknown",
              "split_counts": {name: {"samples": len(group), "positives": sum(row["_label"] for row in group),
                                     "first_issue": min(row["_issue"] for row in group).isoformat(),
                                     "last_issue": max(row["_issue"] for row in group).isoformat()}
                               for name, group in groups.items()},
              "source_code_sha256": digest(__file__), "synthetic": False,
              "torch_version": str(torch.__version__), "numpy_version": str(np.__version__),
              "pilot_run": args.pilot_run,
              "evaluation_scope": "selected event windows; engineering verification, not representative forecast skill" if args.pilot_run else "retrospective temporal holdout"}
    write_json(output / "config.json", config)

    def predict(loader):
        model.eval()
        labels, probabilities = [], []
        with torch.no_grad():
            for images, numeric, target in loader:
                prediction = model(images.to(device), numeric.to(device) if args.with_numeric else None)
                labels.extend(target.tolist())
                probabilities.extend(torch.sigmoid(prediction).cpu().tolist())
        return np.asarray(labels, dtype=np.int8), np.asarray(probabilities)

    history, best = [], -1.0
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss, count = 0.0, 0
        for images, numeric, target in loaders["train"]:
            optimizer.zero_grad(set_to_none=True)
            logits = model(images.to(device), numeric.to(device) if args.with_numeric else None)
            loss = criterion(logits, target.to(device))
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite training loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            total_loss += float(loss.detach().cpu()) * len(target)
            count += len(target)
        labels, probabilities = predict(loaders["val"])
        metrics = classification_metrics(labels, probabilities)
        record = {"epoch": epoch, "train_loss": total_loss / count, "validation": metrics}
        history.append(record)
        print(json.dumps({"epoch": epoch, "train_loss": record["train_loss"],
                          "val_average_precision": metrics["average_precision"]}), flush=True)
        if metrics["average_precision"] > best:
            best = metrics["average_precision"]
            temporary = output / "model.pt.tmp"
            torch.save({"model_state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()},
                        "model_config": model_config, "config": config, "best_epoch": epoch}, temporary)
            temporary.replace(output / "model.pt")
        write_json(output / "history.json", history)

    checkpoint = torch.load(output / "model.pt", map_location=device, weights_only=True)
    model.load_state_dict(checkpoint["model_state_dict"])
    val_labels, val_probabilities = predict(loaders["val"])
    decision = select_threshold(val_labels, val_probabilities)
    test_labels, test_probabilities = predict(loaders["test"])
    report = {"synthetic": False, "pilot_run": args.pilot_run, "evaluation": config["evaluation_scope"],
              "best_epoch": checkpoint["best_epoch"], "selection_metric": "validation average_precision",
              "decision_threshold_source": "validation F1 only", "decision_threshold": decision,
              "validation": classification_metrics(val_labels, val_probabilities, decision),
              "test": classification_metrics(test_labels, test_probabilities, decision),
              "baselines": {"no_rain": classification_metrics(test_labels, np.zeros(len(test_labels)), 0.5),
                            "train_prevalence": classification_metrics(test_labels, np.full(len(test_labels), prevalence), 0.5)},
              "samples_sha256": config["samples_sha256"], "checkpoint_sha256": digest(output / "model.pt")}
    # A persistence baseline requires complete, proven-at-issue past-rain inputs.
    if args.with_numeric and all(math.isfinite(numeric_value(row.get("past_rain_1h_mm"))) for row in groups["test"]):
        persistence = np.asarray([float(row["past_rain_1h_mm"]) >= args.threshold_mm for row in groups["test"]], dtype=float)
        report["baselines"]["rain_persistence"] = classification_metrics(test_labels, persistence, 0.5)
    write_json(output / "metrics.json", report)
    with (output / "test_predictions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("sample_id", "issue_time_kst", "rain_1h_mm", "label", "probability", "prediction"), lineterminator="\n")
        writer.writeheader()
        for row, probability in zip(groups["test"], test_probabilities):
            writer.writerow({**{key: row[key] for key in ("sample_id", "issue_time_kst", "rain_1h_mm", "label")},
                             "probability": float(probability), "prediction": int(probability >= decision)})
    return report


def smoke_test(output, *, seed=42):
    import torch
    seed_all(seed)
    output = Path(output)
    if output.exists():
        raise ValueError("Smoke-test output already exists; choose a fresh directory")
    output.mkdir(parents=True)
    model = make_model()
    images = torch.rand(8, 8, 16, 16)
    labels = torch.tensor([0., 1.] * 4)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    losses = []
    for _ in range(2):
        model.train()
        optimizer.zero_grad(set_to_none=True)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(model(images), labels)
        loss.backward()
        optimizer.step()
        losses.append(float(loss.detach()))
    model.eval()
    with torch.no_grad():
        expected = model(images)
    torch.save({"model_config": {"use_numeric": False, "numeric_size": 2 * len(NUMERIC_FEATURES)},
                "model_state_dict": model.state_dict(), "synthetic": True}, output / "model.pt")
    checkpoint = torch.load(output / "model.pt", map_location="cpu", weights_only=True)
    restored = make_model(**checkpoint["model_config"])
    restored.load_state_dict(checkpoint["model_state_dict"])
    restored.eval()
    with torch.no_grad():
        identical = bool(torch.allclose(expected, restored(images), atol=1e-7))
    if not all(math.isfinite(value) for value in losses) or not identical:
        raise RuntimeError("Synthetic optimizer/checkpoint smoke test failed")
    report = {"synthetic": True, "purpose": "optimizer and checkpoint verification only; no rainfall skill measured",
              "optimization_steps": 2, "losses": losses, "checkpoint_roundtrip_equal": identical,
              "checkpoint_sha256": digest(output / "model.pt"), "seed": seed}
    write_json(output / "smoke-test.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, help="dataset.py samples.csv")
    parser.add_argument("--output", type=Path, required=True, help="New local run directory")
    parser.add_argument("--threshold-mm", type=float, default=30.0)
    parser.add_argument("--min-class-samples", type=int, default=5)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=("cpu", "cuda", "mps"), default="cpu")
    parser.add_argument("--with-numeric", action="store_true", help="Require proven-at-issue numeric inputs")
    parser.add_argument("--pilot-run", action="store_true", help="Mark small/event-selected real-data runs as engineering pilots only")
    parser.add_argument("--smoke-test", action="store_true", help="Synthetic optimization/checkpoint check only")
    args = parser.parse_args()
    if args.smoke_test:
        result = smoke_test(args.output, seed=args.seed)
    else:
        if args.samples is None:
            parser.error("--samples is required for real training")
        result = train(args)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ImportError) as exc:
        raise SystemExit(f"Training stopped: {exc}") from None
