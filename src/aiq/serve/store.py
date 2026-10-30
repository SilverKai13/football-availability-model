"""The in-memory predictions table and what the API asks of it.

One frame, swapped atomically when a new manifest appears. Reads never block on
a reload: the old frame answers until the new one is ready.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from aiq.serve.contract import (
    CONTRIBUTION_COLUMNS,
    MANIFEST_FILE,
    METRICS_FILE,
    PREDICTIONS_FILE,
    Manifest,
    validate_predictions,
)

log = logging.getLogger(__name__)

POSITIONS = {24: "GK", 25: "DEF", 26: "MID", 27: "ATT"}

READABLE = {
    "minutes_7d": "minutes, last 7 days",
    "minutes_14d": "minutes, last 14 days",
    "minutes_28d": "minutes, last 28 days",
    "matches_7d": "matches, last 7 days",
    "matches_14d": "matches, last 14 days",
    "matches_28d": "matches, last 28 days",
    "away_7d": "away trips, last 7 days",
    "away_14d": "away trips, last 14 days",
    "away_28d": "away trips, last 28 days",
    "short_7d": "short turnarounds, last 7 days",
    "short_14d": "short turnarounds, last 14 days",
    "short_28d": "short turnarounds, last 28 days",
    "acwr_minutes": "acute:chronic workload",
    "days_since_last_match": "days since last match",
    "team_fixtures_7d": "team fixtures last week",
    "team_fixtures_14d": "team fixtures, last fortnight",
    "team_fixtures_next_7d": "team fixtures scheduled next week",
    "post_intl_window": "week after an international break",
    "off_season": "off-season week",
    "absences_365d": "absences in the last year",
    "absence_days_365d": "days absent in the last year",
    "injury_absences_365d": "injuries in the last year",
    "days_since_return": "days since last return",
    "history_weeks": "weeks of history we can see",
    "age_years": "age",
    "position_id": "position",
    "league_id": "league",
}


def _label(value: Any) -> bool | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return bool(int(value))


class NotLoadedError(RuntimeError):
    pass


class NotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Loaded:
    frame: pd.DataFrame
    manifest: Manifest | None
    metrics: pd.DataFrame | None
    base_rate: float
    loaded_at: float
    manifest_mtime: float


class PredictionStore:
    def __init__(self, artifact_dir: Path, teams_path: Path | None = None) -> None:
        self.artifact_dir = artifact_dir
        self.teams_path = teams_path
        self._lock = threading.Lock()
        self._loaded: Loaded | None = None
        self._attempted_mtime = 0.0  # a manifest that failed to load; not retried
        self._team_names: dict[int, str] = {}
        self.reloads = 0
        self.failed_reloads = 0

    # ---------------------------------------------------------------- loading

    @property
    def ready(self) -> bool:
        return self._loaded is not None

    def load(self) -> Loaded:
        """Read the files. Raises if they are missing or fail the contract."""
        predictions = self.artifact_dir / PREDICTIONS_FILE
        frame = pd.read_parquet(predictions)
        problems = validate_predictions(frame)
        if problems:
            raise ValueError("predictions file fails the contract: " + "; ".join(problems))
        frame["monday"] = pd.to_datetime(frame.monday)
        frame = frame.sort_values(["monday", "team_id", "risk"], ascending=[True, True, False])
        frame = frame.reset_index(drop=True)

        manifest_path = self.artifact_dir / MANIFEST_FILE
        manifest = Manifest.read(manifest_path) if manifest_path.exists() else None
        metrics_path = self.artifact_dir / METRICS_FILE
        metrics = pd.read_parquet(metrics_path) if metrics_path.exists() else None
        if self.teams_path and self.teams_path.exists():
            teams = pd.read_parquet(self.teams_path)
            self._team_names = {int(i): str(n) for i, n in zip(teams.id, teams.name, strict=False)}

        loaded = Loaded(
            frame=frame,
            manifest=manifest,
            metrics=metrics,
            base_rate=float(frame.label_injury.mean()),
            loaded_at=time.time(),
            manifest_mtime=manifest_path.stat().st_mtime if manifest_path.exists() else 0.0,
        )
        with self._lock:
            self._loaded = loaded
        self.reloads += 1
        log.info(
            "loaded %d player-weeks, %s to %s, version %s",
            len(frame),
            frame.monday.min().date(),
            frame.monday.max().date(),
            manifest.version if manifest else "none",
        )
        return loaded

    def maybe_reload(self) -> bool:
        """Reload if the manifest changed since the last load. Returns whether it did."""
        manifest_path = self.artifact_dir / MANIFEST_FILE
        current = self._loaded
        if current is None:
            return False
        if not manifest_path.exists():
            return False
        mtime = manifest_path.stat().st_mtime
        if mtime <= current.manifest_mtime or mtime == self._attempted_mtime:
            return False
        self._attempted_mtime = mtime
        try:
            self.load()
        except (OSError, ValueError) as exc:
            self.failed_reloads += 1
            log.error("reload failed, keeping the previous predictions: %s", exc)
            return False
        return True

    def _current(self) -> Loaded:
        loaded = self._loaded
        if loaded is None:
            raise NotLoadedError("predictions are not loaded")
        return loaded

    # ---------------------------------------------------------------- facts

    def age_seconds(self) -> float | None:
        """Seconds since the predictions were published, from the manifest."""
        loaded = self._loaded
        if loaded is None or loaded.manifest is None:
            return None
        published = pd.Timestamp(loaded.manifest.published_at)
        if published.tzinfo is None:
            published = published.tz_localize("UTC")
        return float((pd.Timestamp.now(tz="UTC") - published).total_seconds())

    def rows(self) -> int:
        loaded = self._loaded
        return 0 if loaded is None else int(len(loaded.frame))

    def team_name(self, team_id: int) -> str:
        return self._team_names.get(int(team_id), f"team {int(team_id)}")

    def weeks(self, team_id: int | None = None) -> list[date]:
        frame = self._current().frame
        if team_id is not None:
            frame = frame[frame.team_id == team_id]
        return [pd.Timestamp(m).date() for m in sorted(frame.monday.unique())]

    def latest_week(self) -> date:
        return self.weeks()[-1]

    def teams(self) -> list[dict[str, Any]]:
        frame = self._current().frame
        rows = []
        for team_id, league_id in (
            frame[["team_id", "league_id"]]
            .drop_duplicates()
            .sort_values("team_id")
            .itertuples(index=False)
        ):
            rows.append(
                {
                    "team_id": int(team_id),
                    "league_id": int(league_id),
                    "name": self.team_name(team_id),
                }
            )
        return rows

    def squad(self, team_id: int, monday: date, *, shortlist: int, top: int) -> dict[str, Any]:
        loaded = self._current()
        frame = loaded.frame
        rows = frame[(frame.team_id == team_id) & (frame.monday == pd.Timestamp(monday))]
        if rows.empty:
            if (frame.team_id == team_id).sum() == 0:
                raise NotFoundError(f"no squad with team_id {team_id}")
            raise NotFoundError(f"team {team_id} has no risk set for the week of {monday}")
        rows = rows.sort_values("risk", ascending=False)
        players = [self._player_row(row, loaded.base_rate, top) for row in rows.itertuples()]
        for rank, player in enumerate(players, start=1):
            player["rank"] = rank
            player["shortlisted"] = rank <= shortlist
        return {
            "team_id": int(team_id),
            "team": self.team_name(team_id),
            "league_id": int(rows.league_id.iloc[0]),
            "week": str(monday),
            "base_rate": loaded.base_rate,
            "shortlist": shortlist,
            "players": players,
        }

    def player(self, player_id: int, monday: date | None, *, top: int) -> dict[str, Any]:
        loaded = self._current()
        frame = loaded.frame
        rows = frame[frame.player_id == player_id]
        if rows.empty:
            raise NotFoundError(f"no player with player_id {player_id}")
        if monday is not None:
            rows = rows[rows.monday == pd.Timestamp(monday)]
            if rows.empty:
                raise NotFoundError(f"player {player_id} has no risk for the week of {monday}")
        row = rows.sort_values("monday").iloc[-1]
        result = self._player_row(row, loaded.base_rate, top)
        result["team_id"] = int(row.team_id)
        result["team"] = self.team_name(int(row.team_id))
        result["week"] = str(pd.Timestamp(row.monday).date())
        result["base_rate"] = loaded.base_rate
        result["weeks_available"] = [str(d) for d in self.weeks_for_player(player_id)]
        return result

    def weeks_for_player(self, player_id: int) -> list[date]:
        frame = self._current().frame
        rows = frame[frame.player_id == player_id]
        return [pd.Timestamp(m).date() for m in sorted(rows.monday.unique())]

    def _player_row(self, row: Any, base_rate: float, top: int) -> dict[str, Any]:
        risk = float(row.risk)
        contributions: list[dict[str, Any]] = []
        for column in CONTRIBUTION_COLUMNS:
            value = getattr(row, column, None)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue
            feature = column.removeprefix("contrib_")
            contributions.append(
                {
                    "feature": feature,
                    "label": READABLE.get(feature, feature),
                    "log_odds": round(float(value), 4),
                }
            )
        contributions.sort(key=lambda c: abs(c["log_odds"]), reverse=True)
        name = row.player_name
        position = row.position_id
        return {
            "player_id": int(row.player_id),
            "name": None
            if name is None or (isinstance(name, float) and math.isnan(name))
            else str(name),
            "position": POSITIONS.get(int(position)) if position == position else None,
            "risk": round(risk, 5),
            "vs_base_rate": round(risk / base_rate, 2) if base_rate > 0 else None,
            "became_unavailable": _label(row.label_injury),
            "contributions": contributions[:top],
        }

    def model_card(self, version: str) -> dict[str, Any]:
        """What is loaded, how it did on the honest split, and what it cannot see."""
        loaded = self._current()
        manifest = loaded.manifest
        card: dict[str, Any] = {
            "model": manifest.model if manifest else "logistic_regression",
            "version": manifest.version if manifest else version,
            "published_at": manifest.published_at if manifest else None,
            "predictions_sha256": manifest.sha256 if manifest else None,
            "rows": int(len(loaded.frame)),
            "weeks_from": str(loaded.frame.monday.min().date()),
            "weeks_to": str(loaded.frame.monday.max().date()),
            "base_rate": loaded.base_rate,
            "mean_risk": float(loaded.frame.risk.mean()),
            "max_risk": float(loaded.frame.risk.max()),
            "teams": int(loaded.frame.team_id.nunique()),
            "players": int(loaded.frame.player_id.nunique()),
            "what_it_cannot_see": [
                "training load: only match minutes are observed",
                "Champions League, Europa League and international fixtures",
                "absences that were never reported; the label is a Transfermarkt record",
            ],
            "how_to_read_it": (
                "A calibrated probability for the next seven days. Read every number against "
                "the base rate. On precision at five it does not beat counting a player's "
                "injuries over the last year; treat it as one weak input, not a recommendation."
            ),
        }
        if loaded.metrics is not None:
            honest = loaded.metrics[loaded.metrics.splitter == "honest_temporal"]
            if not honest.empty:
                summary = (
                    honest.groupby("model")[["pr_auc", "ece", "brier_skill", "precision_at_5"]]
                    .mean()
                    .round(4)
                )
                card["honest_metrics"] = {
                    str(model): {k: float(v) for k, v in row.items()}
                    for model, row in summary.iterrows()
                }
        return card
