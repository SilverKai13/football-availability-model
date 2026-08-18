"""Transfermarkt absence spells: categorisation and the L1 ``absences`` table.

The headline label is the onset of an *injury* spell (D-010), and the only thing
distinguishing an injury from an illness, a rest day or an unknown is a free-text
string. So categorisation is not a detail here — it is the label.

Per D-011 the mapping is an explicit reviewed allowlist in
``config/absence_categories.yaml``, and anything it does not recognise becomes
``unknown`` rather than being quietly swept into the majority class. ``unknown``
spells still remove their weeks from the risk set — a player who was absent was
absent — but they count toward neither label.

One thing this source does not contain: **suspensions**. Transfermarkt keeps them in a
separate table, so ``label_any`` here means "any recorded absence in this feed", not
"unavailable for any reason". That belongs in the README, not a footnote.
"""

from pathlib import Path
from typing import Any

import pandas as pd
import yaml

CONFIG = Path("config/absence_categories.yaml")

#: Categories counting toward ``label_any``. ``unknown`` is deliberately absent.
LABELLED = ("injury", "illness", "fatigue", "rest", "personal")


def load_rules(path: Path = CONFIG) -> dict[str, Any]:
    """Read the category allowlist."""
    with path.open(encoding="utf-8") as handle:
        rules: dict[str, Any] = yaml.safe_load(handle)
    return rules


def categorise(reason: object, rules: dict[str, Any]) -> str:
    """Map one free-text reason to a category, or ``unknown``.

    Exact matches win; then pattern groups in the configured order. The ordering is
    load-bearing: ``Muscle fatigue`` must reach ``fatigue`` before the ``muscle``
    keyword claims it for ``injury``.
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
    """Build the L1 absence spells, resolved to Sportmonks ``player_id``.

    Args:
        injuries: Raw Transfermarkt injuries.
        crosswalk: ``tm_player_id`` -> ``player_id``.
        rules: Category allowlist; loaded from config when omitted.

    Returns:
        One row per spell for players we could resolve, with ``category`` and
        ``is_injury``. Spells whose start date will not parse are dropped: a spell
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

    D-011 does not consider Phase 0 passed until the ``unknown`` residual has been
    looked at, so this returns the whole vocabulary rather than a summary.
    """
    rules = rules if rules is not None else load_rules()
    counts = injuries.injury_reason.value_counts(dropna=False).rename("spells").reset_index()
    counts.columns = ["reason", "spells"]
    counts["category"] = counts.reason.map(lambda r: categorise(r, rules))
    return counts.sort_values(["category", "spells"], ascending=[True, False]).reset_index(
        drop=True
    )
