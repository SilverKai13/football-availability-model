"""The player-week panel: risk set, labels, censoring.

The unit of analysis is one (player, Monday) pair **that is in the risk set**. Weeks
where the player was already absent do not appear as rows at all — a player who is
out cannot become out, and labelling those weeks zero would flood the panel with
guaranteed negatives (architecture.md §5, invariant 3).

Two labels ride on the same rows (D-010): ``label_injury`` is the headline,
``label_any`` is reported beside it. The risk set is identical for both, and excludes
weeks covered by an absence of **any** category — a suspended player is not available
for selection, so they cannot become unavailable.

**The trap this module exists to avoid.** It is tempting to decide "this player is
still absent" by looking at when they next played. That reads the future, and it makes
the training risk set something you could not compute on a Monday morning in
production. Membership here uses trailing information plus an assumed duration for
spells with no recorded end (invariant 4).
"""

from datetime import date, timedelta

import pandas as pd

from aiq import absences

#: Horizon. Locked at seven days (CLAUDE.md locked decision 8).
HORIZON_DAYS = 7

#: A player is "in contention for selection" if they appeared within this window.
#: Trailing-only, so it is computable on the Monday.
CONTENTION_WINDOW_DAYS = 56

#: Categories that count toward ``label_injury``.
INJURY_CATEGORIES = frozenset({"injury"})

#: Categories that count toward ``label_any``. Taken from :mod:`aiq.absences` so the
#: vocabulary has exactly one definition. An earlier hand-written copy here was
#: guessed before the real data arrived — it listed ``suspension`` and
#: ``international``, which this source does not contain, and omitted ``illness`` and
#: ``fatigue``, which are 11.5% of spells. ``label_any`` came out at 1.44% against a
#: ``label_injury`` of 1.42%, which is what gave it away.
#:
#: ``unknown`` is in neither list: an absence we could not categorise is not evidence
#: that no injury occurred (D-011).
LABELLED_CATEGORIES = frozenset(absences.LABELLED)


def mondays_between(start: date, end: date) -> list[date]:
    """Every Monday in ``[start, end]``, inclusive."""
    first = start + timedelta(days=(7 - start.weekday()) % 7)
    out: list[date] = []
    cursor = first
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(days=7)
    return out


def median_durations(absences: pd.DataFrame) -> dict[str, float]:
    """Median observed duration per category, for imputing unrecorded returns.

    Computed from spells that *do* have an end date, then applied to those that do
    not. The assumption is reported with a sensitivity analysis; it affects risk-set
    re-entry only, never a label.
    """
    closed = absences.dropna(subset=["end_date"]).copy()
    closed["duration"] = (
        pd.to_datetime(closed.end_date) - pd.to_datetime(closed.start_date)
    ).dt.days
    grouped = closed.groupby("category").duration.median()
    return {str(k): float(v) for k, v in grouped.items() if pd.notna(v)}


def with_assumed_end(absences: pd.DataFrame, medians: dict[str, float]) -> pd.DataFrame:
    """Add ``assumed_end``: the real end date, or an imputed one where it is missing."""
    frame = absences.copy()
    fallback = float(pd.Series(list(medians.values())).median()) if medians else 30.0
    imputed_days = frame.category.map(medians).fillna(fallback)
    imputed_end = pd.to_datetime(frame.start_date) + pd.to_timedelta(imputed_days, unit="D")
    frame["assumed_end"] = pd.to_datetime(frame.end_date).fillna(imputed_end)
    return frame


def build_panel(
    appearances: pd.DataFrame,
    absences: pd.DataFrame,
    *,
    horizon_days: int = HORIZON_DAYS,
    contention_days: int = CONTENTION_WINDOW_DAYS,
    label_end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build the player-week panel.

    Args:
        appearances: L1 exposure spine — ``player_id``, ``match_date``, ``team_id``,
            ``league_id``, ``season_id``.
        absences: L1 spells — ``player_id``, ``start_date``, ``category``, and
            ``assumed_end`` (add it with :func:`with_assumed_end` first).
        horizon_days: Label window length. Locked at 7.
        contention_days: Trailing appearance window defining selection contention.
        label_end: Last date the label source can be trusted to cover. **Pass this
            whenever the absence feed ends before the exposure data does.** The
            Transfermarkt feed stops on 2025-12-22 while the corpus runs to
            2026-05-24; without this the last five months would silently become
            ~40,000 guaranteed negatives and quietly halve the base rate.

    Returns:
        One row per in-risk-set (player, Monday), with ``label_injury`` and
        ``label_any``. The final ``horizon_days`` of the observation window are
        dropped as right-censored: their outcome is not observable.
    """
    app = appearances.copy()
    app["match_date"] = pd.to_datetime(app.match_date)
    spells = absences.copy()
    spells["start_date"] = pd.to_datetime(spells.start_date)
    spells["assumed_end"] = pd.to_datetime(spells.assumed_end)

    coverage_end = (
        app.match_date.max() if label_end is None else min(app.match_date.max(), label_end)
    )
    observation_end = coverage_end - pd.Timedelta(days=horizon_days)
    weeks = mondays_between(app.match_date.min().date(), observation_end.date())

    rows: list[pd.DataFrame] = []
    for monday in weeks:
        stamp = pd.Timestamp(monday)
        window_open = stamp - pd.Timedelta(days=contention_days)

        # In contention: appeared recently. Trailing only.
        recent = app[(app.match_date < stamp) & (app.match_date >= window_open)]
        if recent.empty:
            continue
        candidates = (
            recent.sort_values("match_date")
            .groupby("player_id")
            .agg(
                team_id=("team_id", "last"),
                league_id=("league_id", "last"),
                season_id=("season_id", "last"),
            )
            .reset_index()
        )

        # Already absent on the Monday -> excluded, not zero-labelled (invariant 3).
        covering = spells[(spells.start_date <= stamp) & (spells.assumed_end >= stamp)]
        candidates = candidates[~candidates.player_id.isin(covering.player_id)]
        if candidates.empty:
            continue

        # Onsets strictly inside (monday, monday + horizon].
        horizon_end = stamp + pd.Timedelta(days=horizon_days)
        onsets = spells[(spells.start_date > stamp) & (spells.start_date <= horizon_end)]
        injured = set(onsets[onsets.category.isin(INJURY_CATEGORIES)].player_id)
        any_absence = set(onsets[onsets.category.isin(LABELLED_CATEGORIES)].player_id)

        candidates["monday"] = monday
        candidates["label_injury"] = candidates.player_id.isin(injured).astype("int8")
        candidates["label_any"] = candidates.player_id.isin(any_absence).astype("int8")
        rows.append(candidates)

    if not rows:
        return pd.DataFrame(
            columns=[
                "player_id",
                "monday",
                "team_id",
                "league_id",
                "season_id",
                "label_injury",
                "label_any",
                "in_risk_set",
            ]
        )

    panel = pd.concat(rows, ignore_index=True)
    panel["in_risk_set"] = True
    return panel
