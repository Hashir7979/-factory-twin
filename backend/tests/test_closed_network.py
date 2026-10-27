"""Closed networks: a fixed fleet circulates (earthmoving trucks)."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from pydantic import ValidationError

from experiments.optimizer import cost_per_hour, default_variables
from sim import FactoryModel, Scenario, apply_overrides, compute_kpis, load_scenario
from sim.config import OverrideError


def const(v: float) -> dict[str, Any]:
    return {"type": "constant", "value": v}


def loop(fleet: int, **extra: Any) -> dict[str, Any]:
    """Deterministic loop: load 100 s, haul 300 s, dump 50 s, return 200 s (cycle 650 s)."""
    data: dict[str, Any] = {
        "id": "loop",
        "name": "loop",
        "entity": "truck",
        "output_station": "dump",
        "variants": [{"id": "T", "name": "T", "weight": 1}],
        "source": {"interarrival": const(1), "output": "ret", "max_arrivals": fleet},
        "buffers": [
            {
                "id": "haul",
                "name": "haul",
                "kind": "road",
                "capacity": 20,
                "transit_time": 300,
                "path": [[0, 0], [1, 0]],
            },
            {"id": "ret", "name": "ret", "kind": "road", "capacity": 20, "transit_time": 200, "path": [[1, 0], [0, 0]]},
        ],
        "stations": [
            {"id": "load", "name": "Load", "input": "ret", "output": "haul", "process_time": const(100)},
            {"id": "dump", "name": "Dump", "input": "haul", "output": "ret", "process_time": const(50)},
        ],
        "run": {"horizon": 65_000, "warmup": 2_000},
    }
    data.update(extra)
    return data


def run(sc: Scenario, seed: int | None = None) -> FactoryModel:
    model = FactoryModel(sc, seed=seed)
    model.run()
    return model


def test_single_truck_cycles_at_the_sum_of_activity_times() -> None:
    k = compute_kpis(run(Scenario.model_validate(loop(1))))
    assert k.line.throughput_per_hour == pytest.approx(3600 / 650, rel=0.01)
    assert k.line.mean_cycle_time == pytest.approx(650, abs=1)
    assert k.bottleneck.station == "fleet"


def test_large_fleet_is_limited_by_the_loader() -> None:
    k = compute_kpis(run(Scenario.model_validate(loop(10))))
    assert k.line.throughput_per_hour == pytest.approx(3600 / 100, rel=0.01)
    assert k.bottleneck.station == "load"
    ret = next(b for b in k.buffers if b.id == "ret")
    # 10 trucks share a 650 s loop with a 100 s loader: about 350 s of queueing per cycle.
    assert ret.mean_queue_delay == pytest.approx(10 * 100 - 650, rel=0.05)


def test_fleet_stays_in_the_system_and_units_scale_output() -> None:
    sc = Scenario.model_validate(loop(3, units_per_output=18, output_unit="m³"))
    model = run(sc)
    assert len(model.products) == 3 and model.monitor.total_exited == 0
    line = compute_kpis(model).line
    assert line.output_units_per_hour == pytest.approx(line.throughput_per_hour * 18)
    assert line.line_oee > 0  # falls back to the busiest station when the fleet is the constraint


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (loop(3, output_station=None), "output_station"),
        ({**loop(3), "source": {"interarrival": const(1), "output": "ret"}}, "max_arrivals"),
        (loop(25), "must hold the whole fleet"),
    ],
)
def test_closed_network_validation(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Scenario.model_validate(data)


def test_output_station_rejected_in_open_network() -> None:
    data = load_scenario("battery_line").model_dump(mode="json")
    data["output_station"] = "weld"
    with pytest.raises(ValidationError, match="only used by closed networks"):
        Scenario.model_validate(data)


def test_travel_time_rules() -> None:
    data = loop(2)
    data["buffers"][0].pop("transit_time")
    with pytest.raises(ValidationError, match="transit"):
        Scenario.model_validate(data)
    data = load_scenario("battery_line").model_dump(mode="json")
    data["buffers"][0]["transit_time"] = 5  # a plain queue
    with pytest.raises(ValidationError, match="only conveyors and roads"):
        Scenario.model_validate(data)


def test_fleet_override_respects_capacity() -> None:
    base = load_scenario("earthmoving")
    assert apply_overrides(base, {"source.max_arrivals": 7}).source.max_arrivals == 7
    with pytest.raises(OverrideError, match="whole fleet"):
        apply_overrides(base, {"source.max_arrivals": 21})


def test_random_road_times_vary_and_keep_their_mean() -> None:
    model = FactoryModel(load_scenario("earthmoving"))
    events: list[dict[str, Any]] = []
    model.monitor.listener = events.append
    model.env.run(until=36_000)
    trips = [e["ready_at"] - e["t"] for e in events if e["type"] == "enter" and e["buffer"] == "R_haul"]
    assert len(set(round(t) for t in trips)) > 10
    assert sum(trips) / len(trips) == pytest.approx(330, rel=0.1)


def test_earthmoving_fleet_curve() -> None:
    """Output rises with trucks, then plateaus once the excavator is the constraint."""
    base = load_scenario("earthmoving")
    tp: dict[int, float] = {}
    labels: dict[int, str] = {}
    for n in (2, 4, 6, 9):
        sc = apply_overrides(base, {"source.max_arrivals": n})
        ks = [compute_kpis(run(sc, seed)) for seed in range(1, 6)]
        tp[n] = sum(k.line.throughput_per_hour for k in ks) / len(ks)
        labels[n] = Counter(k.bottleneck.label for k in ks).most_common(1)[0][0] or ""
    assert tp[2] < tp[4] < tp[6]
    assert tp[9] == pytest.approx(tp[6], rel=0.05)
    assert labels[2] == "Truck fleet"
    assert labels[9] == "Excavator"


def test_optimizer_support_for_fleets() -> None:
    sc = load_scenario("earthmoving")
    keys = {v["key"]: v for v in default_variables(sc)}
    assert keys["source.max_arrivals"]["high"] <= 20
    assert not any(k.startswith("buffer.") for k in keys)  # road sizes are not decisions
    c = cost_per_hour(sc, sc.economics)
    assert c.fleet == 4 * 88 and c.machines == 165 + 95
