"""Simulation-based optimization of line configuration (Optuna TPE).

Decision variables are integer scenario overrides (buffer capacities, machines
per station, technicians, operators). Each candidate is evaluated with the same
set of seeds (common random numbers) so candidates are compared on equal terms,
and the objective is hourly contribution profit::

    profit/h = good parts/h * margin - machine, staff and buffer costs per hour

The async loop takes an ``evaluate`` callable so it is independent of how
replications are executed (process pool in the API, inline in tests).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import optuna
from pydantic import BaseModel, Field, model_validator

from sim import Scenario, apply_overrides
from sim.config import Economics

from .runner import ReplicationResult
from .stats import Difference, Estimate, estimate, paired_difference

optuna.logging.set_verbosity(optuna.logging.WARNING)
log = logging.getLogger("factory.optimizer")

INTEGER_KEYS = ("buffer.", "station.", "resources.", "source.max_arrivals")

__all__ = ["Economics", "Variable", "optimize"]


class Variable(BaseModel):
    key: str = Field(max_length=80)
    low: int = Field(ge=0, le=10_000)
    high: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def _check(self) -> Variable:
        if not self.key.startswith(INTEGER_KEYS):
            raise ValueError(f"'{self.key}' cannot be optimized (integer settings only)")
        if self.low > self.high:
            raise ValueError(f"{self.key}: low must be <= high")
        return self


class CostBreakdown(BaseModel):
    machines: float
    buffers: float
    technicians: float
    operators: float
    fleet: float = 0.0

    @property
    def total(self) -> float:
        return self.machines + self.buffers + self.technicians + self.operators + self.fleet


class Evaluation(BaseModel):
    params: dict[str, int]
    throughput: Estimate
    revenue_per_hour: float
    cost_per_hour: float
    profit: Estimate
    costs: CostBreakdown
    bottleneck: str | None


class Trial(BaseModel):
    number: int
    evaluation: Evaluation
    cached: bool


class Confirmation(BaseModel):
    """Re-evaluation of the top candidates on fresh seeds with more replications.

    The search picks the best of many noisy estimates, which is biased upwards
    (the winner is partly just lucky). Selecting again on independent seeds and
    testing the paired profit difference against the baseline removes that bias.
    """

    seeds: list[int]
    baseline: Evaluation
    candidates: list[Evaluation]
    profit_difference: Difference | None  # best - baseline, paired over seeds
    significant: bool


class OptimizationResult(BaseModel):
    scenario_id: str
    variables: list[Variable]
    economics: Economics
    seeds: list[int]
    baseline: Evaluation
    best: Evaluation  # confirmed best (may be the baseline itself)
    trials: list[Trial]
    pareto: list[int]  # trial numbers on the throughput/cost Pareto front
    confirmation: Confirmation
    improvement_per_hour: float


def cost_per_hour(scenario: Scenario, econ: Economics) -> CostBreakdown:
    machines = sum(
        st.servers * econ.station_machine_cost_per_hour.get(st.id, econ.machine_cost_per_hour)
        for st in scenario.stations
    )
    slots = sum(b.capacity or 0 for b in scenario.buffers)
    res = scenario.resources
    return CostBreakdown(
        machines=machines,
        buffers=slots * econ.buffer_slot_cost_per_hour,
        technicians=(res.technicians or 0) * econ.technician_cost_per_hour,
        operators=res.operators * econ.operator_cost_per_hour,
        fleet=(scenario.source.max_arrivals or 0) * econ.fleet_cost_per_hour if scenario.is_closed else 0.0,
    )


def evaluate(
    scenario: Scenario, params: dict[str, int], results: list[ReplicationResult], econ: Economics
) -> Evaluation:
    costs = cost_per_hour(scenario, econ)
    tp = [r.kpis.line.throughput_per_hour for r in results]
    profit = [t * econ.margin_per_part - costs.total for t in tp]
    tp_est = estimate(tp)
    bottlenecks = [r.kpis.bottleneck.station for r in results if r.kpis.bottleneck.station]
    return Evaluation(
        params=params,
        throughput=tp_est,
        revenue_per_hour=tp_est.mean * econ.margin_per_part,
        cost_per_hour=costs.total,
        profit=estimate(profit),
        costs=costs,
        bottleneck=max(set(bottlenecks), key=bottlenecks.count) if bottlenecks else None,
    )


def baseline_params(scenario: Scenario, variables: list[Variable]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in variables:
        kind, _, rest = v.key.partition(".")
        if kind == "buffer":
            node, field = rest.split(".", 1)
            out[v.key] = int(getattr(scenario.buffer(node), field) or 0)
        elif kind == "station":
            node, field = rest.split(".", 1)
            out[v.key] = int(getattr(scenario.station(node), field))
        elif kind == "source":
            out[v.key] = int(getattr(scenario.source, rest) or 0)
        else:
            out[v.key] = int(getattr(scenario.resources, rest) or 0)
    return out


def pareto_front(trials: list[Trial]) -> list[int]:
    """Trials not dominated on (higher throughput, lower cost)."""
    front = []
    for t in trials:
        e = t.evaluation
        dominated = any(
            o.evaluation.throughput.mean >= e.throughput.mean
            and o.evaluation.cost_per_hour <= e.cost_per_hour
            and (o.evaluation.throughput.mean > e.throughput.mean or o.evaluation.cost_per_hour < e.cost_per_hour)
            for o in trials
        )
        if not dominated:
            front.append(t.number)
    return front


Evaluator = Callable[[Scenario, list[int]], Awaitable[list[ReplicationResult]]]

CONFIRM_TOP_K = 3
CONFIRM_SEED_OFFSET = 100_000


def validate_variables(scenario: Scenario, variables: list[Variable]) -> None:
    """Fail fast on unknown keys or out-of-range bounds (raises OverrideError)."""
    if len({v.key for v in variables}) != len(variables):
        raise ValueError("each variable may appear only once")
    apply_overrides(scenario, {v.key: v.low for v in variables})
    apply_overrides(scenario, {v.key: v.high for v in variables})


def _profit_samples(results: list[ReplicationResult], cost: float, econ: Economics) -> list[float]:
    return [
        r.kpis.line.throughput_per_hour * econ.margin_per_part - cost for r in sorted(results, key=lambda r: r.seed)
    ]


async def optimize(
    scenario: Scenario,
    variables: list[Variable],
    econ: Economics,
    trials: int,
    seeds: list[int],
    evaluate_replications: Evaluator,
    progress: Callable[[float], None] | None = None,
    sampler_seed: int = 0,
) -> OptimizationResult:
    validate_variables(scenario, variables)
    confirm_seeds = [CONFIRM_SEED_OFFSET + s for s in range(max(5, 2 * len(seeds)))]
    total_steps = 1 + trials + 1 + CONFIRM_TOP_K + len(variables)
    step = 0

    def advance() -> None:
        nonlocal step
        step += 1
        if progress:
            progress(min(1.0, step / total_steps))

    base = baseline_params(scenario, variables)
    baseline = evaluate(scenario, base, await evaluate_replications(scenario, seeds), econ)
    advance()

    sampler = optuna.samplers.TPESampler(seed=sampler_seed, n_startup_trials=max(3, min(10, trials // 3)))
    study = optuna.create_study(direction="maximize", sampler=sampler)
    cache: dict[tuple[tuple[str, int], ...], Evaluation] = {tuple(sorted(base.items())): baseline}
    history: list[Trial] = []
    for n in range(trials):
        trial = study.ask()
        params = {v.key: trial.suggest_int(v.key, v.low, v.high) for v in variables}
        key = tuple(sorted(params.items()))
        cached = key in cache
        if not cached:
            candidate = apply_overrides(scenario, dict(params))
            cache[key] = evaluate(candidate, params, await evaluate_replications(candidate, seeds), econ)
        ev = cache[key]
        study.tell(trial, ev.profit.mean)
        history.append(Trial(number=n + 1, evaluation=ev, cached=cached))
        advance()

    # Confirmation stage: top distinct candidates vs baseline on fresh common seeds.
    base_key = tuple(sorted(base.items()))
    ranked = sorted((k for k in cache if k != base_key), key=lambda k: cache[k].profit.mean, reverse=True)
    top = [dict(k) for k in ranked[:CONFIRM_TOP_K]]
    base_results = await evaluate_replications(scenario, confirm_seeds)
    confirmed_base = evaluate(scenario, base, base_results, econ)
    base_profit = _profit_samples(base_results, confirmed_base.cost_per_hour, econ)
    advance()
    candidates: list[tuple[Evaluation, list[float]]] = []
    for params in top:
        cand_sc = apply_overrides(scenario, dict(params))
        res = await evaluate_replications(cand_sc, confirm_seeds)
        ev = evaluate(cand_sc, params, res, econ)
        candidates.append((ev, _profit_samples(res, ev.cost_per_hour, econ)))
        advance()

    best, best_profit = max(candidates, key=lambda c: c[0].profit.mean, default=(confirmed_base, base_profit))

    # Polish: the search may carry resources that do nothing but cost money (e.g. an
    # extra technician picked up by noise). Try removing one unit of each variable on
    # the confirmation seeds and keep the change when paired profit improves.
    if best is not confirmed_base:
        for v in variables:
            trial_params = dict(best.params)
            if trial_params[v.key] - 1 < v.low:
                advance()
                continue
            trial_params[v.key] -= 1
            trimmed_sc = apply_overrides(scenario, dict(trial_params))
            res = await evaluate_replications(trimmed_sc, confirm_seeds)
            ev = evaluate(trimmed_sc, trial_params, res, econ)
            if ev.profit.mean > best.profit.mean:
                best, best_profit = ev, _profit_samples(res, ev.cost_per_hour, econ)
                candidates.append((ev, best_profit))
            advance()
    diff: Difference | None = None
    significant = False
    if best.profit.mean > confirmed_base.profit.mean:
        diff = paired_difference("profit_per_hour", base_profit, best_profit)
        significant = diff.significant and diff.diff.mean > 0
    else:
        best = confirmed_base
    if progress:
        progress(1.0)

    log.info(
        "optimization done scenario=%s trials=%d best_profit=%.1f baseline=%.1f significant=%s",
        scenario.id, trials, best.profit.mean, confirmed_base.profit.mean, significant,
    )  # fmt: skip
    return OptimizationResult(
        scenario_id=scenario.id,
        variables=variables,
        economics=econ,
        seeds=seeds,
        baseline=confirmed_base,
        best=best,
        trials=history,
        pareto=pareto_front(history),
        confirmation=Confirmation(
            seeds=confirm_seeds,
            baseline=confirmed_base,
            candidates=[c[0] for c in candidates],
            profit_difference=diff,
            significant=significant,
        ),
        improvement_per_hour=best.profit.mean - confirmed_base.profit.mean,
    )


def default_variables(scenario: Scenario) -> list[dict[str, Any]]:
    """Sensible search space around the current configuration (used by the UI)."""
    out: list[dict[str, Any]] = []
    if scenario.is_closed and scenario.source.max_arrivals:
        # Fleet size is the classic decision in closed networks ("how many trucks?").
        # Its ceiling is the smallest buffer, which must hold the whole fleet.
        n = scenario.source.max_arrivals
        ceiling = min([b.capacity for b in scenario.buffers if b.capacity] or [500])
        out.append({"key": "source.max_arrivals", "low": max(1, n - 3), "high": min(ceiling, n + 4)})
    for st in scenario.stations:
        out.append({"key": f"station.{st.id}.servers", "low": max(1, st.servers - 1), "high": min(20, st.servers + 1)})
    for b in scenario.buffers:
        # Road / conveyor sizes in a closed loop are not design levers.
        if b.capacity and not scenario.is_closed:
            out.append(
                {"key": f"buffer.{b.id}.capacity", "low": max(1, b.capacity // 2), "high": min(10_000, b.capacity * 2)}
            )
    if scenario.resources.technicians:
        t = scenario.resources.technicians
        out.append({"key": "resources.technicians", "low": max(1, t - 1), "high": t + 2})
    return out
