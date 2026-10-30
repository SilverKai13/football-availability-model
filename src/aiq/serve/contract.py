"""The predictions file contract, and the manifest that travels with it.

The weekly job and the API agree on exactly this. A file that fails
``validate_predictions`` is never published, so the API can assume the shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from aiq.models import FEATURE_COLUMNS

PREDICTIONS_FILE = "predictions.parquet"
MANIFEST_FILE = "manifest.json"
METRICS_FILE = "metrics.parquet"

#: Columns every predictions file carries, before the contributions.
BASE_COLUMNS = (
    "player_id",
    "monday",
    "team_id",
    "league_id",
    "risk",
    "label_injury",
    "player_name",
    "position_id",
)
CONTRIBUTION_COLUMNS = tuple(f"contrib_{name}" for name in FEATURE_COLUMNS)
REQUIRED_COLUMNS = BASE_COLUMNS + CONTRIBUTION_COLUMNS


def validate_predictions(frame: pd.DataFrame) -> list[str]:
    """Every reason the frame is not a predictions file. Empty means it is one."""
    problems: list[str] = []
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        problems.append(f"missing columns: {', '.join(missing)}")
        return problems  # the rest would only repeat this
    if frame.empty:
        problems.append("no rows")
        return problems
    if frame.risk.isna().any() or ((frame.risk < 0) | (frame.risk > 1)).any():
        problems.append("risk must be a probability in [0, 1] with no missing values")
    if not frame.label_injury.isin([0, 1]).all():
        problems.append("label_injury must be 0 or 1")
    mondays = pd.to_datetime(frame.monday, errors="coerce")
    if mondays.isna().any():
        problems.append("monday must be a date")
    elif (mondays.dt.dayofweek != 0).any():
        problems.append("every monday must fall on a Monday")
    if frame.duplicated(["player_id", "monday"]).any():
        problems.append("duplicate (player_id, monday) rows")
    for column in ("player_id", "team_id", "league_id"):
        if not pd.api.types.is_integer_dtype(frame[column]):
            problems.append(f"{column} must be an integer column")
    return problems


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Manifest:
    """What was published, by which code, from what. Written after the file it describes."""

    published_at: str
    rows: int
    weeks_from: str
    weeks_to: str
    base_rate: float
    mean_risk: float
    sha256: str
    model: str = "logistic_regression"
    version: str = "dev"
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def describe(
        cls, frame: pd.DataFrame, *, sha256: str, version: str, source: str = ""
    ) -> Manifest:
        mondays = pd.to_datetime(frame.monday)
        return cls(
            published_at=datetime.now(UTC).isoformat(timespec="seconds"),
            rows=int(len(frame)),
            weeks_from=str(mondays.min().date()),
            weeks_to=str(mondays.max().date()),
            base_rate=float(frame.label_injury.mean()),
            mean_risk=float(frame.risk.mean()),
            sha256=sha256,
            version=version,
            source=source,
        )

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Manifest:
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)
