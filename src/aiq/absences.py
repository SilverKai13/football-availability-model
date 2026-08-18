"""Transfermarkt absence spells, and working out which ones are injuries.

The label is "an injury spell started", and the only thing telling us whether a
spell counts is a free text string, so the mapping lives in
config/absence_categories.yaml rather than being guessed inline. Anything the
mapping does not recognise becomes "unknown" and counts as neither label.

Worth knowing: there are no suspensions in this feed, Transfermarkt keeps those
in a separate table.
"""

from pathlib import Path
from typing import Any

import pandas as pd
import yaml

CONFIG = Path("config/absence_categories.yaml")

# Categories counting toward label_any. unknown is deliberately absent.
LABELLED = ("injury", "illness", "fatigue", "rest", "personal")


def load_rules(path: Path = CONFIG) -> dict[str, Any]:
    """Read the category allowlist."""
    with path.open(encoding="utf-8") as handle:
        rules: dict[str, Any] = yaml.safe_load(handle)
    return rules


def categorise(reason: object, rules: dict[str, Any]) -> str:
    """Map one free-text reason to a category, or unknown.

    Exact matches win; then pattern groups in the configured order. The ordering is
    load-bearing: Muscle fatigue must reach fatigue before the muscle
    keyword claims it for injury.
    """
    if reason is None or (isinstance(reason, float) and pd.isna(reason)):
        return "unknown"
    text = str(reason).strip().lower()
    if not text:
        return "unknown"

    exact: dict[str, str] = rules.get("exact") or {}
    if text in exact:
        return str(exact[text])

    patterns: dict[str, list[str]] = rules.get("patterns") or {}
    for category in rules.get("order") or list(patterns):
        for keyword in patterns.get(category) or []:
            if keyword in text:
                return str(category)
    return "unknown"


def build_absences(
    injuries: pd.DataFrame, crosswalk: pd.DataFrame, rules: dict[str, Any] | None = None
) -> pd.DataFrame:
    """Build the L1 absence spells, resolved to Sportmonks player_id.

    Args:
        injuries: Raw Transfermarkt injuries.
        crosswalk: tm_player_id -> player_id.
        rules: Category allowlist; loaded from config when omitted.

    Returns:
        One row per spell for players we could resolve, with category and
        is_injury. Spells whose start date will not parse are dropped: a spell
        with no onset cannot produce a label.
    """
    rules = rules if rules is not None else load_rules()
    frame = injuries.rename(
        columns={"player_id": "tm_player_id", "from_date": "start_date", "injury_reason": "reason"}
    ).copy()
    frame["start_date"] = pd.to_datetime(frame.start_date, errors="coerce")
    frame["end_date"] = pd.to_datetime(frame.end_date, errors="coerce")
    frame = frame[frame.start_date.notna()]

    lookup = dict(zip(crosswalk.tm_player_id, crosswalk.player_id, strict=False))
    frame["player_id"] = frame.tm_player_id.map(lookup)
    frame = frame[frame.player_id.notna()]

    categories = {reason: categorise(reason, rules) for reason in frame.reason.unique()}
    frame["category"] = frame.reason.map(categories)
    frame["is_injury"] = frame.category == "injury"
    frame["spell_id"] = range(len(frame))
    columns = [
        "spell_id",
        "tm_player_id",
        "player_id",
        "start_date",
        "end_date",
        "reason",
        "category",
        "is_injury",
    ]
    return frame[columns].reset_index(drop=True)


def audit_categories(injuries: pd.DataFrame, rules: dict[str, Any] | None = None) -> pd.DataFrame:
    """Per distinct reason: its category and spell count, for review.
    looked at, so this returns the whole vocabulary rather than a summary.
    """
    rules = rules if rules is not None else load_rules()
    counts = injuries.injury_reason.value_counts(dropna=False).rename("spells").reset_index()
    counts.columns = ["reason", "spells"]
    counts["category"] = counts.reason.map(lambda r: categorise(r, rules))
    return counts.sort_values(["category", "spells"], ascending=[True, False]).reset_index(
        drop=True
    )
