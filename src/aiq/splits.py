"""The three train/test splitters.

Two honest ones and a deliberately bad one. The random split is there to be
reported, not believed: consecutive weeks for the same player look almost
identical, so shuffling them lets the model recognise rows instead of predicting
them.

Only two honest folds are possible. There are three full seasons plus a short
stub of 2025/26 carrying 43 events, which is too few to test on.
"""

from typing import NamedTuple

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.model_selection import KFold

# Fold indices are positional into the panel.
Ints = NDArray[np.int64]

# European seasons start in July.
SEASON_START_MONTH = 7

# A test fold below this many positives gives a PR-AUC too unstable to report.
MIN_TEST_EVENTS = 200


class Fold(NamedTuple):
    """One train/test split, as positional indices into the panel."""

    name: str
    train: Ints
    test: Ints


def season_of(mondays: pd.Series) -> pd.Series:
    """Label each Monday with its European season, e.g. 2023/24."""
    stamps = pd.to_datetime(mondays)
    start_year = stamps.dt.year.where(stamps.dt.month >= SEASON_START_MONTH, stamps.dt.year - 1)
    return start_year.astype(str) + "/" + (start_year + 1).astype(str).str[-2:]


def naive_random_folds(panel: pd.DataFrame, *, n_splits: int = 5, seed: int = 0) -> list[Fold]:
    """Random K-fold over player-weeks. Dishonest by construction.

    Built to be reported, not to be believed. Adjacent weeks of the same player share
    almost all their features and correlated labels, so random assignment leaks the
    test set into training through nothing more exotic than autocorrelation.
    """
    splitter = KFold(n_splits=n_splits, shuffle=True, random_state=seed)
    positions = np.arange(len(panel))
    return [
        Fold(f"naive_fold{index}", train, test)
        for index, (train, test) in enumerate(splitter.split(positions), start=1)
    ]


def _chain(panel: pd.DataFrame, *, group_players: bool, min_test_events: int) -> list[Fold]:
    """Shared body of the two honest splitters."""
    season = season_of(panel.monday)
    order = sorted(season.unique())
    positions = np.arange(len(panel))
    folds: list[Fold] = []

    for index in range(1, len(order)):
        test_season = order[index]
        is_test = (season == test_season).to_numpy()
        is_train = np.isin(season.to_numpy(), order[:index])
        if panel.label_injury.to_numpy()[is_test].sum() < min_test_events:
            continue

        if group_players:
            # Generalisation to a player never seen in training, a new signing.
            test_players = set(panel.player_id.to_numpy()[is_test])
            is_train = is_train & ~panel.player_id.isin(test_players).to_numpy()

        prefix = "grouped" if group_players else "temporal"
        folds.append(Fold(f"{prefix}_{test_season}", positions[is_train], positions[is_test]))
    return folds


def forward_chain_folds(
    panel: pd.DataFrame, *, min_test_events: int = MIN_TEST_EVENTS
) -> list[Fold]:
    """Train on every season up to T, test on T+1. The deployment-faithful split.

    A club predicts for players it already has history on, so those players belong in
    training. Holding them out would answer a different question and understate what
    the model can actually do in the job it is for.
    """
    return _chain(panel, group_players=False, min_test_events=min_test_events)


def forward_chain_grouped_folds(
    panel: pd.DataFrame, *, min_test_events: int = MIN_TEST_EVENTS
) -> list[Fold]:
    """As above, with the test season's players removed from training.

    Answers the new-signing question: how well does this generalise to a player with
    no history in the model at all? Strictly harder, and not the deployment case.
    """
    return _chain(panel, group_players=True, min_test_events=min_test_events)


def all_splitters() -> dict[str, object]:
    """The three splitters by name, for iterating in the metrics report."""
    return {
        "naive_random": naive_random_folds,
        "honest_temporal": forward_chain_folds,
        "honest_grouped": forward_chain_grouped_folds,
    }
