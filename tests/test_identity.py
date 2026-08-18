"""Tests for name cleaning and the crosswalk."""

import numpy as np
import pandas as pd
import pytest

from aiq.identity import (
    attach_dob,
    build_aliases,
    build_appearances,
    build_players_dim,
    first_initial_of,
    normalise_name,
    surname_of,
)


@pytest.fixture
def player_match() -> pd.DataFrame:
    """One real player plus four misnamed rows, mirroring player_id 2017."""
    rows = [("Luke Steele", 24)] * 8 + [
        ("Marlon Harewood", 27),
        ("Stephen Dawson", 26),
    ]
    return pd.DataFrame(
        {
            "player_id": 2017,
            "player_name": [name for name, _ in rows],
            "position_id": [pos for _, pos in rows],
            "fixture_id": range(1, len(rows) + 1),
            "team_id": 10,
            "league_id": 8,
            "season_id": 100,
            "starting_at": pd.date_range("2024-01-06", periods=len(rows), freq="7D"),
            "minutes": 90,
            "is_home": True,
        }
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("John O'Shea", "john oshea"),
        ("César Azpilicueta", "cesar azpilicueta"),
        ("Koke\xa0", "koke"),
        ("L. Cattermole", "l cattermole"),
        ("  Spaced  Out  ", "spaced  out"),
    ],
)
def test_normalise_name_folds_accents_and_punctuation(raw: str, expected: str) -> None:
    assert normalise_name(raw) == expected


@pytest.mark.parametrize("empty", [None, np.nan, float("nan")])
def test_null_names_never_become_the_string_nan(empty: object) -> None:
    """str(NaN) would give "nan", which puts every unnamed player in one bucket."""
    assert normalise_name(empty) == ""
    assert surname_of(empty) == ""
    assert first_initial_of(empty) == ""


def test_surname_and_initial_handle_the_abbreviated_format() -> None:
    assert surname_of("L. Cattermole") == "cattermole"
    assert first_initial_of("L. Cattermole") == "l"
    assert first_initial_of("Koke") == "", "a single token has no given name"


def test_modal_name_wins_over_misnamed_rows(player_match: pd.DataFrame) -> None:
    """. The mode is what makes canonicalisation safe."""
    dim = build_players_dim(player_match)
    assert len(dim) == 1
    assert dim.name_raw.iloc[0] == "Luke Steele"
    assert dim.position_id.iloc[0] == 24, "modal position must be the keeper's"


def test_misnamed_rows_are_dropped_from_the_spine(player_match: pd.DataFrame) -> None:
    """. Rows disagreeing with the canonical surname leave the exposure spine."""
    dim = build_players_dim(player_match)
    appearances = build_appearances(player_match, dim)
    assert len(appearances) == 8, "the two misnamed rows should be gone"
    assert appearances.minutes.sum() == 720


def test_aliases_cover_every_name_variant() -> None:
    """Neither the common name nor the legal name is reliably the one to match on."""
    dim = pd.DataFrame(
        {
            "player_id": [185571],
            "name_raw": ["Koke"],
            "position_id": [26],
            "firstname": ["Jorge"],
            "lastname": ["Resurrección Merodio"],
            "display_name": ["Koke\xa0"],
            "date_of_birth": ["1992-01-08"],
        }
    )
    aliases = build_aliases(dim)
    assert set(aliases.alias) == {"koke", "jorge resurreccion merodio"}
    assert set(aliases.surname) == {"koke", "merodio"}


def test_aliases_skip_players_with_no_usable_name() -> None:
    dim = pd.DataFrame(
        {
            "player_id": [1],
            "name_raw": [None],
            "position_id": [26],
            "firstname": [None],
            "lastname": [None],
            "display_name": [None],
            "date_of_birth": [None],
        }
    )
    assert build_aliases(dim).empty


def test_attach_dob_keeps_all_three_name_sources() -> None:
    dim = build_players_dim(
        pd.DataFrame(
            {
                "player_id": [1],
                "player_name": ["Dani Parejo"],
                "position_id": [26],
            }
        )
    )
    dob = pd.DataFrame(
        {
            "player_id": [1],
            "firstname": ["Daniel"],
            "lastname": ["Parejo Muñoz"],
            "display_name": ["Dani Parejo"],
            "date_of_birth": ["1989-04-16"],
        }
    )
    merged = attach_dob(dim, dob)
    assert merged.name_raw.iloc[0] == "Dani Parejo"
    assert merged.date_of_birth.iloc[0] == "1989-04-16"
    assert len(build_aliases(merged)) == 2
