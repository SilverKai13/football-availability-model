"""Tests for baselines, models and calibration.

The one that matters most is the calibration-leak test: isotonic must never see a row
from the test season. That is the subtlest leak available in this project (invariant 5).
No test reaches the network or opens the corpus.
"""

import numpy as np
import pandas as pd
import pytest
from numpy.typing import NDArray

from aiq.models import (
    FEATURE_COLUMNS,
    CalibratedScorer,
    ColumnBaseline,
    ConstantBaseline,
    build_models,
)


@pytest.fixture
def dataset() -> tuple[pd.DataFrame, NDArray[np.float64], pd.Series]:
    """Two seasons of weeks with a genuine signal in ``injury_absences_365d``."""
    rng = np.random.default_rng(0)
    weeks = pd.date_range("2023-08-07", periods=80, freq="7D")
    rows = [{"monday": week, "player": pid} for week in weeks for pid in range(30)]
    frame = pd.DataFrame(rows)
    n = len(frame)
    for column in FEATURE_COLUMNS:
        frame[column] = rng.normal(size=n)
    frame["injury_absences_365d"] = rng.poisson(1.0, n).astype(float)
    frame["minutes_28d"] = rng.uniform(0, 360, n)
    probability = 0.02 + 0.05 * (frame.injury_absences_365d > 1)
    labels = rng.binomial(1, probability).astype(float)
    return frame, labels, frame.monday


def test_constant_baseline_predicts_the_training_base_rate(
    dataset: tuple[pd.DataFrame, NDArray[np.float64], pd.Series],
) -> None:
    features, labels, _ = dataset
    model = ConstantBaseline().fit(features, labels)
    predictions = model.score(features)
    assert np.allclose(predictions, labels.mean())
    assert len(np.unique(predictions)) == 1, "a constant baseline cannot rank"


def test_column_baseline_ranks_by_its_column(
    dataset: tuple[pd.DataFrame, NDArray[np.float64], pd.Series],
) -> None:
    features, labels, _ = dataset
    model = ColumnBaseline("injury_absences_365d").fit(features, labels)
    scores = model.score(features)
    assert np.corrcoef(scores, features.injury_absences_365d)[0, 1] == pytest.approx(1.0)


def test_column_baseline_fills_nulls_rather_than_propagating_them() -> None:
    features = pd.DataFrame({"minutes_28d": [10.0, np.nan, 30.0]})
    scores = ColumnBaseline("minutes_28d").fit(features, np.zeros(3)).score(features)
    assert not np.isnan(scores).any()


def test_calibrator_never_sees_the_calibration_slice_during_fitting(
    dataset: tuple[pd.DataFrame, NDArray[np.float64], pd.Series],
) -> None:
    """Invariant 5, at the level the class controls: fit and calibrate are disjoint."""
    features, labels, weeks = dataset
    seen: list[int] = []

    class Spy:
        def fit(self, frame: pd.DataFrame, y: NDArray[np.float64]) -> "Spy":
            seen.append(len(frame))
            self.rate = float(y.mean())
            return self

        def score(self, frame: pd.DataFrame) -> NDArray[np.float64]:
            return np.full(len(frame), self.rate)

    model = CalibratedScorer(Spy())
    model.fit(features, labels, weeks)
    assert seen, "the scorer was never fitted"
    assert seen[0] < len(features), "the model was fitted on the calibration slice too"


def test_calibration_slice_is_temporal_and_at_the_end(
    dataset: tuple[pd.DataFrame, NDArray[np.float64], pd.Series],
) -> None:
    """A random slice would put calibration data before fitting data. Deployment cannot."""
    features, labels, weeks = dataset
    fitted_weeks: list[pd.Series] = []

    class Spy:
        def fit(self, frame: pd.DataFrame, y: NDArray[np.float64]) -> "Spy":  # noqa: ARG002
            fitted_weeks.append(frame.monday)
            return self

        def score(self, frame: pd.DataFrame) -> NDArray[np.float64]:
            return np.full(len(frame), 0.02)

    CalibratedScorer(Spy(), fraction=0.25).fit(features, labels, weeks)
    used = fitted_weeks[0]
    held_out = set(weeks) - set(used)
    assert held_out, "nothing was held out for calibration"
    assert max(used) < min(held_out), "calibration data must be strictly later"


