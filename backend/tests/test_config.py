from __future__ import annotations

import pytest
from pydantic import ValidationError

from sim import Scenario, apply_overrides
from sim.config import OverrideError
from tests.conftest import scenario_dict


def test_bundled_scenario_is_valid(battery: Scenario) -> None:
    assert battery.station("weld").name == "Laser Welding"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["stations"][0].update(input="nope"), "is not a buffer"),
        (lambda d: d["stations"][1].update(input="B0"), "must feed exactly one station"),
        (lambda d: d["buffers"][0].update(id="sink"), "reserved"),
        (lambda d: d["stations"][3]["qc"].update(pass_prob=0.99), "must be <= 1"),
        (lambda d: d["buffers"][1].update(path=None), "requires a path"),
        (lambda d: d["stations"][0].update(unknown_field=1), "Extra inputs"),
        (lambda d: d["stations"][0].update(process_time={"type": "normal", "mean": -1, "std": 1}), "greater than 0"),
        (lambda d: d["resources"].update(operators=0), "needs_operator"),
        (lambda d: d["run"].update(warmup=10**9), "warmup"),
        (lambda d: d["stations"].append(dict(d["stations"][0])), "duplicate ids"),
    ],
)
def test_invalid_scenarios_are_rejected(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    data = scenario_dict()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        Scenario.model_validate(data)


def test_overrides_apply_and_validate(battery: Scenario) -> None:
    sc = apply_overrides(battery, {"buffer.B2.capacity": 12, "station.weld.servers": 2, "resources.technicians": 3})
    assert sc.buffer("B2").capacity == 12
    assert sc.station("weld").servers == 2
    assert sc.resources.technicians == 3
    assert battery.buffer("B2").capacity == 6  # original untouched


def test_interarrival_scale(battery: Scenario) -> None:
    sc = apply_overrides(battery, {"source.interarrival_scale": 2})
    assert sc.source.interarrival.mean == 90  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "overrides",
    [
        {"buffer.B2.name": "x"},
        {"station.weld.process_time": 1},
        {"buffer.ZZ.capacity": 3},
        {"buffer.B2.capacity": 0},
        {"buffer.B2.capacity": 2.5},
        {"buffer.B2.capacity": "abc"},
        {"run.seed": 1},
        {"__class__": 1},
    ],
)
def test_bad_overrides_rejected(battery: Scenario, overrides: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(OverrideError):
        apply_overrides(battery, overrides)
