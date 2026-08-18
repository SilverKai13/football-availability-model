"""Player name cleaning and the Transfermarkt crosswalk.

Two jobs. First, player_name in the match data is wrong on about 0.78% of rows
(it shows a teammate's name), so each player_id gets pinned to its most common
name and the rows that disagree get dropped. Second, match Transfermarkt player
IDs onto Sportmonks ones. Names alone are not enough, so date of birth does the
heavy lifting.
"""

import re
import unicodedata

import pandas as pd

# Sportmonks position ids, confirmed against types.developer_name.
POSITIONS = {24: "GK", 25: "DEF", 26: "MID", 27: "ATT"}


def normalise_name(raw: object) -> str:
    """Strip accents, punctuation and case: "John O'Shea" -> "john oshea".

    Nulls come back as "" rather than "nan", otherwise every player without a name
    lands in the same blocking bucket.

    NFKD also deals with the non-breaking space the API puts on some display names.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    decomposed = unicodedata.normalize("NFKD", str(raw))
    ascii_only = decomposed.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", ascii_only.lower()).strip()


def surname_of(raw: object) -> str:
    """Last token of the normalised name, the crosswalk's blocking key."""
    tokens = normalise_name(raw).split()
    return tokens[-1] if tokens else ""


def first_initial_of(raw: object) -> str:
    """First letter of the given name, which is all "L. Cattermole" gives us."""
    tokens = normalise_name(raw).split()
    return tokens[0][0] if len(tokens) > 1 and tokens[0] else ""


def build_players_dim(player_match: pd.DataFrame) -> pd.DataFrame:
    """One row per player_id, carrying its modal name and position.

    Date of birth is joined in separately; it is not in the corpus, whose
    players table is empty.
    """
    counts = player_match.groupby(["player_id", "player_name"]).size().rename("n").reset_index()
    modal = counts.sort_values("n", ascending=False).drop_duplicates("player_id")
    modal = modal.rename(columns={"player_name": "name_raw"})[["player_id", "name_raw"]]

    position = (
        player_match.dropna(subset=["position_id"])
        .groupby("player_id")
        .position_id.agg(lambda s: s.mode().iloc[0] if len(s.mode()) else None)
        .rename("position_id")
        .reset_index()
    )

    dim = modal.merge(position, on="player_id", how="left")
    dim["name_norm"] = dim.name_raw.map(normalise_name)
    dim["surname_norm"] = dim.name_raw.map(surname_of)
    dim["first_initial"] = dim.name_raw.map(first_initial_of)
    return dim


def attach_dob(players_dim: pd.DataFrame, dob: pd.DataFrame) -> pd.DataFrame:
    """Join API dates of birth and the API's name variants onto the dimension.

    Three names per player, none of which is reliably the right one to match on:

    - display_name, the API's common name. Matches the corpus name 79.7% of the
      time, and most of the misses are a trailing non-breaking space.
    - the legal name from firstname/lastname, matches only 51%. Transfermarkt
      generally lists common names, so preferring this would *hurt* the join:
      Koke becomes Jorge Resurrección Merodio.
    - name_raw, the corpus modal name, mangled on 0.78% of rows and
      abbreviated for 12.7% of players (L. Cattermole).

    So none is promoted to "the" name. All three are kept and
    build_aliases fans them out for matching.
    """
    return players_dim.merge(
        dob[["player_id", "firstname", "lastname", "display_name", "date_of_birth"]],
        on="player_id",
        how="left",
    )


def build_aliases(players_dim: pd.DataFrame) -> pd.DataFrame:
    """One row per (player_id, normalised name variant, source).

    Identity resolution against a source that may use any of a player's names is a
    many-to-one problem, so the crosswalk matches against every variant rather than
    betting on one. surname and first_initial ride along as blocking keys.
    """
    legal = (players_dim.firstname.fillna("") + " " + players_dim.lastname.fillna("")).str.strip()
    sources = {
        "display": players_dim.get("display_name"),
        "legal": legal,
        "corpus": players_dim.name_raw,
    }
    frames = []
    for source, values in sources.items():
        if values is None:
            continue
        frame = pd.DataFrame(
            {
                "player_id": players_dim.player_id,
                "alias": values.map(normalise_name),
                "surname": values.map(surname_of),
                "first_initial": values.map(first_initial_of),
                "source": source,
            }
        )
        frames.append(frame[frame.alias.str.len() > 0])
    aliases = pd.concat(frames, ignore_index=True)
    return aliases.drop_duplicates(subset=["player_id", "alias"]).reset_index(drop=True)


