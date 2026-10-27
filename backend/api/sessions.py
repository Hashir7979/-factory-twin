"""Live simulation sessions.

Each WebSocket client owns one ``SimSession``. The simulation is advanced in
small real-time slices (``speed`` simulated seconds per real second) and every
state change produced during a slice is forwarded to the client in one batched
message. The client animates between events, so the stream stays small no
matter how fast the simulation runs.

A session survives a dropped connection for ``session_resume_ttl`` seconds so a
flaky network does not throw away a long-running simulation.
"""

from __future__ import annotations

import bisect
import logging
import secrets
import time
from enum import StrEnum
from typing import Any

from sim import FactoryModel, Scenario, apply_overrides, compute_kpis

log = logging.getLogger("factory.sessions")

DEFAULT_SPEED = 30.0
# Clamp a single slice so a stalled event loop cannot trigger a huge jump.
MAX_SLICE_REAL_SECONDS = 0.25


class SessionStatus(StrEnum):
    PAUSED = "paused"
    PLAYING = "playing"
    FINISHED = "finished"


class SessionLimitError(RuntimeError):
    pass


class SimSession:
    def __init__(self, scenario: Scenario, *, max_speed: float, max_sim_time: float) -> None:
        self.id = secrets.token_urlsafe(16)
        self.base_scenario = scenario
        self.max_speed = max_speed
        self.max_sim_time = max_sim_time
        self.speed = min(DEFAULT_SPEED, max_speed)
        self.status = SessionStatus.PAUSED
        self.overrides: dict[str, float] = {}
        self.seed = scenario.run.seed
        self.seq = 0
        self.attached = False
        self.detached_at: float | None = None
        self._pending: list[dict[str, Any]] = []
        self.scenario = scenario
        self.model = self._build()

    def _build(self) -> FactoryModel:
        self._pending = []
        return FactoryModel(self.scenario, seed=self.seed, listener=self._pending.append)

    # Commands ------------------------------------------------------------------------
    def play(self) -> None:
        if self.status != SessionStatus.FINISHED:
            self.status = SessionStatus.PLAYING

    def pause(self) -> None:
        if self.status == SessionStatus.PLAYING:
            self.status = SessionStatus.PAUSED

    def set_speed(self, value: float) -> None:
        if not 0 < value <= self.max_speed:
            raise ValueError(f"speed must be between 0 and {self.max_speed:g}")
        self.speed = value

    def reset(self, overrides: dict[str, float], seed: int | None) -> None:
        """Rebuild the model. Raises OverrideError (a ValueError) on bad overrides."""
        scenario = apply_overrides(self.base_scenario, overrides) if overrides else self.base_scenario
        self.scenario = scenario
        self.overrides = dict(overrides)
        if seed is not None:
            self.seed = seed
        self.model = self._build()
        self.status = SessionStatus.PAUSED

    def inject_failure(self, machine: str, repair_time: float | None) -> bool:
        if machine not in self.model.machines:
            raise ValueError(f"unknown machine '{machine}'")
        return self.model.inject_failure(machine, repair_time)

    # Simulation ----------------------------------------------------------------------
    def step(self, real_dt: float) -> None:
        if self.status != SessionStatus.PLAYING:
            return
        target = self.model.env.now + self.speed * min(real_dt, MAX_SLICE_REAL_SECONDS)
        if target >= self.max_sim_time:
            target = self.max_sim_time
            self.status = SessionStatus.FINISHED
        self.model.advance(target)

    def drain(self) -> list[dict[str, Any]]:
        events, self._pending[:] = list(self._pending), []
        return events

    # Messages ------------------------------------------------------------------------
    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def status_message(self) -> dict[str, Any]:
        return {"type": "status", "status": self.status.value, "speed": self.speed, "max_speed": self.max_speed}

    def hello_message(self) -> dict[str, Any]:
        """Everything a client needs to (re)build its view from scratch."""
        self.drain()  # the snapshot supersedes any queued incremental events
        return {
            "type": "hello",
            "session": self.id,
            "seq": self.next_seq(),
            "scenario": self.scenario.model_dump(mode="json"),
            "overrides": self.overrides,
            "seed": self.seed,
            "snapshot": self.model.snapshot(),
            **{k: v for k, v in self.status_message().items() if k != "type"},
        }

    def events_message(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "events", "seq": self.next_seq(), "t": self.model.env.now, "events": events}

    def kpi_message(self) -> dict[str, Any]:
        model = self.model
        now = model.env.now
        times = model.monitor.completion_times
        rolling = len(times) - bisect.bisect_left(times, now - 3600) if now > 0 else 0
        return {
            "type": "kpis",
            "seq": self.next_seq(),
            "t": now,
            "warmup": self.scenario.run.warmup,
            "warmed_up": model.warmed_up,
            # Completions during the last simulated hour (or since start if shorter).
            "rolling_throughput": rolling * 3600 / min(3600, now) if now > 0 else 0.0,
            "data": compute_kpis(model).model_dump(mode="json"),
        }


class SessionManager:
    def __init__(self, *, max_sessions: int, resume_ttl: float, max_speed: float, max_sim_time: float) -> None:
        self.max_sessions = max_sessions
        self.resume_ttl = resume_ttl
        self.max_speed = max_speed
        self.max_sim_time = max_sim_time
        self._sessions: dict[str, SimSession] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def create(self, scenario: Scenario) -> SimSession:
        self.expire()
        if len(self._sessions) >= self.max_sessions:
            # Resumable (detached) sessions are best-effort: evict the oldest before
            # refusing a live user.
            detached = [s for s in self._sessions.values() if not s.attached]
            if not detached:
                raise SessionLimitError
            oldest = min(detached, key=lambda s: s.detached_at or 0)
            del self._sessions[oldest.id]
            log.info("session evicted id=%s", oldest.id[:6])
        session = SimSession(scenario, max_speed=self.max_speed, max_sim_time=self.max_sim_time)
        session.attached = True  # attached from birth so expire() never reaps it early
        self._sessions[session.id] = session
        log.info("session created id=%s scenario=%s active=%d", session.id[:6], scenario.id, len(self._sessions))
        return session

    def resume(self, session_id: str, scenario_id: str) -> SimSession | None:
        self.expire()
        session = self._sessions.get(session_id)
        if session is None or session.attached or session.base_scenario.id != scenario_id:
            return None
        return session

    def attach(self, session: SimSession) -> None:
        session.attached = True
        session.detached_at = None

    def detach(self, session: SimSession) -> None:
        session.attached = False
        session.detached_at = time.monotonic()
        session.pause()

    def expire(self) -> None:
        cutoff = time.monotonic() - self.resume_ttl
        stale = [s.id for s in self._sessions.values() if not s.attached and (s.detached_at or 0) < cutoff]
        for sid in stale:
            del self._sessions[sid]
            log.info("session expired id=%s", sid[:6])
