"""Leakage tests for the as-of feature builder.

The two that matter most, per CLAUDE.md: shifting ``as_of`` forward one week changes
every rolling feature, and no feature reads a row dated after the cutoff. Everything
else here supports those. No test reaches the network or opens the corpus.
"""

import numpy as np
import pandas as pd
import pytest

from aiq.features import (
    absence_history,
    add_acwr,
    break_weeks,
    build_features,
    congestion_features,
    rolling_exposure,
    week_start,
    weekly_exposure,
    window_columns,
)


@pytest.fixture
def appearances() -> pd.DataFrame:
    """One player on an irregular schedule: blank weeks, doubles, rising minutes.

    A regular one-match-a-week fixture makes several rolling features constant, which
    would let a broken as-of cutoff pass unnoticed. The irregularity is the point.
    """
    dates = [
        "2024-01-03",
        "2024-01-09",
        "2024-01-11",
        "2024-01-17",
        "2024-01-31",
        "2024-02-03",
        "2024-02-07",
        "2024-02-14",
        "2024-02-17",
        "2024-02-28",
        "2024-03-02",
        "2024-03-06",
        "2024-03-13",
        "2024-03-20",
        "2024-03-23",
    ]
    home = [
        True,
        False,
        True,
        True,
        False,
        False,
        True,
        False,
        True,
        True,
        False,
        True,
        False,
        False,
        True,
    ]
    return pd.DataFrame(
        {
            "player_id": 1,
            "fixture_id": range(1, len(dates) + 1),
            "match_date": pd.to_datetime(dates),
            "team_id": 10,
            "league_id": 8,
            "season_id": 100,
            "minutes": [10 * (i + 1) for i in range(len(dates))],
            "is_home": home,
        }
    )


@pytest.fixture
def fixtures() -> pd.DataFrame:
    dates = pd.date_range("2024-01-03", periods=20, freq="7D")
    return pd.DataFrame(
        {
            "id": range(1, 21),
            "league_id": 8,
            "home_team_id": [10 if i % 2 == 0 else 11 for i in range(20)],
            "away_team_id": [11 if i % 2 == 0 else 10 for i in range(20)],
            "starting_at": dates,
        }
    )


def _panel(mondays: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "player_id": 1,
            "monday": pd.to_datetime(mondays),
            "team_id": 10,
            "league_id": 8,
            "label_injury": 0,
            "label_any": 0,
        }
    )


def test_week_start_returns_mondays() -> None:
    days = pd.Series(pd.to_datetime(["2024-03-07", "2024-03-04", "2024-03-10"]))
    starts = week_start(days)
    assert (starts.dt.weekday == 0).all()
    assert starts.iloc[0] == pd.Timestamp("2024-03-04")
    assert starts.iloc[2] == pd.Timestamp("2024-03-04")


def test_rolling_window_excludes_the_current_week(appearances: pd.DataFrame) -> None:
    """The defining as-of test. Week M's features must not contain week M's minutes.

    Weeks: 01 Jan = 10 (one match), 08 Jan = 20 + 30 (two), 15 Jan = 40 (one).
    """
    rolled = rolling_exposure(weekly_exposure(appearances))
    row = rolled[rolled.week == pd.Timestamp("2024-01-15")].iloc[0]
    assert row.minutes_7d == 50, "7d window must be the previous week only"
    assert row.matches_7d == 2
    assert row.minutes_14d == 60, "14d window must be the two previous weeks"


def test_shifting_as_of_forward_changes_every_rolling_feature(
    appearances: pd.DataFrame,
) -> None:
    """CLAUDE.md's headline leakage test.

    Asserted as "no rolling feature is constant across as_of" rather than "every
    feature differs at one chosen pair". A feature that ignored the cutoff would be
    constant; with small integer counts two adjacent weeks can legitimately tie on a
    single column without anything being wrong.
    """
    rolled = rolling_exposure(weekly_exposure(appearances))
    active = rolled[rolled.history_weeks <= 12]
    for column in window_columns():
        values = active[column].dropna()
        assert values.min() != values.max(), f"{column} does not move with as_of"

    earlier = rolled[rolled.week == pd.Timestamp("2024-03-04")].iloc[0]
    later = rolled[rolled.week == pd.Timestamp("2024-03-11")].iloc[0]
    for column in ("minutes_7d", "minutes_14d", "minutes_28d"):
        assert earlier[column] != later[column], f"{column} did not move one week on"


