"""As-of feature construction. Every column is computed from strictly-earlier data.

This is the invariant the whole project exists to demonstrate (CLAUDE.md invariant 2),
so the module is arranged to make violating it awkward rather than merely forbidden.

**Why weekly buckets.** Every window here is a multiple of seven days and every panel
row is anchored on a Monday. Bucket an appearance by the Monday that opens its week
and the trailing windows become exact, not approximate: the 7-day window before Monday
``M`` is precisely the bucket ``M - 7d``, because that week runs ``[M-7d, M)``. The
as-of cutoff then falls out of a ``shift(1)`` on a contiguous weekly grid rather than
depending on a date comparison someone might get wrong later.

**What is allowed to look forward.** A fixture's *scheduled existence*. A club knows
next week's fixture list on Monday morning, so ``team_fixtures_next_7d`` is legitimate.
Realised outcomes of those fixtures are not, and nothing here reads one.

**Left censoring, declared.** Exposure history starts when the corpus does (2012) or
when a player first appears in one of the five leagues. A player arriving from Serie A
carries real prior load that reads here as zero. ``history_weeks`` records how much
observable history each row actually has, so the model can learn to distrust thin
histories rather than silently treating them as genuine rest.
"""

import numpy as np
import pandas as pd

#: Trailing windows, in weeks. All exact under Monday anchoring.
WINDOWS_WEEKS = (1, 2, 4)

#: A turnaround shorter than this counts as congested.
SHORT_TURNAROUND_DAYS = 4

#: Weeks a player stays in the risk set after their last appearance (56 days, panel.py).
#: The exposure grid must reach this far past a player's final match to feed those rows.
CONTENTION_WEEKS = 8

#: A league-week with at most this share of its typical fixture count is a break
#: candidate. Derived from the schedule, never from an invented calendar.
BREAK_WEEK_FRACTION = 0.2

#: Weeks either side of a quiet week that must contain fixtures for it to count as a
#: mid-season break rather than the off-season.
SEASON_GAP_WEEKS = 3

#: Columns the panel carries that must never reach the feature matrix.
LABEL_COLUMNS = ("label_injury", "label_any")


def window_columns() -> list[str]:
    """The rolling window column names, listed explicitly.

    Deliberately not inferred from a name pattern. An earlier version selected these
    with ``endswith("d")``, which also matched ``player_id``, ``team_id``,
    ``league_id`` and ``season_id`` — and duly shifted the join keys by one row.
    """
    return [
        f"{source}_{weeks * 7}d"
        for weeks in WINDOWS_WEEKS
        for source in ("minutes", "matches", "away", "short")
    ]


def week_start(dates: pd.Series) -> pd.Series:
    """The Monday opening each date's week, at midnight. The key for every window.

    ``normalize()`` is load-bearing, not tidiness. Fixture kickoff times carry a clock
    component, and without flooring it a 22:00 Saturday fixture keys to "Monday 22:00"
    while a 15:00 Sunday fixture in the same week keys to "Monday 15:00" — two
    different weeks, and neither matches a panel Monday at midnight. That silently
    nulled every congestion feature the first time this ran on real data.
    """
    stamps = pd.to_datetime(dates)
    return (stamps - pd.to_timedelta(stamps.dt.weekday, unit="D")).dt.normalize()


def weekly_exposure(appearances: pd.DataFrame) -> pd.DataFrame:
    """Collapse appearances to one row per (player, week) with raw exposure counts.

    ``min_count=1`` on minutes keeps D-007: a week in which every appearance has a null
    minute stays null rather than summing to a confident zero. Unknown is not unplayed.
    """
    frame = appearances.copy()
    frame["match_date"] = pd.to_datetime(frame.match_date)
    frame = frame.sort_values(["player_id", "match_date"])
    frame["week"] = week_start(frame.match_date)
    frame["is_away"] = (~frame.is_home.astype(bool)).astype("int16")
    gap = frame.groupby("player_id").match_date.diff().dt.days
    frame["short"] = (gap < SHORT_TURNAROUND_DAYS).astype("int16")
    return (
        frame.groupby(["player_id", "week"])
        .agg(
            minutes=("minutes", lambda s: s.sum(min_count=1)),
            matches=("fixture_id", "size"),
            away=("is_away", "sum"),
            short=("short", "sum"),
        )
        .reset_index()
    )


