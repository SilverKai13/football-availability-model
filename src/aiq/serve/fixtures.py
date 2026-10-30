"""Synthetic artefacts for tests and smoke checks.

Nothing here resembles a real player. The point is a predictions file that passes
the contract, with enough structure (several squads, several Mondays, a base rate
near the real one) for the API and the deployment checks to exercise every path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from aiq.models import FEATURE_COLUMNS
from aiq.serve.contract import METRICS_FILE, PREDICTIONS_FILE

POSITION_IDS = (24, 25, 26, 27)


def synthetic_predictions(
    *, teams: int = 3, weeks: int = 4, players_per_team: int = 18, seed: int = 0
) -> pd.DataFrame:
    """One row per player per Monday, risks around a 2% base rate."""
    rng = np.random.default_rng(seed)
    mondays = pd.date_range("2025-08-04", periods=weeks, freq="7D")  # Mondays
    rows = []
    for t in range(teams):
        team_id = 1000 + t
        league_id = (8, 82, 564)[t % 3]
        for p in range(players_per_team):
            player_id = team_id * 100 + p
            base = float(np.clip(rng.beta(1.2, 50), 0.001, 0.075))
            for monday in mondays:
                risk = float(np.clip(base * rng.lognormal(0, 0.3), 0.0005, 0.075))
                rows.append(
                    {
                        "player_id": player_id,
                        "monday": monday,
                        "team_id": team_id,
                        "league_id": league_id,
                        "risk": risk,
                        "label_injury": int(rng.random() < risk),
                        "player_name": f"Player {player_id}",
                        "position_id": float(POSITION_IDS[p % 4]),
                    }
                )
    frame = pd.DataFrame(rows)
    contributions = rng.normal(0, 0.3, size=(len(frame), len(FEATURE_COLUMNS)))
    for i, name in enumerate(FEATURE_COLUMNS):
        frame[f"contrib_{name}"] = contributions[:, i]
    frame["label_injury"] = frame.label_injury.astype("int8")
    return frame


def synthetic_teams(frame: pd.DataFrame) -> pd.DataFrame:
    ids = sorted(int(t) for t in frame.team_id.unique())
    return pd.DataFrame({"id": ids, "name": [f"Synthetic FC {i - 999}" for i in ids]})


def synthetic_metrics() -> pd.DataFrame:
    """A metrics table in the shape ``aiq train`` writes, with made-up values."""
    rows = []
    for splitter in ("naive_random", "honest_temporal"):
        for model, pr_auc in (
            ("base_rate", 0.020),
            ("prior_injuries", 0.029),
            ("logistic_regression", 0.038),
        ):
            rows.append(
                {
                    "base_rate": 0.02,
                    "pr_auc": pr_auc,
                    "roc_auc": 0.6,
                    "brier": 0.0195,
                    "brier_skill": 0.005,
                    "ece": 0.004,
                    "precision_at_5": 0.03,
                    "n": 1000.0,
                    "positives": 20.0,
                    "splitter": splitter,
                    "fold": "fold1",
                    "model": model,
                    "label": "label_injury",
                    "train_n": 5000,
                    "train_events": 100,
                }
            )
    return pd.DataFrame(rows)


def write_synthetic_artifacts(
    directory: Path, *, teams: int = 3, weeks: int = 4, players_per_team: int = 18, seed: int = 0
) -> pd.DataFrame:
    """Write predictions, teams and metrics files under ``directory``; return the predictions."""
    directory.mkdir(parents=True, exist_ok=True)
    frame = synthetic_predictions(
        teams=teams, weeks=weeks, players_per_team=players_per_team, seed=seed
    )
    frame.to_parquet(directory / PREDICTIONS_FILE, index=False)
    synthetic_teams(frame).to_parquet(directory / "teams.parquet", index=False)
    synthetic_metrics().to_parquet(directory / METRICS_FILE, index=False)
    return frame