def test_first_week_has_no_history(appearances: pd.DataFrame) -> None:
    """Nothing precedes the first observed week, so every window must be null."""
    rolled = rolling_exposure(weekly_exposure(appearances))
    first = rolled.sort_values("week").iloc[0]
    for column in window_columns():
        assert pd.isna(first[column]), f"{column} invented history for the first week"
    assert first.history_weeks == 0


def test_join_keys_are_never_shifted(appearances: pd.DataFrame) -> None:
    """Regression: an endswith("d") filter once matched player_id and shifted it."""
    rolled = rolling_exposure(weekly_exposure(appearances))
    assert rolled.player_id.notna().all(), "player_id was shifted along with the windows"
    assert (rolled.player_id == 1).all()


def test_gap_weeks_do_not_widen_the_window() -> None:
    """A missed week must count as zero load, not be skipped over."""
    frame = pd.DataFrame(
        {
            "player_id": 1,
            "fixture_id": [1, 2],
            "match_date": pd.to_datetime(["2024-01-03", "2024-02-07"]),
            "team_id": 10,
            "minutes": [90, 90],
            "is_home": [True, True],
        }
    )
    rolled = rolling_exposure(weekly_exposure(frame))
    row = rolled[rolled.week == pd.Timestamp("2024-02-05")].iloc[0]
    assert row.minutes_28d == 0, "a five-week gap must read as zero load, not 90"


def test_acwr_is_null_when_chronic_load_is_zero() -> None:
    frame = pd.DataFrame({"minutes_7d": [90.0, 90.0], "minutes_28d": [0.0, 360.0]})
    result = add_acwr(frame)
    assert pd.isna(result.acwr_minutes.iloc[0]), "undefined ratio must be NaN, not inf or 0"
    assert result.acwr_minutes.iloc[1] == 1.0


def test_scheduled_next_week_is_allowed_but_trailing_is_shifted(
    fixtures: pd.DataFrame,
) -> None:
    """next_7d reads the current week by design; the trailing counts must not."""
    congestion = congestion_features(fixtures)
    row = congestion[
        (congestion.team_id == 10) & (congestion.week == pd.Timestamp("2024-01-15"))
    ].iloc[0]
    assert row.team_fixtures_next_7d == 1
    assert row.team_fixtures_7d == 1
    first = congestion[congestion.team_id == 10].sort_values("week").iloc[0]
    assert pd.isna(first.team_fixtures_7d), "trailing count must be null with no history"


def test_blank_team_week_is_zero_not_skipped(fixtures: pd.DataFrame) -> None:
    """Regression: a groupby drops empty weeks, so a trailing count reached over them."""
    sparse = fixtures[fixtures.starting_at != pd.Timestamp("2024-02-07")]
    congestion = congestion_features(sparse)
    row = congestion[
        (congestion.team_id == 10) & (congestion.week == pd.Timestamp("2024-02-12"))
    ].iloc[0]
    assert row.team_fixtures_7d == 0, "the blank week must read as zero, not last week's match"


def test_break_weeks_are_derived_not_hardcoded(fixtures: pd.DataFrame) -> None:
    """A week with no fixtures in a league reads as a break, without a FIFA calendar."""
    sparse = fixtures[fixtures.starting_at != pd.Timestamp("2024-02-07")]
    breaks = break_weeks(sparse)
    flagged = set(breaks[breaks.is_break == 1].week)
    assert pd.Timestamp("2024-02-05") in flagged, "a zero-fixture week must read as a break"
    assert breaks.is_break.isin([0, 1]).all()


