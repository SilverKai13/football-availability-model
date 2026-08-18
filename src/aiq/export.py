"""Pulls the tables we need out of the Sportmonks sqlite database.

Opened read-only, since the database belongs to another project of mine. Run
once; everything downstream works off the parquet files.
"""

import sqlite3
from pathlib import Path

import pandas as pd

CORPUS = Path(
    r"C:\Users\91979\OneDrive\Desktop\PPA Intelligence project\ppa-data-engine\ppa.sqlite"
)
RAW = Path("data/raw")

# Tables copied verbatim. lineups and player_match_stats are deliberately
# excluded: player_match already carries minutes, and they are 11.6M rows.
TABLES = ("player_match", "fixtures", "sidelined", "leagues", "seasons", "types", "teams")


class CorpusMissingError(RuntimeError):
    """The corpus is not where it is expected to be."""


def connect_readonly(path: Path = CORPUS) -> sqlite3.Connection:
    """Open the corpus read-only. Never open it any other way."""
    if not path.exists():
        raise CorpusMissingError(
            f"corpus not found at {path}\n"
            "AvailabilityIQ reads it once, read-only, to build data/raw/. "
            "Check the CORPUS path at the top of this file."
        )
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def export_all(dest: Path = RAW) -> dict[str, int]:
    """Copy every table in TABLES to Parquet. Returns rows written per table."""
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
