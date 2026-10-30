"""The predictions contract and the publish hand-off.

The job and the API agree on one file shape; a file that fails it is never
installed. Publishing must leave either the old or the new predictions in place,
never a half-written file, and the previous version must be one command away.
"""

from pathlib import Path

import pandas as pd
import pytest

from aiq.serve.contract import (
    MANIFEST_FILE,
    PREDICTIONS_FILE,
    Manifest,
    validate_predictions,
)
from aiq.serve.fixtures import synthetic_predictions, write_synthetic_artifacts
from aiq.serve.publish import PublishError, publish, rollback


@pytest.fixture
def source(tmp_path: Path) -> Path:
    directory = tmp_path / "artifacts"
    write_synthetic_artifacts(directory, teams=2, weeks=2, players_per_team=5)
    return directory


class TestContract:
    def test_synthetic_file_passes(self) -> None:
        assert validate_predictions(synthetic_predictions(teams=1, weeks=1)) == []

    def test_missing_columns_are_named(self) -> None:
        frame = synthetic_predictions(teams=1, weeks=1).drop(columns=["risk", "contrib_age_years"])
        problems = validate_predictions(frame)
        assert len(problems) == 1
        assert "risk" in problems[0]
        assert "contrib_age_years" in problems[0]

    def test_risk_must_be_a_probability(self) -> None:
        frame = synthetic_predictions(teams=1, weeks=1)
        frame.loc[0, "risk"] = 1.5
        assert any("probability" in p for p in validate_predictions(frame))

    def test_mondays_must_be_mondays(self) -> None:
        frame = synthetic_predictions(teams=1, weeks=1)
        frame["monday"] = frame.monday + pd.Timedelta(days=1)
        assert any("Monday" in p for p in validate_predictions(frame))

    def test_duplicates_are_refused(self) -> None:
        frame = synthetic_predictions(teams=1, weeks=1)
        frame = pd.concat([frame, frame.iloc[:1]])
        assert any("duplicate" in p for p in validate_predictions(frame))

    def test_empty_is_refused(self) -> None:
        assert validate_predictions(synthetic_predictions(teams=1, weeks=1).iloc[:0]) == ["no rows"]


class TestPublish:
    def test_installs_files_and_writes_a_manifest(self, source: Path, tmp_path: Path) -> None:
        dest = tmp_path / "published"
        manifest = publish(source, dest, version="abc123")
        assert (dest / PREDICTIONS_FILE).exists()
        assert (dest / "metrics.parquet").exists()
        assert not (dest / (PREDICTIONS_FILE + ".tmp")).exists()
        read = Manifest.read(dest / MANIFEST_FILE)
        assert read.version == "abc123"
        assert read.rows == 20
        assert read.sha256 == manifest.sha256
        assert read.weeks_from == "2025-08-04"
        assert read.weeks_to == "2025-08-11"
        assert 0 < read.mean_risk < 0.1

    def test_refuses_a_file_that_fails_the_contract(self, source: Path, tmp_path: Path) -> None:
        frame = pd.read_parquet(source / PREDICTIONS_FILE)
        frame.loc[0, "risk"] = -1.0
        frame.to_parquet(source / PREDICTIONS_FILE, index=False)
        dest = tmp_path / "published"
        with pytest.raises(PublishError, match="probability"):
            publish(source, dest)
        assert not dest.exists() or not (dest / PREDICTIONS_FILE).exists()

    def test_refuses_a_missing_source(self, tmp_path: Path) -> None:
        with pytest.raises(PublishError, match="does not exist"):
            publish(tmp_path / "nowhere", tmp_path / "published")

    def test_previous_version_goes_to_history_and_rollback_restores_it(
        self, source: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "published"
        first = publish(source, dest, version="one")
        # A second, different file.
        frame = synthetic_predictions(teams=1, weeks=1, players_per_team=3, seed=7)
        frame.to_parquet(source / PREDICTIONS_FILE, index=False)
        second = publish(source, dest, version="two")
        assert second.sha256 != first.sha256
        assert Manifest.read(dest / MANIFEST_FILE).version == "two"
        slots = sorted((dest / "history").iterdir())
        assert len(slots) == 1
        assert Manifest.read(slots[0] / MANIFEST_FILE).version == "one"

        restored = rollback(dest)
        assert restored.version == "one"
        assert Manifest.read(dest / MANIFEST_FILE).sha256 == first.sha256
        assert len(pd.read_parquet(dest / PREDICTIONS_FILE)) == first.rows
        assert not list((dest / "history").iterdir())

    def test_history_is_pruned(self, source: Path, tmp_path: Path) -> None:
        dest = tmp_path / "published"
        for n in range(5):
            frame = synthetic_predictions(teams=1, weeks=1, players_per_team=2, seed=n)
            frame.to_parquet(source / PREDICTIONS_FILE, index=False)
            publish(source, dest, version=str(n), keep=2)
        assert len(list((dest / "history").iterdir())) == 2

    def test_rollback_with_no_history_is_refused(self, source: Path, tmp_path: Path) -> None:
        dest = tmp_path / "published"
        publish(source, dest)
        with pytest.raises(PublishError, match="nothing in history"):
            rollback(dest)