def test_absence_history_ignores_spells_starting_after_the_monday() -> None:
    """A spell inside the label horizon is the target; it must never be a feature."""
    panel = _panel(["2024-03-04"])
    spells = pd.DataFrame(
        [
            {
                "player_id": 1,
                "start_date": "2024-01-10",
                "assumed_end": "2024-01-24",
                "category": "injury",
            },
            {
                "player_id": 1,
                "start_date": "2024-03-06",
                "assumed_end": "2024-03-20",
                "category": "injury",
            },
        ]
    )
    history = absence_history(spells, panel)
    assert history.absences_365d.iloc[0] == 1, "the future spell leaked into history"
    assert history.injury_absences_365d.iloc[0] == 1


def test_absence_history_drops_spells_older_than_a_year() -> None:
    panel = _panel(["2024-03-04"])
    spells = pd.DataFrame(
        [
            {
                "player_id": 1,
                "start_date": "2022-01-10",
                "assumed_end": "2022-01-24",
                "category": "injury",
            },
        ]
    )
    history = absence_history(spells, panel)
    assert history.absences_365d.iloc[0] == 0


def test_build_features_carries_no_labels(
    appearances: pd.DataFrame, fixtures: pd.DataFrame
) -> None:
    """The feature matrix must not contain either label, by construction."""
    panel = _panel(["2024-02-05", "2024-02-12", "2024-02-19"])
    dim = pd.DataFrame({"player_id": [1], "position_id": [26], "date_of_birth": ["1995-06-01"]})
    features = build_features(panel, appearances, fixtures, pd.DataFrame(), dim)
    assert "label_injury" not in features.columns
    assert "label_any" not in features.columns
    assert len(features) == len(panel), "features must key 1:1 to the panel"


def test_build_features_computes_age_at_the_monday(
    appearances: pd.DataFrame, fixtures: pd.DataFrame
) -> None:
    panel = _panel(["2024-02-05"])
    dim = pd.DataFrame({"player_id": [1], "position_id": [26], "date_of_birth": ["1994-02-05"]})
    features = build_features(panel, appearances, fixtures, pd.DataFrame(), dim)
    assert features.age_years.iloc[0] == pytest.approx(30.0, abs=0.05)


def test_no_feature_reads_a_future_appearance(
    appearances: pd.DataFrame, fixtures: pd.DataFrame
) -> None:
    """Truncating the data after the cutoff must not change any feature value."""
    panel = _panel(["2024-03-04"])
    dim = pd.DataFrame({"player_id": [1], "position_id": [26]})
    cutoff = pd.Timestamp("2024-03-04")

    full = build_features(panel, appearances, fixtures, pd.DataFrame(), dim)
    truncated = build_features(
        panel,
        appearances[appearances.match_date < cutoff],
        fixtures[fixtures.starting_at < cutoff],
        pd.DataFrame(),
        dim,
    )
    # These three read the *published fixture schedule* ahead of the Monday, which a
    # club genuinely has. Truncating the fixture list therefore moves them by design.
    # Everything else must be invariant to data after the cutoff.
    schedule_derived = {"team_fixtures_next_7d", "post_intl_window", "off_season"}
    shared = [c for c in full.columns if c in truncated.columns and c not in schedule_derived]
    for column in shared:
        left, right = full[column].iloc[0], truncated[column].iloc[0]
        if pd.isna(left) and pd.isna(right):
            continue
        assert left == right, f"{column} changed when future rows were removed"


def test_history_weeks_records_observable_history(appearances: pd.DataFrame) -> None:
    """Left censoring is declared, not hidden."""
    rolled = rolling_exposure(weekly_exposure(appearances)).sort_values("week")
    assert rolled.history_weeks.iloc[0] == 0
    assert rolled.history_weeks.iloc[5] == 5
    assert np.diff(rolled.history_weeks.to_numpy()).min() == 1, "history must be contiguous"
