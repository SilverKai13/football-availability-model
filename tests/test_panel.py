"""Tests for the player-week panel, mostly around leakage."""

from datetime import date

import pandas as pd
import pytest

from aiq.panel import (
    build_panel,
    median_durations,
    mondays_between,
    with_assumed_end,
)


@pytest.fixture
def appearances() -> pd.DataFrame:
    """One player appearing weekly through 2024, so contention never lapses."""
    dates = pd.date_range("2024-01-06", "2024-06-29", freq="7D")
    return pd.DataFrame(
        {
            "player_id": 1,
            "match_date": dates,
            "team_id": 10,
            "league_id": 8,
            "season_id": 100,
            "minutes": 90,
        }
    )


def _spells(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    return with_assumed_end(frame, median_durations(frame))


def test_mondays_are_mondays() -> None:
    weeks = mondays_between(date(2024, 1, 3), date(2024, 2, 1))
    assert all(m.weekday() == 0 for m in weeks)
    assert weeks[0] == date(2024, 1, 8)


def test_onset_inside_horizon_is_labelled(appearances: pd.DataFrame) -> None:
    """A spell starting 3 days after a Monday labels that Monday."""
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-03-07",
                "end_date": "2024-03-28",
                "category": "injury",
            }
        ]
    )
    panel = build_panel(appearances, spells)
    row = panel[panel.monday == date(2024, 3, 4)]
    assert row.label_injury.iloc[0] == 1


def test_onset_outside_horizon_is_not_labelled(appearances: pd.DataFrame) -> None:
    """A spell starting 8 days out must not label the Monday, the horizon is 7."""
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-03-12",
                "end_date": "2024-03-28",
                "category": "injury",
            }
        ]
    )
    panel = build_panel(appearances, spells)
    row = panel[panel.monday == date(2024, 3, 4)]
    assert row.label_injury.iloc[0] == 0


def test_already_absent_weeks_are_excluded_not_zero_labelled(
    appearances: pd.DataFrame,
) -> None:
    """Invariant 3. The weeks inside a spell must be absent from the panel entirely."""
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-03-05",
                "end_date": "2024-04-10",
                "category": "injury",
            }
        ]
    )
    panel = build_panel(appearances, spells)
    covered = panel[(panel.monday > date(2024, 3, 5)) & (panel.monday < date(2024, 4, 10))]
    assert covered.empty, "an already-absent player-week survived into the panel"


def test_non_injury_absence_removes_the_week_but_never_labels_injury(
    appearances: pd.DataFrame,
) -> None:
    """.

    Uses illness, which is about 10.5% of spells. There are no suspensions in this
    feed at all.
    """
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-03-07",
                "end_date": "2024-03-21",
                "category": "illness",
            }
        ]
    )
    panel = build_panel(appearances, spells)
    assert panel[panel.monday == date(2024, 3, 4)].label_injury.iloc[0] == 0
    assert panel[panel.monday == date(2024, 3, 4)].label_any.iloc[0] == 1
    covered = panel[(panel.monday > date(2024, 3, 7)) & (panel.monday < date(2024, 3, 21))]
    assert covered.empty, "an absent week must leave the panel, not become a negative"


def test_label_any_dominates_label_injury(appearances: pd.DataFrame) -> None:
    """label_any >= label_injury on every row, by construction."""
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-02-08",
                "end_date": "2024-02-20",
                "category": "injury",
            },
            {
                "player_id": 1,
                "start_date": "2024-04-11",
                "end_date": "2024-04-18",
                "category": "illness",
            },
        ]
    )
    panel = build_panel(appearances, spells)
    assert (panel.label_any >= panel.label_injury).all()


def test_unknown_category_removes_week_but_labels_neither(
    appearances: pd.DataFrame,
) -> None:
    """. An uncategorised absence is not evidence that no injury occurred."""
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-03-07",
                "end_date": "2024-03-21",
                "category": "unknown",
            }
        ]
    )
    panel = build_panel(appearances, spells)
    monday = panel[panel.monday == date(2024, 3, 4)]
    assert monday.label_injury.iloc[0] == 0
    assert monday.label_any.iloc[0] == 0
    covered = panel[(panel.monday > date(2024, 3, 7)) & (panel.monday < date(2024, 3, 21))]
    assert covered.empty


def test_right_censored_tail_is_dropped(appearances: pd.DataFrame) -> None:
    """The final horizon cannot be labelled, so it must not appear."""
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-01-15",
                "end_date": "2024-01-20",
                "category": "injury",
            }
        ]
    )
    panel = build_panel(appearances, spells)
    assert panel.monday.max() <= appearances.match_date.max().date()
    latest_labelable = (appearances.match_date.max() - pd.Timedelta(days=7)).date()
    assert panel.monday.max() <= latest_labelable


def test_no_label_reads_beyond_the_horizon(appearances: pd.DataFrame) -> None:
    """Moving an onset from just inside to just outside the horizon flips the label."""
    inside = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-03-11",
                "end_date": "2024-03-20",
                "category": "injury",
            }
        ]
    )
    outside = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-03-12",
                "end_date": "2024-03-20",
                "category": "injury",
            }
        ]
    )
    target = date(2024, 3, 4)
    hit = build_panel(appearances, inside)
    miss = build_panel(appearances, outside)
    assert hit[hit.monday == target].label_injury.iloc[0] == 1
    assert miss[miss.monday == target].label_injury.iloc[0] == 0


def test_player_out_of_contention_leaves_the_panel() -> None:
    """No appearance in the trailing window means not in contention for selection."""
    app = pd.DataFrame(
        {
            "player_id": [1],
            "match_date": [pd.Timestamp("2024-01-06")],
            "team_id": [10],
            "league_id": [8],
            "season_id": [100],
            "minutes": [90],
        }
    )
    spells = _spells(
        [
            {
                "player_id": 1,
                "start_date": "2024-11-01",
                "end_date": "2024-11-10",
                "category": "injury",
            }
        ]
    )
    panel = build_panel(app, spells)
    assert (panel.monday <= date(2024, 3, 4)).all()


def test_null_end_date_uses_assumed_duration_not_next_appearance() -> None:
    """Invariant 4. Re-entry must not be decided by looking at future appearances."""
    spells = pd.DataFrame(
        [
            {
                "player_id": 1,
                "start_date": "2024-01-10",
                "end_date": "2024-01-24",
                "category": "injury",
            },
            {"player_id": 2, "start_date": "2024-03-06", "end_date": None, "category": "injury"},
        ]
    )
    medians = median_durations(spells)
    assert medians["injury"] == 14.0
    filled = with_assumed_end(spells, medians)
    assert filled.assumed_end.iloc[1] == pd.Timestamp("2024-03-20")
