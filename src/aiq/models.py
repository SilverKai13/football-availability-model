"""Baselines, models, and calibration that never sees the test season.

Three dumb baselines come first, because they decide whether there is a project. The
strongest is ``prior_injuries``: injury history is the best-established predictor in
the literature, and a gradient booster that cannot beat *counting a player's injuries
last year* has not earned its place. That comparison is the point of including it, and
D-009 already committed to reporting the answer either way.

**Everything is calibrated, including the baselines.** A raw ``minutes_28d`` value is
not a probability, so Brier and ECE would be meaningless against it. Putting every
scorer through the same isotonic step makes the comparison fair on calibration as well
as on ranking.

**Where the calibration slice comes from** (invariant 5, the subtlest leak here). The
last ``CALIBRATION_FRACTION`` of *weeks inside the training window* is held out, the
model is fitted on the earlier part, and isotonic is fitted on the held-out part. The
split is temporal rather than random, so the calibration data sits between the fitting
data and the test season exactly as it would in deployment. Nothing the test season
touches is ever involved.

No resampling anywhere (locked decision 5). Imbalance is handled by choosing metrics
that survive it, not by editing the data.
"""

from typing import Protocol

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

Floats = NDArray[np.float64]

#: Share of training *weeks* reserved for fitting the calibrator.
CALIBRATION_FRACTION = 0.25

#: Minimum calibration rows behind an isotonic step before its value is trusted.
#: Isotonic is a step function and its topmost step is often supported by a handful of
#: points; with `out_of_bounds="clip"` everything above it inherits that value. On real
#: data that produced 28 rows predicted at exactly 1.000 whose actual rate was 3.57%.
#: The aggregate metrics barely moved — 0.05% of rows — but a squad page showing a
#: player at "100% risk" is not recoverable, so unsupported top steps are capped.
MIN_STEP_SUPPORT = 50

#: Features fed to the models. Keys are excluded; ``suspensions_365d`` is excluded
#: because this label source contains no suspensions at all (D-016), making it a
#: constant-zero column that would only clutter the SHAP plots.
FEATURE_COLUMNS = [
    "minutes_7d",
    "minutes_14d",
    "minutes_28d",
    "matches_7d",
    "matches_14d",
    "matches_28d",
    "away_7d",
    "away_14d",
    "away_28d",
    "short_7d",
    "short_14d",
    "short_28d",
    "acwr_minutes",
    "days_since_last_match",
    "team_fixtures_7d",
    "team_fixtures_14d",
    "team_fixtures_next_7d",
    "post_intl_window",
    "off_season",
    "absences_365d",
    "absence_days_365d",
    "injury_absences_365d",
    "days_since_return",
    "history_weeks",
    "age_years",
    "position_id",
    "league_id",
]


class Scorer(Protocol):
    """Anything that can be fitted and then produce a raw risk score."""

    def fit(self, features: pd.DataFrame, labels: Floats) -> "Scorer": ...

    def score(self, features: pd.DataFrame) -> Floats: ...


class ConstantBaseline:
    """Predicts the training base rate for everyone. The floor.

    Cannot rank at all, so its PR-AUC is the base rate and its precision@5 is chance.
    It exists to anchor the Brier skill score, which is defined against it.
    """

    def __init__(self) -> None:
        self.rate = 0.0

    def fit(self, features: pd.DataFrame, labels: Floats) -> "ConstantBaseline":  # noqa: ARG002
        """Only the label rate matters. Signature set by the protocol."""
        self.rate = float(np.mean(labels))
        return self

    def score(self, features: pd.DataFrame) -> Floats:
        return np.full(len(features), self.rate, dtype=float)


class ColumnBaseline:
    """Ranks by a single feature. No fitting beyond remembering the column.

    ``minutes_28d`` asks whether recent workload alone predicts unavailability.
    ``injury_absences_365d`` asks whether injury history alone does — the one to beat.
    """

    def __init__(self, column: str) -> None:
        self.column = column

    def fit(self, features: pd.DataFrame, labels: Floats) -> "ColumnBaseline":  # noqa: ARG002
        """Nothing to learn — the column *is* the model. Signature set by the protocol."""
        return self

    def score(self, features: pd.DataFrame) -> Floats:
        values = features[self.column].to_numpy(dtype=float)
        return np.nan_to_num(values, nan=float(np.nanmedian(values)))


