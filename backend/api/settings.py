"""Runtime configuration from environment variables (12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    # Comma-separated list. The Vite dev server is allowed by default for local work.
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
            if o.strip()
        ]
    )
    max_sessions: int = field(default_factory=lambda: _int("MAX_SESSIONS", 20))
    max_speed: float = field(default_factory=lambda: _float("MAX_SPEED", 1000.0))
    # Live runs stop after this much simulated time so memory stays bounded.
    max_live_sim_time: float = field(default_factory=lambda: _float("MAX_LIVE_SIM_TIME", 7 * 24 * 3600))
    # A disconnected session can be resumed for this many seconds.
    session_resume_ttl: float = field(default_factory=lambda: _float("SESSION_RESUME_TTL", 60.0))
    tick_seconds: float = field(default_factory=lambda: _float("TICK_SECONDS", 0.05))
    kpi_interval_seconds: float = field(default_factory=lambda: _float("KPI_INTERVAL_SECONDS", 1.0))
    max_replications: int = field(default_factory=lambda: _int("MAX_REPLICATIONS", 50))
    max_batch_horizon: float = field(default_factory=lambda: _float("MAX_BATCH_HORIZON", 30 * 24 * 3600))
    max_optimizer_trials: int = field(default_factory=lambda: _int("MAX_OPTIMIZER_TRIALS", 60))
    max_jobs: int = field(default_factory=lambda: _int("MAX_JOBS", 4))
    workers: int = field(default_factory=lambda: _int("SIM_WORKERS", max(1, (os.cpu_count() or 2) - 1)))
    # Built frontend to serve from the same origin (single-container deploys).
    static_dir: Path | None = field(default_factory=lambda: Path(p) if (p := os.environ.get("STATIC_DIR")) else None)
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))


settings = Settings()