def _week_grid(frame: pd.DataFrame, key: str, extend_weeks: int = 0) -> pd.DataFrame:
    """A contiguous weekly index per ``key``, optionally extended past the last week.

    Filling gaps is not cosmetic. A rolling window over *observed* rows silently
    widens itself across any missing week: a team that played four weeks ago but not
    since would otherwise report that match as last week's. Every window in this module
    depends on the grid being calendar-contiguous.
    """
    spans = frame.groupby(key).week.agg(["min", "max"])
    spans["max"] = spans["max"] + pd.Timedelta(weeks=extend_weeks)
    all_weeks = pd.date_range(frame.week.min(), spans["max"].max(), freq="7D")
    index = pd.MultiIndex.from_product([spans.index, all_weeks], names=[key, "week"])
    grid = pd.DataFrame(index=index).reset_index().merge(spans, on=key)
    grid = grid[(grid.week >= grid["min"]) & (grid.week <= grid["max"])]
    return grid.drop(columns=["min", "max"])


def _contiguous_grid(weekly: pd.DataFrame) -> pd.DataFrame:
    """Fill each player's week range so rolling windows count calendar weeks.

    Extended forward by ``CONTENTION_WEEKS`` because a player stays in the risk set for
    up to 56 days after their last appearance, and those weeks still need features.
    """
    grid = _week_grid(weekly, "player_id", extend_weeks=CONTENTION_WEEKS)
    merged = grid.merge(weekly, on=["player_id", "week"], how="left")
    for column in ("matches", "away", "short"):
        merged[column] = merged[column].fillna(0)
    # minutes keeps its nulls inside a player's span (D-007); only weeks with no
    # appearance at all are a genuine zero.
    merged.loc[merged.matches == 0, "minutes"] = merged.loc[merged.matches == 0, "minutes"].fillna(
        0
    )
    return merged.sort_values(["player_id", "week"]).reset_index(drop=True)


def rolling_exposure(weekly: pd.DataFrame) -> pd.DataFrame:
    """Trailing sums over 1/2/4 weeks, shifted so week ``M`` sees only weeks before it.

    ``shift(1)`` is what enforces the as-of rule: the value landing on Monday ``M`` is
    the window ending at ``M - 7d``. Nothing on row ``M`` has seen week ``M``.
    """
    grid = _contiguous_grid(weekly)
    grouped = grid.groupby("player_id", sort=False)
    for name in window_columns():
        source, suffix = name.rsplit("_", 1)
        weeks = int(suffix.removesuffix("d")) // 7
        grid[name] = grouped[source].rolling(weeks, min_periods=1).sum().reset_index(0, drop=True)
    windowed = window_columns()
    grid[windowed] = grid.groupby("player_id", sort=False)[windowed].shift(1)
    grid["history_weeks"] = grouped.cumcount()
    return grid.drop(columns=["minutes", "matches", "away", "short"])


def add_acwr(frame: pd.DataFrame) -> pd.DataFrame:
    """Acute:chronic workload ratio on minutes.

    Chronic is the mean weekly load across the 28-day window, so the ratio is
    ``minutes_7d / (minutes_28d / 4)``. A zero chronic load yields NaN, never an
    infinity and never a zero: an undefined ratio is unknown, not low.

    Its standing as a risk ratio is contested (Impellizzeri et al.: mathematical
    coupling, arbitrary discretisation). It is a feature here, not a claim.
    """
    chronic = frame.minutes_28d / 4.0
    frame["acwr_minutes"] = np.where(chronic > 0, frame.minutes_7d / chronic, np.nan)
    return frame


def days_since_last_match(panel: pd.DataFrame, appearances: pd.DataFrame) -> pd.DataFrame:
    """Days from each panel Monday back to the player's most recent match.

    An as-of lookup, not a weekly join. Joining on "the week before the Monday" only
    answers for players who happened to play that week, which nulled the feature on
    47% of real rows — precisely the rested and returning players it exists to describe.

    ``allow_exact_matches=False`` keeps the cutoff strict: a match kicking off *on* the
    Monday is not visible to that Monday.
    """
    left = panel[["player_id", "monday"]].copy()
    left["monday"] = pd.to_datetime(left.monday)
    left = left.sort_values("monday")
    right = appearances[["player_id", "match_date"]].copy()
    right["match_date"] = pd.to_datetime(right.match_date)
    right = right.sort_values("match_date")

    merged = pd.merge_asof(
        left,
        right.rename(columns={"match_date": "last_match"}),
        left_on="monday",
        right_on="last_match",
        by="player_id",
        direction="backward",
        allow_exact_matches=False,
    )
    merged["days_since_last_match"] = (merged.monday - merged.last_match).dt.days
    return merged.drop(columns=["last_match"])


