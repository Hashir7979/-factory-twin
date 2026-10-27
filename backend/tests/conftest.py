from __future__ import annotations

import copy
from typing import Any

import pytest

from sim.config import Scenario
from sim.scenarios import load_scenario


def exp(mean: float) -> dict[str, Any]:
    return {"type": "exponential", "mean": mean}


def const(value: float) -> dict[str, Any]:
    return {"type": "constant", "value": value}


def single_station(
    interarrival: dict[str, Any],
    process: dict[str, Any],
    *,
    servers: int = 1,
    capacity: int | None = None,
    failure: dict[str, Any] | None = None,
    horizon: float = 10_000,
    warmup: float = 0,
    seed: int = 1,
) -> Scenario:
    """Source → Q (capacity) → station → sink."""
    station: dict[str, Any] = {
        "id": "S",
        "name": "Server",
        "servers": servers,
        "input": "Q",
        "output": "sink",
        "process_time": process,
    }
    if failure:
        station["failure"] = failure
    return Scenario.model_validate(
        {
            "id": "single",
            "name": "single",
            "variants": [{"id": "A", "name": "A", "weight": 1}],
            "source": {"interarrival": interarrival, "output": "Q"},
            "buffers": [{"id": "Q", "name": "Queue", "capacity": capacity}],
            "stations": [station],
            "run": {"horizon": horizon, "warmup": warmup, "seed": seed},
        }
    )


def scenario_dict(scenario_id: str = "battery_line") -> dict[str, Any]:
    return copy.deepcopy(load_scenario(scenario_id).model_dump(mode="json"))


@pytest.fixture
def battery() -> Scenario:
    return load_scenario("battery_line")
