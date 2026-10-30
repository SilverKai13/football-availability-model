"""The API over a synthetic predictions file.

What is checked is what a physio or a dashboard would depend on: the ranking, the
base rate next to every number, exact contributions, honest readiness, a reload
when a new file is published, and the metrics the alerts are built on.
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from aiq.serve.api import create_app
from aiq.serve.config import Settings
from aiq.serve.contract import MANIFEST_FILE, PREDICTIONS_FILE
from aiq.serve.fixtures import synthetic_predictions, write_synthetic_artifacts
from aiq.serve.publish import publish


@pytest.fixture
def published(tmp_path: Path) -> Path:
    source = tmp_path / "artifacts"
    write_synthetic_artifacts(source, teams=3, weeks=4, players_per_team=18)
    dest = tmp_path / "published"
    publish(source, dest, version="test-sha")
    (dest / "teams.parquet").write_bytes((source / "teams.parquet").read_bytes())
    return dest


@pytest.fixture
def client(published: Path) -> Iterator[TestClient]:
    settings = Settings(
        artifact_dir=published, teams_path=published / "teams.parquet", reload_seconds=0.0
    )
    with TestClient(create_app(settings)) as client:
        yield client


class TestReadiness:
    def test_health_is_independent_of_data(self, tmp_path: Path) -> None:
        settings = Settings(artifact_dir=tmp_path / "empty", teams_path=None)
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
            ready = client.get("/ready")
            assert ready.status_code == 503
            assert ready.json()["status"] == "not ready"
            assert client.get("/v1/weeks").status_code == 503

    def test_ready_once_loaded(self, client: TestClient) -> None:
        body = client.get("/ready").json()
        assert body["status"] == "ready"
        assert body["rows"] == 3 * 4 * 18
        assert body["predictions_age_seconds"] >= 0

    def test_becomes_ready_when_a_file_appears(self, tmp_path: Path) -> None:
        directory = tmp_path / "late"
        settings = Settings(artifact_dir=directory, teams_path=None, reload_seconds=0.0)
        with TestClient(create_app(settings)) as client:
            assert client.get("/ready").status_code == 503
            source = tmp_path / "src"
            write_synthetic_artifacts(source, teams=1, weeks=1, players_per_team=3)
            publish(source, directory)
            assert client.get("/ready").status_code == 200


class TestSquads:
    def test_squad_is_ranked_with_the_base_rate(self, client: TestClient) -> None:
        body = client.get("/v1/squads/1000/risk?week=2025-08-11").json()
        assert body["team"] == "Synthetic FC 1"
        assert body["week"] == "2025-08-11"
        risks = [p["risk"] for p in body["players"]]
        assert risks == sorted(risks, reverse=True)
        assert len(risks) == 18
        assert [p["rank"] for p in body["players"]] == list(range(1, 19))
        assert sum(p["shortlisted"] for p in body["players"]) == 5
        first = body["players"][0]
        assert first["vs_base_rate"] == round(first["risk"] / body["base_rate"], 2)
        assert len(first["contributions"]) == 5
        assert first["contributions"][0]["label"]
        assert first["position"] in {"GK", "DEF", "MID", "ATT"}

    def test_latest_week_when_none_given(self, client: TestClient) -> None:
        body = client.get("/v1/squads/1000/risk").json()
        assert body["week"] == "2025-08-25"

    def test_shortlist_and_top_are_adjustable(self, client: TestClient) -> None:
        body = client.get("/v1/squads/1000/risk?shortlist=3&top=0").json()
        assert sum(p["shortlisted"] for p in body["players"]) == 3
        assert body["players"][0]["contributions"] == []

    def test_unknown_team_is_404(self, client: TestClient) -> None:
        assert client.get("/v1/squads/42/risk").status_code == 404

    def test_week_must_be_a_monday(self, client: TestClient) -> None:
        response = client.get("/v1/squads/1000/risk?week=2025-08-12")
        assert response.status_code == 422
        assert "Monday" in response.json()["detail"]

    def test_week_with_no_risk_set_is_404(self, client: TestClient) -> None:
        assert client.get("/v1/squads/1000/risk?week=2020-01-06").status_code == 404

    def test_weeks_and_teams(self, client: TestClient) -> None:
        weeks = client.get("/v1/weeks").json()
        assert weeks["latest"] == "2025-08-25"
        assert len(weeks["weeks"]) == 4
        teams = client.get("/v1/teams").json()
        assert [t["team_id"] for t in teams] == [1000, 1001, 1002]
        squad_weeks = client.get("/v1/squads/1001/weeks").json()
        assert squad_weeks["team"] == "Synthetic FC 2"


class TestPlayers:
    def test_player_latest_and_specific_week(self, client: TestClient) -> None:
        latest = client.get("/v1/players/100003/risk").json()
        assert latest["week"] == "2025-08-25"
        assert latest["team_id"] == 1000
        assert len(latest["weeks_available"]) == 4
        earlier = client.get("/v1/players/100003/risk?week=2025-08-04").json()
        assert earlier["week"] == "2025-08-04"

    def test_unknown_player_is_404(self, client: TestClient) -> None:
        assert client.get("/v1/players/1/risk").status_code == 404


class TestModelCard:
    def test_card_reports_version_and_limits(self, client: TestClient) -> None:
        card = client.get("/v1/model").json()
        assert card["version"] == "test-sha"
        assert card["rows"] == 216
        assert card["teams"] == 3
        assert card["max_risk"] <= 0.075
        assert "training load" in card["what_it_cannot_see"][0]
        assert card["honest_metrics"]["logistic_regression"]["pr_auc"] == 0.038


class TestReloadAndMetrics:
    def test_metrics_expose_rows_age_and_version(self, client: TestClient) -> None:
        text = client.get("/metrics").text
        assert "aiq_predictions_rows 216.0" in text
        assert "aiq_predictions_loaded 1.0" in text
        assert "aiq_predictions_age_seconds" in text
        assert 'aiq_model_info{model="logistic_regression",version="test-sha"} 1.0' in text
        assert 'aiq_http_requests_total{method="GET",route="/metrics"' not in text

    def test_requests_are_counted_by_route_template(self, client: TestClient) -> None:
        client.get("/v1/squads/1000/risk")
        client.get("/v1/squads/42/risk")
        text = client.get("/metrics").text
        assert 'route="/v1/squads/{team_id}/risk",status="200"' in text
        assert 'route="/v1/squads/{team_id}/risk",status="404"' in text
        assert "aiq_http_request_seconds_bucket" in text

    def test_request_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/health", headers={"x-request-id": "abc"})
        assert response.headers["x-request-id"] == "abc"
        assert client.get("/health").headers["x-request-id"]

    def test_new_publish_is_picked_up(
        self, client: TestClient, published: Path, tmp_path: Path
    ) -> None:
        source = tmp_path / "src2"
        source.mkdir()
        synthetic_predictions(teams=1, weeks=1, players_per_team=4, seed=3).to_parquet(
            source / PREDICTIONS_FILE, index=False
        )
        time.sleep(0.01)
        publish(source, published, version="second")
        os.utime(published / MANIFEST_FILE)  # mtime resolution on some filesystems is coarse
        card = client.get("/v1/model").json()
        assert card["version"] == "second"
        assert card["rows"] == 4
        assert 'aiq_predictions_reloads_total{result="ok"} 2.0' in client.get("/metrics").text

    def test_a_bad_publish_does_not_replace_good_data(
        self, client: TestClient, published: Path
    ) -> None:
        bad = pd.read_parquet(published / PREDICTIONS_FILE).iloc[:0]
        bad.to_parquet(published / PREDICTIONS_FILE, index=False)
        time.sleep(0.01)
        (published / MANIFEST_FILE).write_text((published / MANIFEST_FILE).read_text())
        os.utime(published / MANIFEST_FILE)
        assert client.get("/v1/model").json()["rows"] == 216
        client.get("/v1/model")  # a bad file is tried once, not on every request
        assert 'aiq_predictions_reloads_total{result="failed"} 1.0' in client.get("/metrics").text
