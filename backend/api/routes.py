"""REST endpoints: scenarios, single runs, experiments, comparisons, jobs."""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response

from experiments.optimizer import Economics, default_variables, optimize, validate_variables
from experiments.runner import compare, summarize
from sim import Scenario, apply_overrides, list_scenarios, load_scenario
from sim.config import OverrideError
from sim.kpi import Kpis

from .jobs import JobManager, ProgressFn, TooManyJobsError
from .schemas import (
    ComparisonRequest,
    ExperimentRequest,
    JobAccepted,
    OptimizationRequest,
    RunRequest,
    ScenarioSummary,
)
from .settings import Settings

router = APIRouter(prefix="/api")

ScenarioId = Annotated[str, Path(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")]
JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,32}$")]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _jobs(request: Request) -> JobManager:
    jobs: JobManager = request.app.state.jobs
    return jobs


def _scenario(scenario_id: str, overrides: dict[str, float] | None = None) -> Scenario:
    try:
        base = load_scenario(scenario_id)
    except KeyError:
        raise HTTPException(404, f"Scenario '{scenario_id}' does not exist.") from None
    if not overrides:
        return base
    try:
        return apply_overrides(base, overrides)
    except OverrideError as exc:
        raise HTTPException(422, str(exc)) from None


def _run_window(
    settings: Settings, scenario: Scenario, horizon: float | None, warmup: float | None
) -> tuple[float, float]:
    h = scenario.run.horizon if horizon is None else horizon
    w = scenario.run.warmup if warmup is None else warmup
    if h > settings.max_batch_horizon:
        raise HTTPException(422, f"horizon must be at most {settings.max_batch_horizon:g} seconds")
    if w >= h:
        raise HTTPException(422, "warmup must be shorter than horizon")
    return h, w


def _check_replications(settings: Settings, n: int) -> None:
    if n > settings.max_replications:
        raise HTTPException(422, f"replications must be at most {settings.max_replications}")


def _submit(jobs: JobManager, kind: str, work: Any) -> JobAccepted:
    try:
        job = jobs.submit(kind, work)
    except TooManyJobsError:
        raise HTTPException(429, "Too many experiments are running. Please wait for one to finish.") from None
    return JobAccepted(job_id=job.id, status=job.status.value)


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {"status": "ok", "sessions": len(request.app.state.sessions)}


@router.get("/scenarios")
async def scenarios() -> list[ScenarioSummary]:
    return [
        ScenarioSummary(id=s.id, name=s.name, description=s.description, stations=len(s.stations),
                        buffers=len(s.buffers))
        for s in list_scenarios()
    ]  # fmt: skip


@router.get("/scenarios/{scenario_id}")
async def scenario_detail(scenario_id: ScenarioId) -> dict[str, Any]:
    return _scenario(scenario_id).model_dump(mode="json")


@router.get("/scenarios/{scenario_id}/optimizer-defaults")
async def optimizer_defaults(scenario_id: ScenarioId, request: Request) -> dict[str, Any]:
    scenario = _scenario(scenario_id)
    return {
        "variables": default_variables(scenario),
        "economics": Economics().model_dump(),
        "max_trials": _settings(request).max_optimizer_trials,
    }


@router.post("/runs")
async def single_run(body: RunRequest, request: Request) -> Kpis:
    """Run one replication synchronously (in the worker pool) and return its KPIs."""
    scenario = _scenario(body.scenario_id, body.overrides)
    horizon, warmup = _run_window(_settings(request), scenario, body.horizon, body.warmup)
    seed = scenario.run.seed if body.seed is None else body.seed
    [result] = await _jobs(request).map_replications(scenario, [seed], horizon, warmup)
    return result.kpis


@router.post("/experiments", status_code=202)
async def start_experiment(body: ExperimentRequest, request: Request) -> JobAccepted:
    settings, jobs = _settings(request), _jobs(request)
    _check_replications(settings, body.replications)
    scenario = _scenario(body.scenario_id, body.overrides)
    horizon, warmup = _run_window(settings, scenario, body.horizon, body.warmup)
    seeds = list(range(body.base_seed, body.base_seed + body.replications))
    overrides = dict(body.overrides)

    async def work(progress: ProgressFn) -> dict[str, Any]:
        results = await jobs.map_replications(scenario, seeds, horizon, warmup, progress)
        return summarize(scenario, overrides, horizon, warmup, results).model_dump(mode="json")

    return _submit(jobs, "experiment", work)


@router.post("/comparisons", status_code=202)
async def start_comparison(body: ComparisonRequest, request: Request) -> JobAccepted:
    settings, jobs = _settings(request), _jobs(request)
    _check_replications(settings, body.replications)
    sc_a = _scenario(body.scenario_id, body.a_overrides)
    sc_b = _scenario(body.scenario_id, body.b_overrides)
    horizon, warmup = _run_window(settings, sc_a, body.horizon, body.warmup)
    # Same seeds for both arms → common random numbers → paired comparison.
    seeds = list(range(body.base_seed, body.base_seed + body.replications))

    async def work(progress: ProgressFn) -> dict[str, Any]:
        res_a = await jobs.map_replications(sc_a, seeds, horizon, warmup, lambda p: progress(p / 2))
        res_b = await jobs.map_replications(sc_b, seeds, horizon, warmup, lambda p: progress(0.5 + p / 2))
        a = summarize(sc_a, dict(body.a_overrides), horizon, warmup, res_a)
        b = summarize(sc_b, dict(body.b_overrides), horizon, warmup, res_b)
        out = compare(a, b).model_dump(mode="json")
        out["labels"] = {"a": body.a_label, "b": body.b_label}
        return out

    return _submit(jobs, "comparison", work)


@router.post("/optimizations", status_code=202)
async def start_optimization(body: OptimizationRequest, request: Request) -> JobAccepted:
    settings, jobs = _settings(request), _jobs(request)
    if body.trials > settings.max_optimizer_trials:
        raise HTTPException(422, f"trials must be at most {settings.max_optimizer_trials}")
    scenario = _scenario(body.scenario_id)
    horizon, warmup = _run_window(settings, scenario, body.horizon, body.warmup)
    try:
        validate_variables(scenario, body.variables)
    except ValueError as exc:  # OverrideError or duplicate keys
        raise HTTPException(422, str(exc)) from None
    seeds = list(range(body.base_seed, body.base_seed + body.replications))

    async def work(progress: ProgressFn) -> dict[str, Any]:
        async def evaluate(candidate: Scenario, run_seeds: list[int]) -> list[Any]:
            return await jobs.map_replications(candidate, run_seeds, horizon, warmup)

        result = await optimize(
            scenario,
            body.variables,
            body.economics,
            body.trials,
            seeds,
            evaluate,
            progress,
            sampler_seed=body.base_seed,
        )
        return result.model_dump(mode="json")

    return _submit(jobs, "optimization", work)


@router.get("/jobs/{job_id}")
async def job_status(job_id: JobId, request: Request) -> dict[str, Any]:
    job = _jobs(request).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or expired.")
    return job.public()


@router.get("/jobs/{job_id}/runs.csv")
async def job_runs_csv(job_id: JobId, request: Request) -> Response:
    """Per-replication line KPIs as CSV, for analysis in Excel / pandas."""
    job = _jobs(request).get(job_id)
    if job is None or job.result is None:
        raise HTTPException(404, "Job not found, expired or not finished.")
    arms = [("a", job.result["a"]), ("b", job.result["b"])] if job.kind == "comparison" else [("", job.result)]
    buf = io.StringIO()
    writer: csv.DictWriter[str] | None = None
    for arm, result in arms:
        if "runs" not in result:
            raise HTTPException(404, "This job has no per-replication data.")
        for seed, run in zip(result["seeds"], result["runs"], strict=True):
            row = {"arm": arm, "seed": seed, **run} if arm else {"seed": seed, **run}
            if writer is None:
                writer = csv.DictWriter(buf, fieldnames=list(row))
                writer.writeheader()
            writer.writerow(row)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{job.kind}-{job.id}.csv"'},
    )
