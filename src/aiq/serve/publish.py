"""Publish a scored predictions file where the API reads it.

The hand-off between the weekly job and the API is two files in one directory:
``predictions.parquet`` and ``manifest.json``. The parquet is written to a temporary
name and renamed into place, so a reader never sees a half-written file, and the
manifest is written last, so a reader that watches the manifest only reloads once
the data it describes is complete. The previous version is kept under ``history/``
for a rollback that is a copy, not a re-run.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from aiq.serve.contract import (
    MANIFEST_FILE,
    METRICS_FILE,
    PREDICTIONS_FILE,
    Manifest,
    file_sha256,
    validate_predictions,
)


class PublishError(ValueError):
    pass


def publish(source: Path, destination: Path, *, version: str = "dev", keep: int = 3) -> Manifest:
    """Validate ``source/predictions.parquet`` and install it into ``destination``.

    Returns the manifest that was written. Raises ``PublishError`` with every problem
    found, and leaves the destination untouched in that case.
    """
    predictions = source / PREDICTIONS_FILE
    if not predictions.exists():
        raise PublishError(f"{predictions} does not exist")
    frame = pd.read_parquet(predictions)
    problems = validate_predictions(frame)
    if problems:
        raise PublishError("not a valid predictions file: " + "; ".join(problems))

    destination.mkdir(parents=True, exist_ok=True)
    _archive_current(destination, keep=keep)

    staged = destination / (PREDICTIONS_FILE + ".tmp")
    shutil.copyfile(predictions, staged)
    os.replace(staged, destination / PREDICTIONS_FILE)

    metrics = source / METRICS_FILE
    if metrics.exists():
        staged_metrics = destination / (METRICS_FILE + ".tmp")
        shutil.copyfile(metrics, staged_metrics)
        os.replace(staged_metrics, destination / METRICS_FILE)

    manifest = Manifest.describe(
        frame,
        sha256=file_sha256(destination / PREDICTIONS_FILE),
        version=version,
        source=str(predictions),
    )
    manifest.write(destination / MANIFEST_FILE)
    return manifest


def _archive_current(destination: Path, *, keep: int) -> None:
    """Move the currently published files under history/<published_at>/, keeping ``keep``."""
    current = destination / PREDICTIONS_FILE
    manifest_path = destination / MANIFEST_FILE
    if not current.exists():
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    if manifest_path.exists():
        with contextlib.suppress(ValueError, KeyError, TypeError):
            previous = Manifest.read(manifest_path)
            published = previous.published_at.replace(":", "").replace("+00:00", "Z")
            stamp = f"{published}-{previous.sha256[:8]}"
    history = destination / "history"
    slot = history / stamp
    slot.mkdir(parents=True, exist_ok=True)
    for name in (PREDICTIONS_FILE, MANIFEST_FILE, METRICS_FILE):
        path = destination / name
        if path.exists():
            shutil.copy2(path, slot / name)
    if keep >= 0:
        slots = sorted(p for p in history.iterdir() if p.is_dir())
        for old in slots[: max(0, len(slots) - keep)]:
            shutil.rmtree(old, ignore_errors=True)


def rollback(destination: Path) -> Manifest:
    """Reinstate the most recent history entry. Returns its manifest."""
    history = destination / "history"
    slots = sorted(p for p in history.iterdir() if p.is_dir()) if history.exists() else []
    if not slots:
        raise PublishError("nothing in history to roll back to")
    slot = slots[-1]
    for name in (PREDICTIONS_FILE, METRICS_FILE):
        if (slot / name).exists():
            staged = destination / (name + ".tmp")
            shutil.copyfile(slot / name, staged)
            os.replace(staged, destination / name)
    manifest = Manifest.read(slot / MANIFEST_FILE)
    manifest.write(destination / MANIFEST_FILE)
    shutil.rmtree(slot, ignore_errors=True)
    return manifest
