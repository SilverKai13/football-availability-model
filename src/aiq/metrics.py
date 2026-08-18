"""Metrics that survive a 2% base rate.

ROC-AUC is not reported on its own, at this base rate it is flattered by the
majority class. PR-AUC and Brier instead, plus calibration error, because a
probability you can trust is more use here than a slightly better ranking.

precision@5 is the practical one: of the 5 players flagged highest for a squad
in a given week, how many actually became unavailable.
"""

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.metrics import average_precision_score, roc_auc_score

# Metric inputs are plain float arrays; the alias keeps signatures readable.
Floats = NDArray[np.float64] | NDArray[np.int64]

# Shortlist length for precision@k. A physio scans a handful of names, not a threshold.
DEFAULT_K = 5


def pr_auc(y_true: Floats, y_prob: Floats) -> float:
    """Area under the precision-recall curve. Its floor is the base rate, not 0.5."""
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(average_precision_score(y_true, y_prob))


def roc_auc(y_true: Floats, y_prob: Floats) -> float:
    """Reported only beside the base rate. See the module docstring."""
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(roc_auc_score(y_true, y_prob))


def brier(y_true: Floats, y_prob: Floats) -> float:
    """Mean squared error of the predicted probabilities. Lower is better."""
    return float(np.mean((y_prob - y_true) ** 2))


def brier_skill_score(y_true: Floats, y_prob: Floats) -> float:
    """Brier against a constant base-rate prediction. Positive means better than nothing.

    At a 2% base rate this is a low bar. Clearing it is a prerequisite, not a result.
    """
    baseline = np.full_like(y_prob, float(np.mean(y_true)), dtype=float)
    reference = brier(y_true, baseline)
    if reference == 0:
        return float("nan")
    return float(1.0 - brier(y_true, y_prob) / reference)


def expected_calibration_error(y_true: Floats, y_prob: Floats, *, bins: int = 10) -> float:
    """Mean |predicted - observed| across probability bins, weighted by bin size.

    Uses quantile bins rather than equal-width ones. At a 2% base rate almost every
    prediction lands in the lowest equal-width bin, and the resulting number describes
    that bin rather than the model.
    """
    frame = pd.DataFrame({"y": y_true, "p": y_prob})
    edges = np.unique(np.quantile(frame.p, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        return float(abs(frame.p.mean() - frame.y.mean()))
    frame["bin"] = pd.cut(frame.p, bins=edges, include_lowest=True, duplicates="drop")
    grouped = frame.groupby("bin", observed=True).agg(
        predicted=("p", "mean"), observed=("y", "mean"), n=("y", "size")
    )
    weights = grouped.n / grouped.n.sum()
    return float((weights * (grouped.predicted - grouped.observed).abs()).sum())


def reliability_table(y_true: Floats, y_prob: Floats, *, bins: int = 10) -> pd.DataFrame:
    """Binned predicted-vs-observed, the data behind the reliability diagram."""
    frame = pd.DataFrame({"y": y_true, "p": y_prob})
    edges = np.unique(np.quantile(frame.p, np.linspace(0, 1, bins + 1)))
    frame["bin"] = pd.cut(frame.p, bins=edges, include_lowest=True, duplicates="drop")
    return (
        frame.groupby("bin", observed=True)
        .agg(predicted=("p", "mean"), observed=("y", "mean"), n=("y", "size"))
        .reset_index(drop=True)
    )


def precision_at_k(
    keys: pd.DataFrame, y_true: Floats, y_prob: Floats, *, k: int = DEFAULT_K
) -> float:
    """Mean precision in the top k of each squad-week.

    Args:
        keys: Must carry team_id and monday, the squad-week grouping.
        y_true: Realised labels.
        y_prob: Predicted probabilities.
        k: Shortlist length.

    Squad-weeks with fewer than k players in the risk set are skipped: a top-5 out
    of three is not a top-5, and including them inflates the score.
    """
    frame = keys[["team_id", "monday"]].copy()
    frame["y"] = y_true
    frame["p"] = y_prob
    scores: list[float] = []
    for _, group in frame.groupby(["team_id", "monday"], observed=True):
        if len(group) < k:
            continue
        scores.append(float(group.nlargest(k, "p").y.mean()))
    return float(np.mean(scores)) if scores else float("nan")


def evaluate(
    keys: pd.DataFrame,
    y_true: Floats,
    y_prob: Floats,
    *,
    k: int = DEFAULT_K,
) -> dict[str, float]:
    """Every metric for one model on one fold."""
    return {
        "base_rate": float(np.mean(y_true)),
        "pr_auc": pr_auc(y_true, y_prob),
        "roc_auc": roc_auc(y_true, y_prob),
        "brier": brier(y_true, y_prob),
        "brier_skill": brier_skill_score(y_true, y_prob),
        "ece": expected_calibration_error(y_true, y_prob),
        f"precision_at_{k}": precision_at_k(keys, y_true, y_prob, k=k),
        "n": float(len(y_true)),
        "positives": float(np.sum(y_true)),
    }
