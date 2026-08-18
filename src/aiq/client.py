"""The one Sportmonks client. All HTTP for this project goes through here.

Deliberately thin. AvailabilityIQ needs exactly one thing from the API — dates of
birth, because the corpus ``players`` table is empty and DOB is what turns a fuzzy
name match into a reliable one. Everything else comes from the exported corpus, so
this module exists for a single Phase 0 errand and is never on the modelling path.

Conventions inherited from the sibling project's client, which was hardened against
this API over several months (see ``ppa-data-engine/src/ppa/client/sportmonks.py``):

- the key travels in the ``Authorization`` header, never in a URL or a log line;
- rate limits are read from the response **root**, per entity bucket, never counted;
- raw-first — responses are written verbatim before anything parses them.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.sportmonks.com/v3/"
ENV_FILE = Path(".env")
RAW = Path("data/raw")

Json = dict[str, Any]


class ApiKeyMissingError(RuntimeError):
    """No API key configured — set SPORTMONKS_API_KEY in .env."""


def load_api_key() -> str:
    """Read the key from the environment, falling back to a ``.env`` file.

    The value is never printed, logged, or written to ``data/raw/``.
    """
    key = os.environ.get("SPORTMONKS_API_KEY")
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "SPORTMONKS_API_KEY":
                key = value.strip().strip("\"'")
                break
    if not key:
        raise ApiKeyMissingError(
            "no Sportmonks API key: put SPORTMONKS_API_KEY=... in .env at the repo root"
        )
    return key


class SportmonksClient:
    """Minimal typed gateway. One request method, one pagination helper."""

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": api_key or load_api_key(), "Accept": "application/json"},
            timeout=timeout,
        )
        #: Latest rate_limit object seen, per entity bucket.
        self.rate_limits: dict[str, Json] = {}

    def get(self, endpoint: str, **params: str | int) -> Json:
        """One GET. Honours a 429 reset hint rather than backing off blindly."""
        for attempt in range(5):
            response = self._http.get(endpoint, params=params)
            body: Json = response.json() if response.text else {}
            limit = body.get("rate_limit")
            if isinstance(limit, dict):
                self.rate_limits[str(limit.get("entity", "unknown"))] = limit
            if response.status_code == 429:
                resets = (limit or {}).get("resets_in_seconds", 60)
                time.sleep(min(float(resets), 120.0))
                continue
            if response.status_code >= 500:
                time.sleep(2.0**attempt)
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"{response.status_code} on {endpoint}: {body.get('message')}")
            return body
        raise RuntimeError(f"giving up on {endpoint} after 5 attempts")

    def close(self) -> None:
        self._http.close()


def write_raw(name: str, payload: object) -> Path:
    """Persist a raw payload under ``data/raw/``. Never contains the API key."""
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / name
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def fetch_squad_players(pairs: list[tuple[int, int]], *, verbose: bool = True) -> list[Json]:
    """Fetch squads for ``(season_id, team_id)`` pairs, returning raw squad rows.

    The squads route is the cheap way to reach dates of birth. Fetching players by id
    would be one call each — 11,175 calls against a 2,000/hour bucket, so nearly six
    hours. Squads cover the same players at one call per team-season: 378 calls for the
    2022+ window, inside a single hour's budget.

    Each row carries a ``player`` sub-object with ``firstname``, ``lastname`` and
    ``date_of_birth``. Those name parts are materially cleaner than the corpus's
    ``player_name``, which is mangled on 0.78% of rows (D-012).
    """
    client = SportmonksClient()
    rows: list[Json] = []
    try:
        for index, (season_id, team_id) in enumerate(pairs, start=1):
            endpoint = f"football/squads/seasons/{season_id}/teams/{team_id}"
            try:
                body = client.get(endpoint, include="player")
            except RuntimeError as error:  # entitlement gaps are expected, not fatal
                if verbose:
                    print(f"  skip {endpoint}: {error}")
                continue
            rows.extend(body.get("data") or [])
            if verbose and index % 50 == 0:
                print(f"  {index}/{len(pairs)} pairs, {len(rows):,} squad rows")
    finally:
        client.close()
    return rows
