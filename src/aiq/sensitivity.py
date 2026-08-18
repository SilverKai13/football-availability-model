"""Sensitivity analyses for the two assumptions most likely to be wrong.

**1. ``unknown injury``.** 18.9% of spells carry no diagnosis at all. They are mapped to
``injury`` because the string says so and because Transfermarkt uses it when a club did
not disclose (D-011). If the headline result depends on that call, the call needs
defending; if it does not, the result is sturdier than it looked.

**2. Invisible European exposure.** The corpus holds five domestic leagues, so Champions
League and Europa League minutes do not exist in it (risks.md #3). Rather than only
excluding those clubs and re-checking, this module runs the sharper test: if the bias
is real, European clubs should show a **higher injury rate at the same observed
minutes**, because their true load is higher than anything we can see.

European participation is proxied by a **top-six domestic finish in the previous
season**, computed from stored fixture results. It is a proxy: the exact qualification
rules vary by league and season, and cup routes are invisible here.
"""

import pandas as pd

#: Domestic finishing position taken as a proxy for European participation next season.
EUROPEAN_POSITIONS = 6


def league_tables(fixtures: pd.DataFrame) -> pd.DataFrame:
    """League table per (league, season) from stored results."""
    played = fixtures[fixtures.home_score.notna() & fixtures.away_score.notna()]
    home = played.assign(
        team_id=played.home_team_id,
        points=(played.home_score > played.away_score) * 3
        + (played.home_score == played.away_score) * 1,
    )
    away = played.assign(
        team_id=played.away_team_id,
        points=(played.away_score > played.home_score) * 3
        + (played.home_score == played.away_score) * 1,
    )
    columns = ["league_id", "season_id", "team_id", "points"]
    table = (
        pd.concat([home[columns], away[columns]])
        .groupby(["league_id", "season_id", "team_id"], as_index=False)
        .points.sum()
    )
    table["position"] = table.groupby(["league_id", "season_id"]).points.rank(
        ascending=False, method="first"
    )
    return table


def european_team_seasons(fixtures: pd.DataFrame) -> set[tuple[int, int]]:
    """``(team_id, season_id)`` pairs likely playing in Europe *that* season.

    A club plays in Europe in the season *after* the one it qualified in, so the flag
    is carried forward one season rather than applied to the finishing season itself.
    """
    table = league_tables(fixtures)
    qualified = table[table.position <= EUROPEAN_POSITIONS]
    season_order = fixtures.groupby(["league_id", "season_id"]).starting_at.min().reset_index()
    season_order["rank"] = season_order.groupby("league_id").starting_at.rank(method="dense")
    lookup = qualified.merge(season_order, on=["league_id", "season_id"])
    following = season_order.rename(columns={"season_id": "next_season", "rank": "next_rank"})
    joined = lookup.merge(
        following[["league_id", "next_season", "next_rank"]], on="league_id", how="left"
    )
    joined = joined[joined.next_rank == joined["rank"] + 1]
    return set(zip(joined.team_id, joined.next_season, strict=False))


def flag_european(panel: pd.DataFrame, european: set[tuple[int, int]]) -> pd.Series:
    """True where a panel row's club was in Europe that season."""
    pairs = zip(panel.team_id, panel.season_id, strict=False)
    return pd.Series([pair in european for pair in pairs], index=panel.index)


def rate_by_minutes_band(
    panel: pd.DataFrame, features: pd.DataFrame, european: pd.Series, *, bands: int = 5
) -> pd.DataFrame:
    """Injury rate by observed-minutes band, split by European participation.

    **The test for invisible exposure.** Domestic minutes are the only load either
    group is measured on. If European clubs carry extra unseen minutes, their injury
    rate should sit above the domestic-only clubs *within the same observed band* —
    same measured load, more real load, more injuries.
    """
    frame = pd.DataFrame(
        {
            "label": panel.label_injury.to_numpy(),
            "minutes_28d": features.minutes_28d.to_numpy(),
            "european": european.to_numpy(),
        }
    ).dropna(subset=["minutes_28d"])
    frame["band"] = pd.qcut(frame.minutes_28d, bands, labels=False, duplicates="drop")
    grouped = frame.groupby(["band", "european"], as_index=False).agg(
        rate=("label", "mean"), n=("label", "size"), mean_minutes=("minutes_28d", "mean")
    )
    wide = grouped.pivot_table(index="band", columns="european", values="rate")
    wide.columns = ["domestic_only", "european"]
    counts = grouped.pivot_table(index="band", columns="european", values="n")
    counts.columns = ["n_domestic", "n_european"]
    minutes = grouped.pivot_table(index="band", columns="european", values="mean_minutes")
    out = wide.join(counts)
    out["mean_minutes"] = minutes.mean(axis=1)
    out["ratio"] = out.european / out.domestic_only
    return out.reset_index()
