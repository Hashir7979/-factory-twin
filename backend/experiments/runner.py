"""Replications and experiment summaries.

``run_replication`` is a top-level function so it can be shipped to worker
processes. Summaries are pure functions of replication results, which keeps
them trivially testable.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel

from sim import FactoryModel, Scenario, compute_kpis
from sim.kpi import Kpis, LineKpi

from .stats import Difference, Estimate, WarmupAnalysis, binned_means, estimate, paired_difference, welch_warmup

WARMUP_BIN_SECONDS = 900.0

LINE_METRICS = (
    "throughput_per_hour",
    "mean_cycle_time",
    "mean_wip",
    "line_oee",
    "first_pass_yield",
    "scrapped",
)
STATION_METRICS = ("utilization", "oee", "active_pct", "blocked_pct", "starved_pct", "down_pct")
BUFFER_METRICS = ("mean_level", "mean_wait")


class ReplicationResult(BaseModel):
    seed: int
    kpis: Kpis
    cycle_time_bins: list[float | None]


def run_replication(scenario: Scenario, seed: int, horizon: float, warmup: float) -> ReplicationResult:
    model = FactoryModel(scenario, seed=seed)
    model.run(horizon=horizon, warmup=warmup)
    mon = model.monitor
    return ReplicationResult(
        seed=seed,
        kpis=compute_kpis(model),
        cycle_time_bins=binned_means(mon.completion_times, mon.completion_cycle_times, horizon, WARMUP_BIN_SECONDS),
    )


class ExperimentResult(BaseModel):
    scenario_id: str
    overrides: dict[str, Any]
    horizon: float
    warmup: float
    seeds: list[int]
    metrics: dict[str, Estimate]
    stations: dict[str, dict[str, Estimate]]
    buffers: dict[str, dict[str, Estimate]]
    bottleneck_frequency: dict[str, float]
    warmup_analysis: WarmupAnalysis
    runs: list[LineKpi]


def summarize(
    scenario: Scenario, overrides: dict[str, Any], horizon: float, warmup: float, results: list[ReplicationResult]
) -> ExperimentResult:
    results = sorted(results, key=lambda r: r.seed)
    lines = [r.kpis.line for r in results]
    metrics = {m: estimate([float(getattr(line, m)) for line in lines]) for m in LINE_METRICS}

    stations: dict[str, dict[str, Estimate]] = {}
    for st in scenario.stations:
        rows = [next(s for s in r.kpis.stations if s.id == st.id) for r in results]
        stations[st.id] = {m: estimate([getattr(row, m) for row in rows]) for m in STATION_METRICS}

    buffers: dict[str, dict[str, Estimate]] = {}
    for b in scenario.buffers:
        rows_b = [next(x for x in r.kpis.buffers if x.id == b.id) for r in results]
        buffers[b.id] = {m: estimate([getattr(row, m) for row in rows_b]) for m in BUFFER_METRICS}

    counts = Counter(r.kpis.bottleneck.station for r in results if r.kpis.bottleneck.station)
    n = len(results) or 1
    return ExperimentResult(
        scenario_id=scenario.id,
        overrides=overrides,
        horizon=horizon,
        warmup=warmup,
        seeds=[r.seed for r in results],
        metrics=metrics,
        stations=stations,
        buffers=buffers,
        bottleneck_frequency={k: v / n for k, v in counts.most_common()},
        warmup_analysis=welch_warmup([r.cycle_time_bins for r in results], WARMUP_BIN_SECONDS),
        runs=lines,
    )


class ComparisonResult(BaseModel):
    a: ExperimentResult
    b: ExperimentResult
    differences: list[Difference]


def compare(a: ExperimentResult, b: ExperimentResult) -> ComparisonResult:
    """Paired comparison. Both experiments must share seeds (common random numbers)."""
    if a.seeds != b.seeds:
        raise ValueError("comparison requires identical seeds for both scenarios")
    diffs = [
        paired_difference(m, [float(getattr(r, m)) for r in a.runs], [float(getattr(r, m)) for r in b.runs])
        for m in LINE_METRICS
    ]
    return ComparisonResult(a=a, b=b, differences=diffs)
