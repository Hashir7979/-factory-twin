from __future__ import annotations

from typing import Any

import pytest

from sim import FactoryModel, Scenario, apply_overrides, compute_kpis, list_scenarios, load_scenario
from sim.monitor import MachineState
from tests.conftest import const, exp, single_station


def run(scenario: Scenario, **kw: Any) -> FactoryModel:
    model = FactoryModel(scenario, **kw)
    model.run()
    return model


def test_battery_line_conservation_and_capacity(battery: Scenario) -> None:
    model = run(battery)
    mon = model.monitor
    in_system = len(model.products)
    assert mon.total_created == mon.total_exited + in_system
    assert mon.wip.value == in_system
    # Every product in the system is in exactly one place: a buffer or a machine.
    located = [p.id for b in model.buffers.values() for p in b.contents()]
    located += [m.product.id for m in model.machines.values() if m.product]
    assert sorted(located) == sorted(model.products)
    for b in compute_kpis(model).buffers:
        if b.capacity is not None:
            assert b.max_level <= b.capacity


def test_same_seed_is_deterministic(battery: Scenario) -> None:
    a = compute_kpis(run(battery, seed=5)).model_dump()
    b = compute_kpis(run(battery, seed=5)).model_dump()
    c = compute_kpis(run(battery, seed=6)).model_dump()
    assert a == b
    assert a != c


def test_battery_bottleneck_is_welding_and_moves_when_relieved(battery: Scenario) -> None:
    assert compute_kpis(run(battery)).bottleneck.station == "weld"
    relieved = apply_overrides(battery, {"station.weld.servers": 2})
    k = compute_kpis(run(relieved))
    assert k.bottleneck.station == "stack"
    assert k.line.throughput_per_hour > compute_kpis(run(battery)).line.throughput_per_hour


def test_blocking_after_service_limits_throughput() -> None:
    """Fast M1 feeding slow M2 through a 1-slot buffer: M1 must be blocked ~half the time."""
    sc = Scenario.model_validate(
        {
            "id": "block",
            "name": "block",
            "variants": [{"id": "A", "name": "A", "weight": 1}],
            "source": {"interarrival": const(0.5), "output": "Q0"},
            "buffers": [{"id": "Q0", "name": "Q0", "capacity": 1}, {"id": "Q1", "name": "Q1", "capacity": 1}],
            "stations": [
                {"id": "M1", "name": "M1", "input": "Q0", "output": "Q1", "process_time": const(1)},
                {"id": "M2", "name": "M2", "input": "Q1", "output": "sink", "process_time": const(2)},
            ],
            "run": {"horizon": 3_600, "warmup": 100},
        }
    )
    k = compute_kpis(run(sc))
    assert k.line.throughput_per_hour == pytest.approx(1800, rel=0.01)
    m1 = next(s for s in k.stations if s.id == "M1")
    assert m1.blocked_pct == pytest.approx(0.5, abs=0.02)
    assert k.bottleneck.station == "M2"


def test_failures_reduce_availability_preempt_resume() -> None:
    # Saturated machine, 1 s parts, fails every 100 s for 10 s → availability 100/110.
    sc = single_station(
        const(0.1), const(1), capacity=5, failure={"ttf": const(100), "ttr": const(10)}, horizon=11_000, warmup=0
    )
    k = compute_kpis(run(sc))
    st = k.stations[0]
    assert st.availability == pytest.approx(100 / 110, abs=0.01)
    assert k.line.throughput_per_hour / 3600 == pytest.approx(100 / 110, rel=0.01)


def test_injected_failure_on_idle_machine_loses_no_parts() -> None:
    sc = single_station(const(50), const(1), capacity=5, horizon=1_000)
    model = FactoryModel(sc)
    model.env.run(until=10)
    assert model.machines["S#1"].state == MachineState.STARVED
    assert model.inject_failure("S#1", repair_time=120) is True
    assert model.inject_failure("S#1") is False  # already down
    model.env.run(until=11)
    assert model.machines["S#1"].state == MachineState.DOWN
    model.env.run(until=200)
    # Parts that arrived at t=50,100 waited for the repair (ends t=130).
    assert model.machines["S#1"].state == MachineState.STARVED
    assert model.monitor.total_completed == 3
    model.env.run(until=1_000)
    mon = model.monitor
    assert mon.total_created == mon.total_exited + len(model.products)


