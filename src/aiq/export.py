"""Read-only export of the Sportmonks corpus into ``data/raw/``.

The corpus ``ppa.sqlite`` belongs to the sibling project ``ppa-data-engine``. It is
~4 GB, lives inside OneDrive, is single-writer, and its Alembic head moves when that
project branches. All three have already broken commands over there, so this module
opens it **read-only**, copies what AvailabilityIQ needs, and is never run again
(decisions.md D-004).

Raw-first: tables are written verbatim. Nothing is renamed, filtered or cleaned here.
The point of L0 is that it can be checked against the source.
"""

import sqlite3
from pathlib import Path

import pandas as pd

CORPUS = Path(
    r"C:\Users\91979\OneDrive\Desktop\PPA Intelligence project\ppa-data-engine\ppa.sqlite"
)
RAW = Path("data/raw")

#: Tables copied verbatim. `lineups` and `player_match_stats` are deliberately
#: excluded: `player_match` already carries minutes, and they are 11.6M rows.
TABLES = ("player_match", "fixtures", "sidelined", "leagues", "seasons", "types", "teams")


class CorpusMissingError(RuntimeError):
    """The corpus is not where it is expected to be."""


def connect_readonly(path: Path = CORPUS) -> sqlite3.Connection:
    """Open the corpus read-only. Never open it any other way."""
    if not path.exists():
        raise CorpusMissingError(
            f"corpus not found at {path}\n"
            "AvailabilityIQ reads it once, read-only, to build data/raw/. "
            "See docs/memory.md for the expected location."
        )
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def export_all(dest: Path = RAW) -> dict[str, int]:
    """Copy every table in ``TABLES`` to Parquet. Returns rows written per table."""
    dest.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    with connect_readonly() as conn:
        for table in TABLES:
            frame = pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
            frame.to_parquet(dest / f"{table}.parquet", index=False)
            written[table] = len(frame)
    return written


if __name__ == "__main__":
    for name, rows in export_all().items():
        print(f"{name:20s} {rows:>10,}")
