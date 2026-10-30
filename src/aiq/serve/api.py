"""The HTTP surface. Thin on purpose: every answer comes from ``PredictionStore``.

Readiness is honest. ``/ready`` answers 503 until a valid predictions file has been
loaded, so a pod with an empty volume takes no traffic, and ``/metrics`` reports
how old the predictions are so an alert can say when the weekly job stopped
delivering.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from aiq.serve.config import Settings
from aiq.serve.store import NotFoundError, NotLoadedError, PredictionStore

log = logging.getLogger("aiq.serve")


class Metrics:
    """Prometheus series, on their own registry so tests can build many apps."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "aiq_http_requests_total",
            "Requests by route and status",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "aiq_http_request_seconds",
            "Request latency by route",
            ["route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
            registry=self.registry,
        )
        self.rows = Gauge(
            "aiq_predictions_rows", "Player-weeks in the loaded predictions", registry=self.registry
        )
        self.age = Gauge(
            "aiq_predictions_age_seconds",
            "Seconds since the loaded predictions were published",
            registry=self.registry,
        )
        self.loaded = Gauge(
            "aiq_predictions_loaded",
            "1 when a valid predictions file is loaded",
            registry=self.registry,
        )
        self.reloads = Counter(
            "aiq_predictions_reloads_total",
            "Reloads of the predictions file by result",
            ["result"],
            registry=self.registry,
        )
        self.info = Gauge(
            "aiq_model_info",
            "Model and version of the loaded predictions (always 1)",
            ["model", "version"],
            registry=self.registry,
        )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "route", "status", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def create_app(settings: Settings | None = None, store: PredictionStore | None = None) -> FastAPI:
    settings = settings or Settings()
    store = store or PredictionStore(settings.artifact_dir, settings.teams_path)
    metrics = Metrics()
    last_check = {"at": 0.0}

    def refresh_gauges() -> None:
        metrics.rows.set(store.rows())
        metrics.loaded.set(1 if store.ready else 0)
        age = store.age_seconds()
        if age is not None:
            metrics.age.set(age)
        if store.ready:
            card = store.model_card(settings.version)
            metrics.info.clear()
            metrics.info.labels(str(card["model"]), str(card["version"])).set(1)

    def check_for_new_predictions() -> None:
        now = time.monotonic()
        if now - last_check["at"] < settings.reload_seconds:
            return
        last_check["at"] = now
        if not store.ready:
            try:
                store.load()
                metrics.reloads.labels("ok").inc()
            except (OSError, ValueError) as exc:
                metrics.reloads.labels("failed").inc()
                log.warning("predictions not loadable yet: %s", exc)
        else:
            failed_before = store.failed_reloads
            if store.maybe_reload():
                metrics.reloads.labels("ok").inc()
            elif store.failed_reloads > failed_before:
                metrics.reloads.labels("failed").inc()
        refresh_gauges()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        try:
            store.load()
            metrics.reloads.labels("ok").inc()
        except (OSError, ValueError) as exc:
            metrics.reloads.labels("failed").inc()
            log.warning("starting without predictions: %s", exc)
        refresh_gauges()
        yield

    app = FastAPI(
        title="AvailabilityIQ",
        version=settings.version,
        description="Seven-day player unavailability risk, from the weekly predictions table.",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.metrics = metrics
    app.state.settings = settings

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        route = request.scope.get("path", "")
        try:
            response = await call_next(request)
        except Exception:
            metrics.requests.labels(route, request.method, "500").inc()
            log.exception("unhandled error", extra={"request_id": request_id, "route": route})
            raise
        duration = time.perf_counter() - started
        template = request.scope.get("route")
        route_name = getattr(template, "path", route)
        if route_name != "/metrics":
            metrics.requests.labels(route_name, request.method, str(response.status_code)).inc()
            metrics.latency.labels(route_name).observe(duration)
        response.headers["x-request-id"] = request_id
        log.info(
            "%s %s %s",
            request.method,
            route,
            response.status_code,
            extra={
                "request_id": request_id,
                "route": route_name,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 1),
            },
        )
        return response

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(NotLoadedError)
    async def not_loaded(_: Request, exc: NotLoadedError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, Any]:
        """Liveness: the process answers."""
        return {"status": "ok", "version": settings.version}

    @app.get("/ready", tags=["ops"])
    def ready(response: Response) -> dict[str, Any]:
        """Readiness: a valid predictions file is loaded."""
        check_for_new_predictions()
        if not store.ready:
            response.status_code = 503
            return {"status": "not ready", "reason": "no valid predictions loaded"}
        age = store.age_seconds()
        return {
            "status": "ready",
            "rows": store.rows(),
            "predictions_age_seconds": None if age is None else round(age),
        }

    @app.get("/metrics", tags=["ops"])
    def prometheus() -> Response:
        check_for_new_predictions()
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/model", tags=["model"])
    def model() -> dict[str, Any]:
        """What is loaded, how it was validated, and what it cannot see."""
        check_for_new_predictions()
        return store.model_card(settings.version)

    @app.get("/v1/teams", tags=["squads"])
    def teams() -> list[dict[str, Any]]:
        check_for_new_predictions()
        return store.teams()

    @app.get("/v1/weeks", tags=["squads"])
    def weeks() -> dict[str, Any]:
        check_for_new_predictions()
        all_weeks = store.weeks()
        return {"weeks": [str(w) for w in all_weeks], "latest": str(all_weeks[-1])}

    @app.get("/v1/squads/{team_id}/weeks", tags=["squads"])
    def squad_weeks(team_id: int) -> dict[str, Any]:
        check_for_new_predictions()
        team_weeks = store.weeks(team_id)
        if not team_weeks:
            raise NotFoundError(f"no squad with team_id {team_id}")
        return {
            "team_id": team_id,
            "team": store.team_name(team_id),
            "weeks": [str(w) for w in team_weeks],
        }

    @app.get("/v1/squads/{team_id}/risk", tags=["squads"])
    def squad_risk(
        team_id: int,
        week: Annotated[
            date | None, Query(description="Monday of the week; latest if omitted")
        ] = None,
        shortlist: Annotated[int | None, Query(ge=1, le=30)] = None,
        top: Annotated[
            int | None, Query(ge=0, le=27, description="contributions per player")
        ] = None,
    ) -> dict[str, Any]:
        """The squad's risk set for a week, ranked, with the base rate and exact contributions."""
        check_for_new_predictions()
        monday = week or store.weeks(team_id)[-1] if store.weeks(team_id) else week
        if monday is None:
            raise NotFoundError(f"no squad with team_id {team_id}")
        if monday.weekday() != 0:
            raise HTTPException(status_code=422, detail=f"{monday} is not a Monday")
        return store.squad(
            team_id,
            monday,
            shortlist=shortlist or settings.shortlist,
            top=settings.max_contributions if top is None else top,
        )

    @app.get("/v1/players/{player_id}/risk", tags=["players"])
    def player_risk(
        player_id: int,
        week: Annotated[
            date | None, Query(description="Monday of the week; latest if omitted")
        ] = None,
        top: Annotated[int | None, Query(ge=0, le=27)] = None,
    ) -> dict[str, Any]:
        check_for_new_predictions()
        if week is not None and week.weekday() != 0:
            raise HTTPException(status_code=422, detail=f"{week} is not a Monday")
        return store.player(player_id, week, top=settings.max_contributions if top is None else top)

    return app