def test_operator_pool_is_shared_constraint() -> None:
    sc = Scenario.model_validate(
        {
            "id": "ops",
            "name": "ops",
            "variants": [{"id": "A", "name": "A", "weight": 1}],
            "source": {"interarrival": const(0.1), "output": "Q0"},
            "buffers": [{"id": "Q0", "name": "Q0", "capacity": 2}, {"id": "Q1", "name": "Q1", "capacity": 2}],
            "stations": [
                {
                    "id": "M1",
                    "name": "M1",
                    "input": "Q0",
                    "output": "Q1",
                    "process_time": const(1),
                    "needs_operator": True,
                },
                {
                    "id": "M2",
                    "name": "M2",
                    "input": "Q1",
                    "output": "sink",
                    "process_time": const(1),
                    "needs_operator": True,
                },
            ],
            "resources": {"operators": 1},
            "run": {"horizon": 2_000, "warmup": 100},
        }
    )
    k = compute_kpis(run(sc))
    assert k.line.throughput_per_hour / 3600 == pytest.approx(0.5, rel=0.02)
    two_ops = apply_overrides(sc, {"resources.operators": 2})
    assert compute_kpis(run(two_ops)).line.throughput_per_hour / 3600 == pytest.approx(1.0, rel=0.02)


def test_qc_routing_fractions(battery: Scenario) -> None:
    # Pool replications: ~9000 inspections gives sd ≈ 0.003 on the pass fraction,
    # so ±0.01 is a >3-sigma bound and the test is not flaky.
    passed = reworked = inspected = 0
    for seed in range(1, 6):
        model = FactoryModel(battery, seed=seed)
        model.run(horizon=100_000, warmup=1_000)
        test = model.stations["test"]
        passed += test.passed
        reworked += test.reworked
        inspected += test.passed + test.reworked + test.scrapped
    assert passed / inspected == pytest.approx(0.92, abs=0.01)
    assert reworked / inspected == pytest.approx(0.06, abs=0.01)


def test_warmup_resets_statistics(battery: Scenario) -> None:
    model = run(battery)
    mon = model.monitor
    assert mon.stats_start == battery.run.warmup
    assert mon.created < mon.total_created
    assert compute_kpis(model).line.observed_time == battery.run.horizon - battery.run.warmup


def test_live_advance_emits_events_and_snapshot(battery: Scenario) -> None:
    events: list[dict[str, Any]] = []
    model = FactoryModel(battery, listener=events.append)
    model.advance(600)
    kinds = {e["type"] for e in events}
    assert {"create", "enter", "take", "state"} <= kinds
    assert all(events[i]["t"] <= events[i + 1]["t"] for i in range(len(events) - 1))
    snap = model.snapshot()
    assert snap["t"] == 600
    assert set(snap["machines"]) == set(model.machines)
    # Crossing the warm-up boundary while live resets statistics exactly once.
    model.advance(battery.run.warmup + 10)
    assert model.warmed_up and model.monitor.stats_start == battery.run.warmup


def test_exponential_variant_mix(battery: Scenario) -> None:
    model = FactoryModel(battery, seed=3)
    model.run(horizon=100_000, warmup=1)
    by_variant = model.monitor.cycle_time_by_variant
    total = sum(t.n for t in by_variant.values())
    assert by_variant["M12"].n / total == pytest.approx(0.6, abs=0.03)


def test_unbounded_queue_with_exponential() -> None:
    model = run(single_station(exp(2), exp(1), horizon=5_000))
    assert model.monitor.total_completed > 0


@pytest.mark.parametrize("scenario_id", [s.id for s in list_scenarios()])
def test_every_bundled_scenario_runs_and_conserves_parts(scenario_id: str) -> None:
    model = FactoryModel(load_scenario(scenario_id))
    model.run(horizon=7_200, warmup=600)
    mon = model.monitor
    assert mon.total_completed > 0
    assert mon.total_created == mon.total_exited + len(model.products)
    assert compute_kpis(model).bottleneck.station is not None
