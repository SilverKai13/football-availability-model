"""Single-page squad risk view. Reads precomputed parquet; fits nothing.

The audience is a physio in a Monday meeting, so the design rules are narrow:

- **Never show a probability without the base rate next to it.** 4% means nothing on
  its own; "4%, typical is 2%" means something.
- **Say what the model cannot do**, on screen, not in a footnote. It does not beat
  counting a player's injuries last year on the metric that matters operationally, and
  a page that hid that would be misleading in exactly the way this project is about.
- **Explanations are exact, not approximate.** The served model is logistic regression,
  so each contribution is coefficient x standardised value and they sum to the log-odds.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

PREDICTIONS = Path("artifacts/predictions.parquet")
TEAMS = Path("data/raw/teams.parquet")

#: Shortlist length, matching the precision@5 reported in the README.
SHORTLIST = 5

POSITIONS = {24: "GK", 25: "DEF", 26: "MID", 27: "ATT"}

READABLE = {
    "minutes_7d": "minutes, last 7 days",
    "minutes_14d": "minutes, last 14 days",
    "minutes_28d": "minutes, last 28 days",
    "matches_7d": "matches, last 7 days",
    "matches_14d": "matches, last 14 days",
    "matches_28d": "matches, last 28 days",
    "away_7d": "away trips, last 7 days",
    "away_14d": "away trips, last 14 days",
    "away_28d": "away trips, last 28 days",
    "short_7d": "short turnarounds, last 7 days",
    "short_14d": "short turnarounds, last 14 days",
    "short_28d": "short turnarounds, last 28 days",
    "acwr_minutes": "acute:chronic workload",
    "days_since_last_match": "days since last match",
    "team_fixtures_7d": "team fixtures last week",
    "team_fixtures_14d": "team fixtures, last fortnight",
    "team_fixtures_next_7d": "team fixtures scheduled next week",
    "post_intl_window": "week after an international break",
    "off_season": "off-season week",
    "absences_365d": "absences in the last year",
    "absence_days_365d": "days absent in the last year",
    "injury_absences_365d": "injuries in the last year",
    "days_since_return": "days since last return",
    "history_weeks": "weeks of history we can see",
    "age_years": "age",
    "position_id": "position",
    "league_id": "league",
}


@st.cache_data
def load() -> tuple[pd.DataFrame, dict[int, str]]:
    if not PREDICTIONS.exists():
        st.error("artifacts/predictions.parquet is missing — run `python -m aiq.cli predict`.")
        st.stop()
    frame = pd.read_parquet(PREDICTIONS)
    frame["monday"] = pd.to_datetime(frame.monday)
    names: dict[int, str] = {}
    if TEAMS.exists():
        teams = pd.read_parquet(TEAMS)
        names = dict(zip(teams.id, teams.name, strict=False))
    return frame, names


def main() -> None:
    st.set_page_config(page_title="AvailabilityIQ", layout="wide")
    frame, team_names = load()
    base_rate = float(frame.label_injury.mean())

    st.title("AvailabilityIQ — squad availability risk")
    st.caption(
        "Probability that a player becomes unavailable for selection in the next seven days. "
        "Season 2024/25, scored out-of-sample by a model trained only on earlier seasons."
    )

    st.warning(
        "**Read this before using any number on this page.** On the metric that matches how a "
        "shortlist is actually used, this model does not beat simply counting a player's "
        "injuries over the last year. Its ranking is weak. What it does do well is stay "
        "calibrated: when it says 4%, roughly 4% of those weeks end in an absence. Treat it as "
        "one weak input, not a recommendation — and see the README for what it cannot see, "
        "which includes all training load and every European and international fixture."
    )

    teams = sorted(frame.team_id.unique(), key=lambda t: team_names.get(t, str(t)))
    with st.sidebar:
        st.header("Pick a squad-week")
        team = st.selectbox("Squad", teams, format_func=lambda t: team_names.get(t, f"team {t}"))
        weeks = sorted(frame[frame.team_id == team].monday.unique())
        monday = st.select_slider(
            "Week beginning",
            options=weeks,
            value=weeks[len(weeks) // 2],
            format_func=lambda d: pd.Timestamp(d).strftime("%d %b %Y"),
        )
        st.metric("Base rate this season", f"{base_rate:.2%}")
        st.caption("Every risk below should be read against this number.")

    squad = frame[(frame.team_id == team) & (frame.monday == monday)].copy()
    if squad.empty:
        st.info("No players in the risk set for that squad-week.")
        return
    squad = squad.sort_values("risk", ascending=False)
    squad["ratio"] = squad.risk / base_rate

    left, right = st.columns([3, 2], gap="large")

    with left:
        st.subheader(f"{team_names.get(team, team)} — week of {pd.Timestamp(monday):%d %b %Y}")
        st.caption(
            f"{len(squad)} players in the risk set. Players already unavailable that Monday are "
            "absent from this list by construction — they cannot become unavailable."
        )
        table = pd.DataFrame(
            {
                "Player": squad.player_name.fillna("unknown"),
                "Pos": squad.position_id.map(POSITIONS).fillna("—"),
                "Risk": squad.risk.map(lambda v: f"{v:.2%}"),
                "vs base": squad.ratio.map(lambda v: f"{v:.1f}x"),
                "Became unavailable": squad.label_injury.map({1: "yes", 0: ""}),
            }
        )
        st.dataframe(table, use_container_width=True, hide_index=True)

        flagged = squad.head(SHORTLIST)
        caught = int(flagged.label_injury.sum())
        st.caption(
            f"Top {SHORTLIST} flagged this week: **{caught}** actually became unavailable. "
            f"Across the season this averages 0.15 per squad-week against 0.10 by chance — "
            "about two extra per club per season."
        )

    with right:
        st.subheader("Why this player?")
        chosen = st.selectbox(
            "Player",
            squad.player_id,
            format_func=lambda p: str(squad.set_index("player_id").player_name.get(p, p)),
        )
        row = squad[squad.player_id == chosen].iloc[0]
        st.metric(
            "Risk this week",
            f"{row.risk:.2%}",
            delta=f"{row.ratio:.1f}x the base rate of {base_rate:.2%}",
        )

        contributions = {
            column.removeprefix("contrib_"): row[column]
            for column in squad.columns
            if column.startswith("contrib_")
        }
        series = pd.Series(contributions).sort_values(key=abs, ascending=False).head(8)
        st.caption(
            "Exact contributions to the log-odds — coefficient x standardised value. "
            "Bars right of zero push risk up, left of zero push it down."
        )
        st.bar_chart(
            pd.DataFrame(
                {"contribution": series.to_numpy()},
                index=[READABLE.get(name, name) for name in series.index],
            ),
            horizontal=True,
        )
        st.caption(
            "These explain the model, not the player. A driver here is something the model "
            "leaned on, which is not the same as a cause."
        )


if __name__ == "__main__":
    main()
