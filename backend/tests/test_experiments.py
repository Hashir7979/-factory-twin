from __future__ import annotations

import asyncio

import pytest

from experiments.optimizer import Economics, Variable, cost_per_hour, optimize, pareto_front
from experiments.runner import ReplicationResult, compare, run_replication, summarize
from experiments.stats import estimate, mser_truncation, paired_difference, welch_warmup
from sim import Scenario, apply_overrides


def test_estimate_confidence_interval() -> None:
    e = estimate([10, 12, 11, 13, 9])
    assert e.mean == pytest.approx(11)
    # t(0.975, 4) = 2.776, s = 1.581 → half width 1.963
    assert e.half_width == pytest.approx(1.963, abs=1e-3)
    assert estimate([5]).half_width == 0
    assert estimate([]).n == 0


def test_paired_difference_detects_consistent_improvement() -> None:
    a = [10.0, 20.0, 30.0, 40.0]
    b = [11.0, 21.2, 30.9, 41.1]  # noisy between seeds, consistent within pairs
    d = paired_difference("x", a, b)
    assert d.significant and d.diff.mean == pytest.approx(1.05)
    assert not paired_difference("x", a, [10.5, 19.5, 30.4, 39.6]).significant


def test_warmup_finds_end_of_transient() -> None:
    ramp = [float(min(i, 10)) for i in range(40)]  # rises for 10 bins then flat
    w = welch_warmup([ramp, ramp], bin_seconds=60, window=1)
    assert w.steady_state == pytest.approx(10)
    assert w.suggested_warmup is not None and 6 * 60 <= w.suggested_warmup <= 11 * 60


def test_warmup_is_robust_to_noise() -> None:
    import numpy as np

    rng = np.random.default_rng(0)
    noisy = [float(min(i, 8) * 2 + rng.normal(0, 3)) for i in range(48)]  # ±15% noise on the plateau
    w = welch_warmup([noisy], bin_seconds=900)
    assert w.suggested_warmup is not None and 4 * 900 <= w.suggested_warmup <= 14 * 900


def test_mser_needs_enough_data() -> None:
    assert mser_truncation([1.0, 2.0, None]) is None


def _reps(scenario: Scenario, seeds: list[int]) -> list[ReplicationResult]:
    return [run_replication(scenario, s, 14_400, 1_800) for s in seeds]


def test_summarize_and_compare_with_crn(battery: Scenario) -> None:
    seeds = [1, 2, 3, 4, 5]

    def reps(sc: Scenario) -> list[ReplicationResult]:
        return [run_replication(sc, s, 32_400, 3_600) for s in seeds]

    a = summarize(battery, {}, 32_400, 3_600, reps(battery))
    relieved = apply_overrides(battery, {"station.weld.servers": 2})
    b = summarize(relieved, {"station.weld.servers": 2}, 32_400, 3_600, reps(relieved))
    assert a.bottleneck_frequency.get("weld", 0) >= 0.8
    tp = next(d for d in compare(a, b).differences if d.metric == "throughput_per_hour")
    assert tp.diff.mean > 0 and tp.significant
    with pytest.raises(ValueError):
        compare(a, summarize(battery, {}, 14_400, 1_800, _reps(battery, [7, 8, 9, 10, 11])))


def test_cost_model(battery: Scenario) -> None:
    econ = Economics(
        machine_cost_per_hour=10, buffer_slot_cost_per_hour=1, technician_cost_per_hour=5, operator_cost_per_hour=2
    )
    c = cost_per_hour(battery, econ)
    assert c.machines == 7 * 10  # 6 stations, stacking has 2 machines
    assert c.buffers == 10 + 8 + 6 + 6 + 10 + 8
    assert c.total == 70 + 48 + 5 + 4


def test_optimizer_finds_second_welder_when_margin_is_high(battery: Scenario) -> None:
    seeds = [1, 2]

    async def evaluate(sc: Scenario, run_seeds: list[int]) -> list[ReplicationResult]:
        # Full 9 h shifts: short runs are dominated by failure timing and would make
        # the (correct) significance test inconclusive.
        return [run_replication(sc, s, 32_400, 3_600) for s in run_seeds]

    result = asyncio.run(
        optimize(
            battery,
            [Variable(key="station.weld.servers", low=1, high=2)],
            Economics(margin_per_part=500),
            trials=4,
            seeds=seeds,
            evaluate_replications=evaluate,
        )
    )
    assert result.baseline.params == {"station.weld.servers": 1}
    assert result.best.params == {"station.weld.servers": 2}
    assert result.improvement_per_hour > 0
    assert any(t.cached for t in result.trials)  # only two candidates exist → repeats are cached
    conf = result.confirmation
    assert conf.seeds and not set(conf.seeds) & set(seeds)  # fresh seeds, not the search seeds
    assert conf.significant and conf.profit_difference is not None


def test_optimizer_keeps_baseline_when_nothing_helps(battery: Scenario) -> None:
    async def evaluate(sc: Scenario, run_seeds: list[int]) -> list[ReplicationResult]:
        return _reps(sc, run_seeds[:3])

    # A second packaging machine cannot raise throughput (packaging is not the constraint) but costs money.
    result = asyncio.run(
        optimize(
            battery,
            [Variable(key="station.pack.servers", low=1, high=2)],
            Economics(machine_cost_per_hour=400),
            trials=3,
            seeds=[1, 2],
            evaluate_replications=evaluate,
        )
    )
    assert result.best.params == {"station.pack.servers": 1}
    assert result.improvement_per_hour == 0
    assert not result.confirmation.significant


def test_pareto_front_excludes_dominated() -> None:
    from experiments.optimizer import CostBreakdown, Evaluation, Trial

    def t(n: int, tp: float, cost: float) -> Trial:
        ev = Evaluation(params={}, throughput=estimate([tp]), revenue_per_hour=0, cost_per_hour=cost,
                        profit=estimate([0]), costs=CostBreakdown(machines=cost, buffers=0, technicians=0, operators=0),
                        bottleneck=None)  # fmt: skip
        return Trial(number=n, evaluation=ev, cached=False)

    assert pareto_front([t(1, 10, 5), t(2, 12, 6), t(3, 9, 7), t(4, 12, 5)]) == [4]
