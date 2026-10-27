"""KPI calculation.

OEE definition used here (per station, averaged over its parallel machines)::

    T            = observed time (after warm-up)
    run_time     = T - down - setup
    availability = run_time / T
    performance  = ideal_cycle_time * parts_processed / run_time
    quality      = first-pass yield of the line (good / inspected at QC stations)
    OEE          = availability * performance * quality

Starved and blocked time show up as *performance* losses, which is how they
appear on a real line when a machine is "running" but not producing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from .bottleneck import BottleneckReport, detect_bottleneck
from .monitor import MachineState

if TYPE_CHECKING:
    from .model import FactoryModel


class MachineKpi(BaseModel):
    id: str
    station: str
    state: str
    processed: int
    failures: int
    state_pct: dict[str, float]
    utilization: float  # share of time PROCESSING
    mean_active_period: float


class StationKpi(BaseModel):
    id: str
    name: str
    servers: int
    processed: int
    utilization: float
    availability: float
    performance: float
    oee: float
    active_pct: float
    blocked_pct: float
    starved_pct: float
    down_pct: float
    passed: int
    reworked: int
    scrapped: int


class BufferKpi(BaseModel):
    id: str
    name: str
    capacity: int | None
    level: int
    mean_level: float
    max_level: float
    mean_wait: float
    fill_pct: float | None


class LineKpi(BaseModel):
    sim_time: float
    observed_time: float
    created: int
    completed: int
    scrapped: int
    reworked: int
    wip: int
    mean_wip: float
    throughput_per_hour: float
    mean_cycle_time: float
    cycle_time_std: float
    first_pass_yield: float
    source_blocked_pct: float
    line_oee: float


class Kpis(BaseModel):
    line: LineKpi
    stations: list[StationKpi]
    machines: list[MachineKpi]
    buffers: list[BufferKpi]
    bottleneck: BottleneckReport


def _pct(part: float, whole: float) -> float:
    return part / whole if whole > 0 else 0.0


def compute_kpis(model: FactoryModel) -> Kpis:
    now = model.env.now
    mon = model.monitor
    observed = now - mon.stats_start

    machines: list[MachineKpi] = []
    totals_by_machine = {}
    for m in model.machines.values():
        totals = m.tracker.totals(now)
        totals_by_machine[m.id] = totals
        elapsed = m.tracker.elapsed(now)
        machines.append(
            MachineKpi(
                id=m.id,
                station=m.station.id,
                state=m.state.value,
                processed=m.processed,
                failures=m.failures,
                state_pct={s.value: _pct(totals[s], elapsed) for s in MachineState},
                utilization=_pct(totals[MachineState.PROCESSING], elapsed),
                mean_active_period=m.tracker.active_periods.mean,
            )
        )

    inspected = sum(s.passed + s.reworked + s.scrapped for s in model.stations.values() if s.cfg.qc)
    passed = sum(s.passed for s in model.stations.values() if s.cfg.qc)
    fpy = _pct(passed, inspected) if inspected else 1.0

    stations: list[StationKpi] = []
    for st in model.stations.values():
        n = len(st.machines)
        span = sum(m.tracker.elapsed(now) for m in st.machines)
        agg = {s: sum(totals_by_machine[m.id][s] for m in st.machines) for s in MachineState}
        processed = sum(m.processed for m in st.machines)
        run_time = span - agg[MachineState.DOWN] - agg[MachineState.SETUP]
        availability = _pct(run_time, span)
        performance = min(1.0, _pct(st.cfg.ideal_cycle_time() * processed, run_time))
        active = agg[MachineState.PROCESSING] + agg[MachineState.SETUP] + agg[MachineState.DOWN]
        stations.append(
            StationKpi(
                id=st.id,
                name=st.cfg.name,
                servers=n,
                processed=processed,
                utilization=_pct(agg[MachineState.PROCESSING], span),
                availability=availability,
                performance=performance,
                oee=availability * performance * fpy,
                active_pct=_pct(active, span),
                blocked_pct=_pct(agg[MachineState.BLOCKED], span),
                starved_pct=_pct(agg[MachineState.STARVED] + agg[MachineState.WAITING_OPERATOR], span),
                down_pct=_pct(agg[MachineState.DOWN], span),
                passed=st.passed,
                reworked=st.reworked,
                scrapped=st.scrapped,
            )
        )

    buffers = [
        BufferKpi(
            id=b.id,
            name=b.cfg.name,
            capacity=b.cfg.capacity,
            level=b.count,
            mean_level=b.level.mean(now),
            max_level=b.level.max,
            mean_wait=b.wait.mean,
            fill_pct=_pct(b.level.mean(now), b.cfg.capacity) if b.cfg.capacity else None,
        )
        for b in model.buffers.values()
    ]

    bottleneck = detect_bottleneck(model, stations)
    bn_station = next((s for s in stations if s.id == bottleneck.station), None)

    line = LineKpi(
        sim_time=now,
        observed_time=observed,
        created=mon.created,
        completed=mon.completed,
        scrapped=mon.scrapped,
        reworked=mon.reworked,
        wip=int(mon.wip.value),
        mean_wip=mon.wip.mean(now),
        throughput_per_hour=_pct(mon.completed, observed) * 3600,
        mean_cycle_time=mon.cycle_time.mean,
        cycle_time_std=mon.cycle_time.std,
        first_pass_yield=fpy,
        source_blocked_pct=_pct(model.source.blocked_time, observed),
        # The line can only run as well as its constraint, so report the
        # bottleneck station's OEE as the line OEE.
        line_oee=bn_station.oee if bn_station else 0.0,
    )
    return Kpis(line=line, stations=stations, machines=machines, buffers=buffers, bottleneck=bottleneck)
