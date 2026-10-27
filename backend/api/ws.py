"""WebSocket endpoint for live simulation sessions.

Protocol (JSON text frames)
---------------------------
server → client
    hello   full scenario + snapshot (on connect, reset, resync)
    events  batched state changes since the previous tick, with sim time ``t``
    kpis    KPI document, about once per second while running
    status  playing / paused / finished and speed
    error   human-readable problem with the last command (never internals)
client → server
    play | pause | speed{value} | reset{overrides, seed} |
    inject_failure{machine, repair_time} | snapshot

Every server message carries an increasing ``seq`` so the client can discard
anything that belongs to a model it has already replaced.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from sim import load_scenario

from .schemas import Command, InjectFailureCmd, PauseCmd, PlayCmd, ResetCmd, SnapshotCmd, SpeedCmd
from .sessions import SessionLimitError, SessionManager, SessionStatus, SimSession
from .settings import Settings

log = logging.getLogger("factory.ws")
router = APIRouter()

_commands: TypeAdapter[Command] = TypeAdapter(Command)
MAX_MESSAGE_BYTES = 8 * 1024
MAX_COMMANDS_PER_SECOND = 20

# Application-defined close codes (4000-4999).
CLOSE_FORBIDDEN_ORIGIN = 4403
CLOSE_UNKNOWN_SCENARIO = 4404
CLOSE_TRY_AGAIN_LATER = 1013


def origin_allowed(origin: str | None, host: str | None, allowed: list[str]) -> bool:
    """Browsers do not apply CORS to WebSockets, so check Origin explicitly.

    Non-browser clients (no Origin header) are allowed; browsers are allowed
    from the configured origins or from the same host serving the API.
    """
    if not origin:
        return True
    if origin in allowed or "*" in allowed:
        return True
    return bool(host) and urlsplit(origin).netloc == host


class _Channel:
    """Serialises sends: the ticker and the command handler both write."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._lock:
            await self.ws.send_json(message)

    async def error(self, message: str, code: str = "bad_request") -> None:
        await self.send({"type": "error", "code": code, "message": message})


@router.websocket("/ws/session")
async def session_socket(
    ws: WebSocket,
    scenario: str = Query(max_length=40),
    resume: str | None = Query(default=None, max_length=64),
) -> None:
    settings: Settings = ws.app.state.settings
    manager: SessionManager = ws.app.state.sessions

    if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host"), settings.cors_origins):
        await ws.close(code=CLOSE_FORBIDDEN_ORIGIN)
        return
    await ws.accept()
    channel = _Channel(ws)

    try:
        base = load_scenario(scenario)
    except KeyError:
        await channel.error(f"Scenario '{scenario}' does not exist.", "unknown_scenario")
        await ws.close(code=CLOSE_UNKNOWN_SCENARIO)
        return

    session = manager.resume(resume, scenario) if resume else None
    if session is not None:
        manager.attach(session)
        log.info("session resumed id=%s", session.id[:6])
    else:
        try:
            session = manager.create(base)
        except SessionLimitError:
            await channel.error("The simulator is busy. Please try again in a minute.", "busy")
            await ws.close(code=CLOSE_TRY_AGAIN_LATER)
            return

    ticker = asyncio.create_task(_tick_loop(channel, session, settings))
    try:
        await channel.send(session.hello_message())
        await channel.send(session.kpi_message())
        await _receive_loop(channel, session)
    except WebSocketDisconnect:
        pass
    finally:
        ticker.cancel()
        manager.detach(session)
        log.info("session detached id=%s sim_t=%.0f", session.id[:6], session.model.env.now)


async def _receive_loop(channel: _Channel, session: SimSession) -> None:
    window, count = time.monotonic(), 0
    while True:
        raw = await channel.ws.receive_text()
        now = time.monotonic()
        if now - window >= 1.0:
            window, count = now, 0
        count += 1
        if count > MAX_COMMANDS_PER_SECOND:
            if count == MAX_COMMANDS_PER_SECOND + 1:
                await channel.error("Too many commands; slow down.", "rate_limited")
            continue
        if len(raw) > MAX_MESSAGE_BYTES:
            await channel.error("Message too large.")
            continue
        try:
            cmd = _commands.validate_json(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            await channel.error(f"Invalid command: {first.get('msg', 'invalid')}" + (f" ({where})" if where else ""))
            continue
        try:
            await _handle(channel, session, cmd)
        except ValueError as exc:  # includes OverrideError
            await channel.error(str(exc))


async def _handle(channel: _Channel, session: SimSession, cmd: Command) -> None:
    if isinstance(cmd, PlayCmd):
        session.play()
        await channel.send(session.status_message())
    elif isinstance(cmd, PauseCmd):
        session.pause()
        await channel.send(session.status_message())
        await channel.send(session.kpi_message())
    elif isinstance(cmd, SpeedCmd):
        session.set_speed(cmd.value)
        await channel.send(session.status_message())
    elif isinstance(cmd, ResetCmd):
        session.reset(dict(cmd.overrides), cmd.seed)
        await channel.send(session.hello_message())
        await channel.send(session.kpi_message())
    elif isinstance(cmd, InjectFailureCmd):
        if not session.inject_failure(cmd.machine, cmd.repair_time):
            await channel.error(f"{cmd.machine} is already down.", "already_down")
        # Process the interrupt immediately so the state change shows even while paused.
        session.model.settle()
    elif isinstance(cmd, SnapshotCmd):
        await channel.send(session.hello_message())


async def _tick_loop(channel: _Channel, session: SimSession, settings: Settings) -> None:
    last = time.monotonic()
    last_kpi = 0.0
    last_status = session.status
    try:
        while True:
            await asyncio.sleep(settings.tick_seconds)
            now = time.monotonic()
            try:
                session.step(now - last)
            except Exception:
                # A model bug must not kill the connection silently: pause and tell the user.
                log.exception("simulation step failed id=%s", session.id[:6])
                session.pause()
                await channel.error("The simulation hit an internal error and was paused.", "sim_error")
            last = now

            events = session.drain()
            playing = session.status == SessionStatus.PLAYING
            if events or playing:
                await channel.send(session.events_message(events))
            if session.status != last_status:
                last_status = session.status
                await channel.send(session.status_message())
            if (playing or events) and now - last_kpi >= settings.kpi_interval_seconds:
                last_kpi = now
                await channel.send(session.kpi_message())
    except (asyncio.CancelledError, WebSocketDisconnect):
        pass
    except RuntimeError:
        # Sending on a socket that closed between ticks; the receive loop cleans up.
        pass
