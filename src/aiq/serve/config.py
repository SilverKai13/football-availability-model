"""Settings for the API, from the environment. Every one has a prefix of ``AIQ_``."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIQ_", extra="ignore")

    #: Directory holding predictions.parquet and manifest.json; the weekly job writes
    #: here and the API only reads. On a cluster this is the shared volume.
    artifact_dir: Path = Path("artifacts")
    #: Optional team names (Sportmonks teams table). Without it, ids are served as ids.
    teams_path: Path | None = Path("data/raw/teams.parquet")
    #: Seconds between checks of the manifest's modification time.
    reload_seconds: float = 30.0
    #: Players flagged per squad-week; matches the precision@5 the README reports.
    shortlist: int = 5
    #: Contributions returned per player, largest absolute first.
    max_contributions: int = 5
    #: Version string reported by /v1/model when the manifest has none. The image
    #: build sets it to the git sha.
    version: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