class SklearnScorer:
    """Adapts an sklearn classifier to the :class:`Scorer` protocol."""

    def __init__(self, estimator: object) -> None:
        self.estimator = estimator

    def fit(self, features: pd.DataFrame, labels: Floats) -> "SklearnScorer":
        self.estimator.fit(features[FEATURE_COLUMNS], labels)  # type: ignore[attr-defined]
        return self

    def score(self, features: pd.DataFrame) -> Floats:
        probabilities = self.estimator.predict_proba(features[FEATURE_COLUMNS])  # type: ignore[attr-defined]
        return np.asarray(probabilities[:, 1], dtype=float)


class CalibratedScorer:
    """A scorer plus an isotonic calibrator fitted on a temporal slice of training.

    The slice is the last ``fraction`` of training weeks. Splitting by week rather
    than by row keeps whole player-weeks together and keeps the calibration data
    strictly later than the fitting data — the same ordering deployment would see.
    """

    def __init__(self, scorer: Scorer, *, fraction: float = CALIBRATION_FRACTION) -> None:
        self.scorer = scorer
        self.fraction = fraction
        self.calibrator: IsotonicRegression | None = None
        #: Highest calibrated value backed by enough calibration rows to believe.
        self.ceiling: float = 1.0

    def fit(self, features: pd.DataFrame, labels: Floats, weeks: pd.Series) -> "CalibratedScorer":
        ordered = np.sort(pd.to_datetime(weeks).unique())
        cut_index = int(len(ordered) * (1.0 - self.fraction))
        cut = ordered[max(cut_index, 1) - 1] if len(ordered) > 1 else ordered[0]
        is_calibration = (pd.to_datetime(weeks) > cut).to_numpy()

        # Degenerate windows (one week, or no positives held out) fall back to fitting
        # on everything: a badly-fitted calibrator is worse than none.
        if is_calibration.sum() < 100 or labels[is_calibration].sum() < 10:
            self.scorer.fit(features, labels)
            self.calibrator = None
            return self

        self.scorer.fit(features[~is_calibration], labels[~is_calibration])
        raw = self.scorer.score(features[is_calibration])
        self.calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        self.calibrator.fit(raw, labels[is_calibration])
        self.ceiling = self._supported_ceiling(raw)
        return self

    def _supported_ceiling(self, calibration_scores: Floats) -> float:
        """Highest isotonic step backed by at least ``MIN_STEP_SUPPORT`` rows."""
        if self.calibrator is None:
            return 1.0
        fitted = pd.Series(self.calibrator.predict(calibration_scores))
        support = fitted.value_counts()
        trusted = support[support >= MIN_STEP_SUPPORT]
        if trusted.empty:
            return float(fitted.max())
        return float(trusted.index.max())

    def predict_raw(self, features: pd.DataFrame) -> Floats:
        return self.scorer.score(features)

    def predict(self, features: pd.DataFrame) -> Floats:
        raw = self.scorer.score(features)
        if self.calibrator is None:
            return raw
        calibrated = np.asarray(self.calibrator.predict(raw), dtype=float)
        return np.minimum(calibrated, self.ceiling)


def build_models() -> dict[str, Scorer]:
    """Every scorer to be reported, baselines first.

    Library defaults throughout: no hyperparameter search (D-006). An honest search
    needs a third nested split and would buy less than the calibration work does.
    """
    logistic = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(max_iter=1000)),
        ]
    )
    return {
        "base_rate": ConstantBaseline(),
        "recent_minutes": ColumnBaseline("minutes_28d"),
        "prior_injuries": ColumnBaseline("injury_absences_365d"),
        "logistic_regression": SklearnScorer(logistic),
        "gradient_boosting": SklearnScorer(HistGradientBoostingClassifier(random_state=0)),
    }
