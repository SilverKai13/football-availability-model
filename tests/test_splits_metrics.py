"""Tests for the three splitters and the metric suite.

The splitter tests are leakage tests: an honest fold must never train on a row dated
at or after its test season. No test reaches the network or opens the corpus.
"""

import numpy as np
import pandas as pd
import pytest

from aiq.metrics import (
    brier,
    brier_skill_score,
    expected_calibration_error,
    pr_auc,
    precision_at_k,
)
from aiq.splits import (
    forward_chain_folds,
    forward_chain_grouped_folds,
    naive_random_folds,
    season_of,
)


@pytest.fixture
def panel() -> pd.DataFrame:
    """Four seasons, 40 players, one row per player-week."""
    weeks = pd.date_range("2022-08-01", "2026-05-25", freq="7D")
    rows = [
        {"player_id": pid, "monday": week, "team_id": pid % 4, "label_injury": 0}
        for week in weeks
        for pid in range(40)
    ]
    frame = pd.DataFrame(rows)
    rng = np.random.default_rng(0)
    frame["label_injury"] = rng.binomial(1, 0.25, len(frame))
    return frame


def test_season_of_starts_in_july() -> None:
    seasons = season_of(pd.Series(pd.to_datetime(["2023-06-30", "2023-07-01", "2024-01-15"])))
    assert list(seasons) == ["2022/23", "2023/24", "2023/24"]


def test_forward_chain_never_trains_on_the_test_season_or_later(panel: pd.DataFrame) -> None:
    """The defining honest-split test."""
    seasons = season_of(panel.monday)
    for fold in forward_chain_folds(panel, min_test_events=1):
        test_season = seasons.iloc[fold.test].unique()
        assert len(test_season) == 1, "a fold must test exactly one season"
        train_seasons = set(seasons.iloc[fold.train])
        assert all(s < test_season[0] for s in train_seasons), "training saw the future"


def test_forward_chain_folds_grow(panel: pd.DataFrame) -> None:
    folds = forward_chain_folds(panel, min_test_events=1)
    sizes = [len(f.train) for f in folds]
    assert sizes == sorted(sizes), "each fold should train on strictly more history"


def test_grouped_split_shares_no_players(panel: pd.DataFrame) -> None:
    """The new-signing question: no test player may appear in training."""
    for fold in forward_chain_grouped_folds(panel, min_test_events=1):
        train_players = set(panel.player_id.iloc[fold.train])
        test_players = set(panel.player_id.iloc[fold.test])
        assert not (train_players & test_players), "a test player leaked into training"


def test_ungrouped_split_does_share_players(panel: pd.DataFrame) -> None:
    """Deliberately so — a club predicts for players it has history on."""
    folds = forward_chain_folds(panel, min_test_events=1)
    overlaps = [
        set(panel.player_id.iloc[f.train]) & set(panel.player_id.iloc[f.test]) for f in folds
    ]
    assert any(overlaps), "the deployment-faithful split should reuse players"


def test_thin_test_seasons_are_skipped(panel: pd.DataFrame) -> None:
    """A fold with too few positives gives an unstable PR-AUC; skip, do not report."""
    assert forward_chain_folds(panel, min_test_events=10_000) == []


def test_naive_split_covers_every_row_exactly_once(panel: pd.DataFrame) -> None:
    folds = naive_random_folds(panel, n_splits=5)
    test_rows = np.concatenate([f.test for f in folds])
    assert len(test_rows) == len(panel)
    assert len(set(test_rows)) == len(panel)


def test_naive_split_mixes_seasons_which_is_the_whole_problem(panel: pd.DataFrame) -> None:
    """Its failure mode, asserted so the report can point at it."""
    seasons = season_of(panel.monday)
    fold = naive_random_folds(panel, n_splits=5)[0]
    assert len(set(seasons.iloc[fold.train])) > 1
    assert set(seasons.iloc[fold.test]) & set(seasons.iloc[fold.train])


def test_pr_auc_floor_is_the_base_rate() -> None:
    """Unlike ROC-AUC, a random model scores the base rate, not 0.5."""
    rng = np.random.default_rng(0)
    y = rng.binomial(1, 0.02, 20_000)
    random_scores = rng.random(20_000)
    assert pr_auc(y, random_scores) == pytest.approx(0.02, abs=0.01)


def test_brier_skill_is_zero_for_a_base_rate_prediction() -> None:
    y = np.array([0, 0, 0, 1] * 50)
    constant = np.full(len(y), y.mean())
    assert brier_skill_score(y, constant) == pytest.approx(0.0, abs=1e-12)


def test_brier_rewards_a_confident_correct_prediction() -> None:
    y = np.array([1, 0])
    assert brier(y, np.array([0.9, 0.1])) < brier(y, np.array([0.6, 0.4]))


def test_ece_is_zero_for_a_perfectly_calibrated_model() -> None:
    rng = np.random.default_rng(1)
    p = rng.uniform(0.05, 0.95, 40_000)
    y = rng.binomial(1, p)
    assert expected_calibration_error(y, p) < 0.02


def test_ece_catches_systematic_overconfidence() -> None:
    rng = np.random.default_rng(2)
    p = rng.uniform(0.05, 0.95, 20_000)
    y = rng.binomial(1, p * 0.5)
    assert expected_calibration_error(y, p) > 0.1


def test_precision_at_k_reads_the_top_of_each_squad_week() -> None:
    keys = pd.DataFrame({"team_id": [1] * 6, "monday": [pd.Timestamp("2024-01-01")] * 6})
    y = np.array([1, 1, 0, 0, 0, 0])
    p = np.array([0.9, 0.8, 0.7, 0.1, 0.1, 0.1])
    assert precision_at_k(keys, y, p, k=2) == pytest.approx(1.0)
    assert precision_at_k(keys, y, p, k=4) == pytest.approx(0.5)


def test_precision_at_k_skips_squads_smaller_than_k() -> None:
    """A top-5 out of three is not a top-5, and counting it inflates the score."""
    keys = pd.DataFrame({"team_id": [1, 1, 1], "monday": [pd.Timestamp("2024-01-01")] * 3})
    y = np.array([1, 0, 0])
    p = np.array([0.9, 0.5, 0.1])
    assert np.isnan(precision_at_k(keys, y, p, k=5))