# Transfermarkt profile names carry their own id: "Silvio Adzic (1)".
_TM_ID_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def clean_tm_name(raw: object) -> str:
    """Strip the trailing (id) Transfermarkt appends to profile names."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    return _TM_ID_SUFFIX.sub("", str(raw)).strip()


def build_crosswalk(
    aliases: pd.DataFrame, players_dim: pd.DataFrame, profiles: pd.DataFrame
) -> pd.DataFrame:
    """Map Transfermarkt player_id to Sportmonks player_id.

    Date of birth carries the join; names only narrow the candidates. On the Sportmonks
    side (surname, DOB) is ambiguous for 0.17% of pairs, so a DOB agreement plus a
    surname agreement is very close to an identifier.

    Two passes, most specific first. A Transfermarkt player is matched once and never
    revisited, so full_name_dob wins over surname_dob where both fire.

    Returns one row per matched pair with the method that matched it, so the audit can
    report match quality by method rather than as a single undifferentiated rate.
    """
    tm = profiles[["player_id", "player_name", "date_of_birth"]].copy()
    tm = tm.rename(columns={"player_id": "tm_player_id"})
    tm["clean"] = tm.player_name.map(clean_tm_name)
    tm["alias"] = tm.clean.map(normalise_name)
    tm["surname"] = tm.clean.map(surname_of)
    tm["dob"] = pd.to_datetime(tm.date_of_birth, errors="coerce")
    tm = tm[(tm.alias.str.len() > 0) & tm.dob.notna()]

    sm = aliases.merge(players_dim[["player_id", "date_of_birth"]], on="player_id", how="left")
    sm["dob"] = pd.to_datetime(sm.date_of_birth, errors="coerce")
    sm = sm[sm.dob.notna()]

    matches: list[pd.DataFrame] = []
    claimed_tm: set[int] = set()
    claimed_sm: set[int] = set()

    for method, keys in (("full_name_dob", ["alias", "dob"]), ("surname_dob", ["surname", "dob"])):
        left = tm[~tm.tm_player_id.isin(claimed_tm)]
        right = sm[~sm.player_id.isin(claimed_sm)][[*keys, "player_id"]].drop_duplicates()
        # Ambiguity on either side means we cannot tell who is who: drop, never guess.
        right = right[~right.duplicated(subset=keys, keep=False)]
        pair = left.merge(right, on=keys, how="inner")
        pair = pair[~pair.duplicated(subset=["tm_player_id"], keep=False)]
        if pair.empty:
            continue
        matches.append(pair[["tm_player_id", "player_id"]].assign(method=method))
        claimed_tm.update(pair.tm_player_id)
        claimed_sm.update(pair.player_id)

    if not matches:
        return pd.DataFrame(columns=["tm_player_id", "player_id", "method"])
    return pd.concat(matches, ignore_index=True)


def build_appearances(player_match: pd.DataFrame, players_dim: pd.DataFrame) -> pd.DataFrame:
    """The exposure spine: one row per (player, fixture), misnamed rows removed.

    A row whose surname disagrees with the player's canonical surname is dropped
    rather than trusted. That costs ~0.78% of rows and removes every row we can
    show to be corrupt. minutes keeps its nulls, a null minute is
    unknown, not unplayed.
    """
    canon = players_dim[["player_id", "surname_norm"]]
    frame = player_match.merge(canon, on="player_id", how="left")
    frame["row_surname"] = frame.player_name.map(surname_of)
    frame = frame[frame.row_surname == frame.surname_norm]

    frame = frame.assign(match_date=pd.to_datetime(frame.starting_at).dt.date)
    columns = [
        "player_id",
        "fixture_id",
        "team_id",
        "league_id",
        "season_id",
        "match_date",
        "minutes",
        "is_home",
        "position_id",
    ]
    return frame[columns].reset_index(drop=True)