def congestion_features(fixtures: pd.DataFrame) -> pd.DataFrame:
    """Per (team, week) fixture counts, plus the *scheduled* week ahead.

    ``team_fixtures_next_7d`` reads the week starting on the Monday itself. That is not
    leakage: the fixture list is published well in advance and a club plans against it.
    """
    frame = fixtures[["id", "home_team_id", "away_team_id", "starting_at"]].copy()
    long = pd.concat(
        [
            frame.rename(columns={"home_team_id": "team_id"})[["id", "team_id", "starting_at"]],
            frame.rename(columns={"away_team_id": "team_id"})[["id", "team_id", "starting_at"]],
        ]
    )
    long["week"] = week_start(long.starting_at)
    played = long.groupby(["team_id", "week"]).id.size().rename("fixtures").reset_index()
    # Blank weeks must exist as zeros, or a trailing count reaches over them.
    per_week = (
        _week_grid(played, "team_id", extend_weeks=CONTENTION_WEEKS)
        .merge(played, on=["team_id", "week"], how="left")
        .fillna({"fixtures": 0})
        .sort_values(["team_id", "week"])
    )
    grouped = per_week.groupby("team_id", sort=False)
    per_week["rolled_2"] = (
        grouped.fixtures.rolling(2, min_periods=1).sum().reset_index(0, drop=True)
    )
    per_week["team_fixtures_7d"] = grouped.fixtures.shift(1)
    per_week["team_fixtures_14d"] = per_week.groupby("team_id", sort=False).rolled_2.shift(1)
    per_week["team_fixtures_next_7d"] = per_week.fixtures
    return per_week.drop(columns=["fixtures", "rolled_2"])


def break_weeks(fixtures: pd.DataFrame) -> pd.DataFrame:
    """League-weeks that look like international breaks, derived from the schedule.

    No hardcoded FIFA calendar: a week carrying at most ``BREAK_WEEK_FRACTION`` of a
    league's typical fixture count is a candidate. Derived from scheduling, which is
    known on the Monday, so it reads nothing unobservable.

    **A break must be mid-season.** A quiet week only counts if the league has fixtures
    on *both* sides of it within ``SEASON_GAP_WEEKS``. Without that test the summer
    off-season reads as one long international break — it put the flag on 24.5% of real
    rows, against the ~12% a five-window season should give. Worse, it pointed the same
    way for opposite things: a congested international window and a restful July.
    ``off_season`` is returned separately, because preseason is its own risk regime.
    """
    frame = fixtures[["id", "league_id", "starting_at"]].copy()
    frame["week"] = week_start(frame.starting_at)
    played = frame.groupby(["league_id", "week"]).id.size().rename("fixtures").reset_index()
    # A week with *no* fixtures is the strongest break signal there is, and it is
    # exactly the week that would be missing entirely from a groupby.
    per_week = (
        _week_grid(played, "league_id", extend_weeks=CONTENTION_WEEKS)
        .merge(played, on=["league_id", "week"], how="left")
        .fillna({"fixtures": 0})
    )
    typical = played.groupby("league_id").fixtures.median().rename("typical")
    per_week = per_week.merge(typical, on="league_id").sort_values(["league_id", "week"])

    grouped = per_week.groupby("league_id", sort=False).fixtures
    window = SEASON_GAP_WEEKS
    before = grouped.rolling(window, min_periods=1).sum().reset_index(0, drop=True)
    before = per_week.assign(v=before).groupby("league_id", sort=False).v.shift(1)
    after = (
        per_week.iloc[::-1]
        .groupby("league_id", sort=False)
        .fixtures.rolling(window, min_periods=1)
        .sum()
        .reset_index(0, drop=True)
        .iloc[::-1]
    )
    after = per_week.assign(v=after).groupby("league_id", sort=False).v.shift(-1)

    quiet = per_week.fixtures <= per_week.typical * BREAK_WEEK_FRACTION
    in_season = (before.fillna(0) > 0) & (after.fillna(0) > 0)
    per_week["is_break"] = (quiet & in_season).astype("int8")
    per_week["off_season"] = (~in_season).astype("int8")
    return per_week[["league_id", "week", "is_break", "off_season"]]


