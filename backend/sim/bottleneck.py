"""Bottleneck detection.

Uses the *active-period* principle (Roser et al.): a station constrains the line
while it is processing, setting up or broken down; it is not constraining while
starved or blocked. The station with the highest share of active time (averaged
over its parallel machines) is the bottleneck.

As supporting evidence the report includes how blocked the upstream station is
and how starved the downstream station is: around a true bottleneck parts pile
up in front of it and the stations behind it wait for work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from .config import SINK_ID

if TYPE_CHECKING:
    from .kpi import StationKpi
    from .model import FactoryModel

# Gap (in active-percentage points) between the top two stations below which
# the result is reported as "shifting" rather than a single clear bottleneck.
MARGINAL_GAP = 0.03
# Closed networks: if the busiest station waits for work at least this share of
# the time, the circulating fleet (not a station) limits output. Between the two
# thresholds the system is balanced around the best fleet size.
FLEET_LIMITED_STARVED = 0.15
FLEET_BALANCED_STARVED = 0.05


class BottleneckRank(BaseModel):
    station: str
    name: str
    active_pct: float


class BottleneckReport(BaseModel):
    # A station id, "fleet" (closed networks with too few circulating units) or None.
    station: str | None
    label: str | None = None  # human-readable name of the constraint
    confidence: Literal["clear", "shifting", "none"]
    ranking: list[BottleneckRank]
    upstream_blocked_pct: float | None
    downstream_starved_pct: float | None
    explanation: str


def main_route(model: FactoryModel) -> list[str]:
    """Station ids along the main flow from source to sink (rework branches excluded)."""
    by_input = {st.cfg.input: st for st in model.stations.values()}
    route: list[str] = []
    buffer_id = model.scenario.source.output
    seen: set[str] = set()
    while buffer_id in by_input and buffer_id not in seen:
        seen.add(buffer_id)
        st = by_input[buffer_id]
        route.append(st.id)
        if st.cfg.output == SINK_ID:
            break
        buffer_id = st.cfg.output
    return route


def detect_bottleneck(model: FactoryModel, stations: list[StationKpi]) -> BottleneckReport:
    ranking = sorted(
        (BottleneckRank(station=s.id, name=s.name, active_pct=s.active_pct) for s in stations),
        key=lambda r: r.active_pct,
        reverse=True,
    )
    if not ranking or ranking[0].active_pct <= 0:
        return BottleneckReport(
            station=None,
            confidence="none",
            ranking=ranking,
            upstream_blocked_pct=None,
            downstream_starved_pct=None,
            explanation="Not enough simulated time to identify a bottleneck yet.",
        )

    top = ranking[0]
    gap = top.active_pct - ranking[1].active_pct if len(ranking) > 1 else 1.0
    confidence: Literal["clear", "shifting"] = "clear" if gap >= MARGINAL_GAP else "shifting"

    by_id = {s.id: s for s in stations}
    route = main_route(model)
    up = down = None
    if top.station in route:
        i = route.index(top.station)
        if i > 0:
            up = by_id[route[i - 1]].blocked_pct
        if i + 1 < len(route):
            down = by_id[route[i + 1]].starved_pct

    if model.scenario.is_closed:
        return _closed_network_report(model, by_id[top.station], ranking, up, down)

    if confidence == "clear":
        explanation = (
            f"{top.name} is active {top.active_pct:.0%} of the time, "
            f"{gap:.0%} more than the next station ({ranking[1].name if len(ranking) > 1 else '-'})."
        )
    else:
        explanation = (
            f"{top.name} and {ranking[1].name} are within {gap:.1%} of each other; the constraint shifts between them."
        )
    return BottleneckReport(
        station=top.station,
        label=top.name,
        confidence=confidence,
        ranking=ranking,
        upstream_blocked_pct=up,
        downstream_starved_pct=down,
        explanation=explanation,
    )


def _closed_network_report(
    model: FactoryModel, top: StationKpi, ranking: list[BottleneckRank], up: float | None, down: float | None
) -> BottleneckReport:
    """In a loop, starvation at the busiest station means too few circulating units."""
    plural = f"{model.scenario.entity}s"
    starved = top.starved_pct
    queue = model.buffers[model.scenario.station(top.id).input].queue_delay.mean
    common = {"ranking": ranking, "upstream_blocked_pct": up, "downstream_starved_pct": down}
    if starved >= FLEET_LIMITED_STARVED:
        return BottleneckReport(
            station="fleet",
            label=f"{model.scenario.entity.capitalize()} fleet",
            confidence="clear",
            explanation=(
                f"Too few {plural}: {top.name}, the busiest station, waits for {plural} {starved:.0%} of the time. "
                f"Adding {plural} raises output."
            ),
            **common,
        )
    if starved >= FLEET_BALANCED_STARVED:
        return BottleneckReport(
            station=top.id,
            label=top.name,
            confidence="shifting",
            explanation=(
                f"Balanced: {top.name} waits for {plural} {starved:.0%} of the time while {plural} queue "
                f"{queue / 60:.1f} min on average at it. The fleet size is close to the best match."
            ),
            **common,
        )
    return BottleneckReport(
        station=top.id,
        label=top.name,
        confidence="clear",
        explanation=(
            f"{top.name} is active {top.active_pct:.0%} of the time and {plural} queue {queue / 60:.1f} min on "
            f"average to reach it. More {plural} would add cost, not output."
        ),
        **common,
    )