def test_calibration_falls_back_rather_than_fitting_on_nothing() -> None:
    """A degenerate window must not produce a calibrator fitted on a handful of rows."""
    features = pd.DataFrame({c: np.zeros(20) for c in FEATURE_COLUMNS})
    features["monday"] = pd.Timestamp("2024-01-01")
    model = CalibratedScorer(ConstantBaseline())
    model.fit(features, np.zeros(20), features.monday)
    assert model.calibrator is None
    assert not np.isnan(model.predict(features)).any()


def test_calibrated_output_stays_a_probability(
    dataset: tuple[pd.DataFrame, NDArray[np.float64], pd.Series],
) -> None:
    features, labels, weeks = dataset
    model = CalibratedScorer(ColumnBaseline("injury_absences_365d"))
    model.fit(features, labels, weeks)
    predictions = model.predict(features)
    assert predictions.min() >= 0.0
    assert predictions.max() <= 1.0


def test_calibration_moves_raw_scores_onto_the_probability_scale(
    dataset: tuple[pd.DataFrame, NDArray[np.float64], pd.Series],
) -> None:
    """A raw count is not a probability; that is why baselines get calibrated too."""
    features, labels, weeks = dataset
    model = CalibratedScorer(ColumnBaseline("injury_absences_365d"))
    model.fit(features, labels, weeks)
    raw = model.predict_raw(features)
    calibrated = model.predict(features)
    assert raw.max() > 1.0, "the raw score is a count, not a probability"
    assert calibrated.max() <= 1.0
    assert abs(calibrated.mean() - labels.mean()) < 0.05


def test_every_model_fits_and_predicts(
    dataset: tuple[pd.DataFrame, NDArray[np.float64], pd.Series],
) -> None:
    features, labels, weeks = dataset
    for name, scorer in build_models().items():
        model = CalibratedScorer(scorer)
        model.fit(features, labels, weeks)
        predictions = model.predict(features)
        assert len(predictions) == len(features), name
        assert not np.isnan(predictions).any(), name


def test_feature_columns_exclude_keys_and_labels() -> None:
    """The feature list is explicit, so a key can never drift into the model."""
    forbidden = {"player_id", "monday", "team_id", "label_injury", "label_any", "in_risk_set"}
    assert not (set(FEATURE_COLUMNS) & forbidden)
    assert "suspensions_365d" not in FEATURE_COLUMNS, "constant-zero column (D-016)"


def test_unsupported_isotonic_steps_are_capped() -> None:
    """Regression: isotonic's top step predicted 1.000 for 28 real rows.

    A step backed by a couple of calibration points is not evidence of certainty, and
    `out_of_bounds="clip"` hands its value to everything above it. The metrics hardly
    noticed; a squad page showing "100% risk" would not have survived contact with a
    physio.
    """
    rng = np.random.default_rng(3)
    weeks = pd.date_range("2023-08-07", periods=60, freq="7D")
    rows = [{"monday": week, "player": pid} for week in weeks for pid in range(40)]
    frame = pd.DataFrame(rows)
    n = len(frame)
    for column in FEATURE_COLUMNS:
        frame[column] = rng.normal(size=n)
    # A handful of extreme rows, all positive — exactly what creates a lone top step.
    frame["injury_absences_365d"] = rng.poisson(0.5, n).astype(float)
    labels = rng.binomial(1, 0.02, n).astype(float)
    extreme = frame.index[:6]
    frame.loc[extreme, "injury_absences_365d"] = 99.0
    labels[extreme] = 1.0

    model = CalibratedScorer(ColumnBaseline("injury_absences_365d"))
    model.fit(frame, labels, frame.monday)
    predictions = model.predict(frame)
    assert predictions.max() <= model.ceiling
    assert predictions.max() < 1.0, "an unsupported top step must not reach certainty"