def absence_history(absences: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """Trailing 365-day absence counts per panel row, from spells that already started.

    Only spells with ``start_date < monday`` are visible. A spell beginning inside the
    label horizon is the thing being predicted and must never appear as a feature.
    """
    keys = panel[["player_id", "monday"]].copy()
    keys["monday"] = pd.to_datetime(keys.monday)
    counts = [
        "absences_365d",
        "absence_days_365d",
        "injury_absences_365d",
        "suspensions_365d",
    ]
    if absences.empty:
        for column in [*counts, "days_since_return"]:
            keys[column] = np.nan
        return keys

    spells = absences.copy()
    spells["start_date"] = pd.to_datetime(spells.start_date)
    spells["assumed_end"] = pd.to_datetime(spells.assumed_end)
    merged = keys.merge(spells, on="player_id", how="left")
    visible = merged[merged.start_date < merged.monday]
    recent = visible[visible.start_date >= visible.monday - pd.Timedelta(days=365)].assign(
        days=lambda f: (f.assumed_end.clip(upper=f.monday) - f.start_date).dt.days,
        is_injury=lambda f: (f.category == "injury").astype("int16"),
        is_susp=lambda f: (f.category == "suspension").astype("int16"),
    )
    agg = (
        recent.groupby(["player_id", "monday"])
        .agg(
            absences_365d=("start_date", "size"),
            absence_days_365d=("days", "sum"),
            injury_absences_365d=("is_injury", "sum"),
            suspensions_365d=("is_susp", "sum"),
        )
        .reset_index()
    )
    returns = (
        visible.groupby(["player_id", "monday"])
        .assumed_end.max()
        .rename("last_return")
        .reset_index()
    )
    out = keys.merge(agg, on=["player_id", "monday"], how="left").merge(
        returns, on=["player_id", "monday"], how="left"
    )
    out["days_since_return"] = (out.monday - out.last_return).dt.days.clip(lower=0)
    out[counts] = out[counts].fillna(0)
    return out.drop(columns=["last_return"])


def build_features(
    panel: pd.DataFrame,
    appearances: pd.DataFrame,
    fixtures: pd.DataFrame,
    absences: pd.DataFrame,
    players_dim: pd.DataFrame,
) -> pd.DataFrame:
    """Assemble the L3 feature matrix, keyed 1:1 to the panel.

    Args:
        panel: L2 player-weeks, already restricted to the risk set.
        appearances: L1 exposure spine.
        fixtures: L0 fixtures, for congestion and break-week derivation.
        absences: L1 spells carrying ``assumed_end``; may be empty.
        players_dim: canonical players, optionally carrying ``date_of_birth``.

    Returns:
        One row per panel row, carrying no labels and no forward-looking columns.
    """
    out = panel[["player_id", "monday", "team_id", "league_id"]].copy()
    out["monday"] = pd.to_datetime(out.monday)

    exposure = add_acwr(rolling_exposure(weekly_exposure(appearances)))
    out = out.merge(
        exposure.rename(columns={"week": "monday"}), on=["player_id", "monday"], how="left"
    )

    out = out.merge(
        days_since_last_match(panel, appearances), on=["player_id", "monday"], how="left"
    )

    out = out.merge(
        congestion_features(fixtures).rename(columns={"week": "monday"}),
        on=["team_id", "monday"],
        how="left",
    )

    breaks = break_weeks(fixtures)
    breaks["monday"] = breaks.week + pd.Timedelta(days=7)
    out = out.merge(
        breaks[["league_id", "monday", "is_break", "off_season"]].rename(
            columns={"is_break": "post_intl_window"}
        ),
        on=["league_id", "monday"],
        how="left",
    )

    out = out.merge(absence_history(absences, panel), on=["player_id", "monday"], how="left")

    dim = players_dim[["player_id", "position_id"]].copy()
    if "date_of_birth" in players_dim.columns:
        dim["date_of_birth"] = pd.to_datetime(players_dim.date_of_birth, errors="coerce")
    out = out.merge(dim, on="player_id", how="left")
    if "date_of_birth" in out.columns:
        out["age_years"] = (out.monday - out.date_of_birth).dt.days / 365.25
        out = out.drop(columns=["date_of_birth"])
    else:
        out["age_years"] = np.nan

    return out
