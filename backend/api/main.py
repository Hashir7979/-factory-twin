"""FastAPI application factory.

Run locally:  uvicorn api.main:app --reload   (from backend/)
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .jobs import JobManager
from .routes import router as api_router
from .sessions import SessionManager
from .settings import Settings
from .settings import settings as default_settings
from .ws import router as ws_router

log = logging.getLogger("factory.http")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.sessions = SessionManager(
            max_sessions=settings.max_sessions,
            resume_ttl=settings.session_resume_ttl,
            max_speed=settings.max_speed,
            max_sim_time=settings.max_live_sim_time,
        )
        app.state.jobs = JobManager(settings.workers, settings.max_jobs)
        yield
        await app.state.jobs.shutdown()

    app = FastAPI(title="Factory Digital Twin API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Accept a well-formed upstream id (e.g. from a reverse proxy), otherwise mint one.
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if 8 <= len(incoming) <= 64 and incoming.isalnum() else secrets.token_hex(8)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error request_id=%s %s %s", request_id, request.method, request.url.path)
            response = JSONResponse({"detail": "Internal server error.", "request_id": request_id}, status_code=500)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api"):
            log.info(
                "%s %s %d %.1fms request_id=%s", request.method, request.url.path, response.status_code,
                (time.perf_counter() - started) * 1000, request_id,
            )  # fmt: skip
        return response

    app.include_router(api_router)
    app.include_router(ws_router)

    if settings.static_dir and settings.static_dir.is_dir():
        # Same-origin hosting of the built frontend (single-container deploy).
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")
    return app


app = create_app()
