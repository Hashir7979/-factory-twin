"""Statistics collection.

Everything here supports a warm-up reset: at ``reset(t)`` accumulated statistics
are discarded but the *current* state of the system (queue lengths, machine
states, parts in progress) is kept, which is exactly what steady-state analysis
needs.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from enum import StrEnum
from typing import Any

Listener = Callable[[dict[str, Any]], None]


class MachineState(StrEnum):
    STARVED = "starved"  # idle, waiting for a part
    WAITING_OPERATOR = "waiting_operator"
    SETUP = "setup"
    PROCESSING = "processing"
    BLOCKED = "blocked"  # finished part cannot move downstream
    DOWN = "down"  # failed / under repair


# States in which a machine constrains the line (Roser's "active" states).
ACTIVE_STATES = frozenset({MachineState.SETUP, MachineState.PROCESSING, MachineState.DOWN})


class TimeWeighted:
    """Time-weighted average of a piecewise-constant quantity (queue length, WIP)."""

    __slots__ = ("_area", "_last_t", "_start", "max", "value")

    def __init__(self, t: float, value: float = 0.0) -> None:
        self.value = value
        self._last_t = t
        self._area = 0.0
        self._start = t
        self.max = value

    def update(self, t: float, value: float) -> None:
        self._area += self.value * (t - self._last_t)
        self._last_t = t
        self.value = value
        if value > self.max:
            self.max = value

    def add(self, t: float, delta: float) -> None:
        self.update(t, self.value + delta)

    def mean(self, t: float) -> float:
        span = t - self._start
        if span <= 0:
            return self.value
        return (self._area + self.value * (t - self._last_t)) / span

    def reset(self, t: float) -> None:
        self._area = 0.0
        self._last_t = t
        self._start = t
        self.max = self.value


class Tally:
    """Running mean / variance of observations (Welford)."""

    __slots__ = ("_m2", "_mean", "max", "min", "n")

    def __init__(self) -> None:
        self.n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self.min = math.inf
        self.max = -math.inf

    def add(self, x: float) -> None:
        self.n += 1
        d = x - self._mean
        self._mean += d / self.n
        self._m2 += d * (x - self._mean)
        self.min = min(self.min, x)
        self.max = max(self.max, x)

    @property
    def mean(self) -> float:
        return self._mean if self.n else 0.0

    @property
    def std(self) -> float:
        return math.sqrt(self._m2 / (self.n - 1)) if self.n > 1 else 0.0


class StateTracker:
    """Time spent in each machine state plus active-period statistics."""

    def __init__(self, t: float, state: MachineState) -> None:
        self.state = state
        self._since = t
        self._start = t
        self.durations: dict[MachineState, float] = {s: 0.0 for s in MachineState}
        self._active_since: float | None = t if state in ACTIVE_STATES else None
        self.active_periods = Tally()

    def change(self, t: float, state: MachineState) -> None:
        if state == self.state:
            return
        self.durations[self.state] += t - self._since
        was_active = self.state in ACTIVE_STATES
        is_active = state in ACTIVE_STATES
        if was_active and not is_active and self._active_since is not None:
            self.active_periods.add(t - self._active_since)
            self._active_since = None
        elif is_active and not was_active:
            self._active_since = t
        self.state = state
        self._since = t

    def totals(self, t: float) -> dict[MachineState, float]:
        out = dict(self.durations)
        out[self.state] += t - self._since
        return out

    def elapsed(self, t: float) -> float:
        return t - self._start

    def reset(self, t: float) -> None:
        self.durations = {s: 0.0 for s in MachineState}
        self._since = t
        self._start = t
        self.active_periods = Tally()
        if self._active_since is not None:
            self._active_since = t


class Monitor:
    """Line-level counters plus an optional event listener for live streaming."""

    def __init__(self, listener: Listener | None = None) -> None:
        self.listener = listener
        self.stats_start = 0.0
        self.wip = TimeWeighted(0.0)
        self.created = 0
        self.completed = 0
        self.scrapped = 0
        self.reworked = 0
        # Never reset: used for conservation checks (created = done + scrap + WIP).
        self.total_created = 0
        self.total_completed = 0
        self.total_scrapped = 0
        self.total_exited = 0  # left the system at the sink or as scrap
        self.cycle_time = Tally()
        self.cycle_time_by_variant: dict[str, Tally] = {}
        # Completion timestamps for the whole run (warm-up included): used for
        # throughput time series and Welch's warm-up analysis.
        self.completion_times: list[float] = []
        self.completion_cycle_times: list[float] = []

    def emit(self, event: dict[str, Any]) -> None:
        if self.listener is not None:
            self.listener(event)

    @property
    def streaming(self) -> bool:
        return self.listener is not None

    def reset(self, t: float) -> None:
        self.stats_start = t
        self.wip.reset(t)
        self.created = 0
        self.completed = 0
        self.scrapped = 0
        self.reworked = 0
        self.cycle_time = Tally()
        self.cycle_time_by_variant = {}
