"""Scenario schema.

A scenario fully describes a production system: routing, capacities, stochastic
timings, failures, resources and 3D layout. The same JSON file drives both the
SimPy model and the Three.js scene, so the twin and the visualization can never
drift apart.

All times are in seconds.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SINK_ID = "sink"
SCRAP_ID = "scrap"
_RESERVED_IDS = {SINK_ID, SCRAP_ID}

_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,39}$"
NodeId = Annotated[str, Field(pattern=_ID_PATTERN)]
Point = tuple[float, float]  # (x, z) on the factory floor, metres


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- distributions


class Constant(_Strict):
    type: Literal["constant"]
    value: float = Field(ge=0)

    @property
    def mean(self) -> float:
        return self.value


class Exponential(_Strict):
    type: Literal["exponential"]
    mean: float = Field(gt=0)


class Uniform(_Strict):
    type: Literal["uniform"]
    low: float = Field(ge=0)
    high: float = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> Uniform:
        if self.high <= self.low:
            raise ValueError("uniform: high must be greater than low")
        return self

    @property
    def mean(self) -> float:
        return (self.low + self.high) / 2


class Triangular(_Strict):
    type: Literal["triangular"]
    low: float = Field(ge=0)
    mode: float = Field(ge=0)
    high: float = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> Triangular:
        if not (self.low <= self.mode <= self.high) or self.low == self.high:
            raise ValueError("triangular: requires low <= mode <= high and low < high")
        return self

    @property
    def mean(self) -> float:
        return (self.low + self.mode + self.high) / 3


class Normal(_Strict):
    """Normal distribution truncated (by resampling) to [min, inf)."""

    type: Literal["normal"]
    mean: float = Field(gt=0)
    std: float = Field(ge=0)
    min: float = Field(default=0.0, ge=0)


class Lognormal(_Strict):
    """Lognormal parameterised by its own mean/std (not the underlying normal's)."""

    type: Literal["lognormal"]
    mean: float = Field(gt=0)
    std: float = Field(gt=0)


class Weibull(_Strict):
    type: Literal["weibull"]
    shape: float = Field(gt=0)
    scale: float = Field(gt=0)

    @property
    def mean(self) -> float:
        from math import gamma

        return self.scale * gamma(1 + 1 / self.shape)


Distribution = Annotated[
    Constant | Exponential | Uniform | Triangular | Normal | Lognormal | Weibull,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- nodes


class Variant(_Strict):
    id: NodeId
    name: str = Field(max_length=60)
    weight: float = Field(gt=0)
    color: str = Field(default="#4f8cff", pattern=r"^#[0-9a-fA-F]{6}$")


class Source(_Strict):
    id: NodeId = "source"
    name: str = Field(default="Source", max_length=60)
    interarrival: Distribution
    output: NodeId
    max_arrivals: int | None = Field(default=None, ge=1)
    pos: Point = (0.0, 0.0)


class Buffer(_Strict):
    id: NodeId
    name: str = Field(max_length=60)
    # None = unbounded (useful for analytic validation; unrealistic on a real floor)
    capacity: int | None = Field(default=None, ge=1, le=10_000)
    # "conveyor" and "road" behave identically (FIFO travel, no overtaking); they
    # differ only in how the 3D view draws them.
    kind: Literal["queue", "conveyor", "road"] = "queue"
    # Minimum time a part spends travelling before it can be picked.
    transit_time: float = Field(default=0.0, ge=0)
    # Random travel time (e.g. truck haul times); overrides transit_time when set.
    transit: Distribution | None = None
    pos: Point = (0.0, 0.0)
    # Centreline for the 3D view; ignored for queues.
    path: list[Point] | None = None

    @property
    def is_travel(self) -> bool:
        return self.kind in ("conveyor", "road")

    @model_validator(mode="after")
    def _check(self) -> Buffer:
        if self.is_travel:
            if self.transit_time <= 0 and self.transit is None:
                raise ValueError(f"buffer {self.id}: {self.kind} requires transit_time > 0 or a transit distribution")
            if self.capacity is None:
                raise ValueError(f"buffer {self.id}: {self.kind} requires a finite capacity")
            if not self.path or len(self.path) < 2:
                raise ValueError(f"buffer {self.id}: {self.kind} requires a path of >= 2 points")
        elif self.transit is not None or self.transit_time > 0:
            raise ValueError(f"buffer {self.id}: only conveyors and roads have travel times")
        return self

    def mean_transit(self) -> float:
        return float(self.transit.mean) if self.transit is not None else self.transit_time


class Failure(_Strict):
    """Time-to-failure is measured in calendar (simulation) time."""

    ttf: Distribution
    ttr: Distribution

    @model_validator(mode="after")
    def _check(self) -> Failure:
        if isinstance(self.ttf, Constant) and self.ttf.value <= 0:
            raise ValueError("failure: constant ttf must be > 0")
        return self


class QualityCheck(_Strict):
    pass_prob: float = Field(ge=0, le=1)
    rework_prob: float = Field(default=0.0, ge=0, le=1)
    rework_output: NodeId | None = None

    @model_validator(mode="after")
    def _check(self) -> QualityCheck:
        if self.pass_prob + self.rework_prob > 1 + 1e-9:
            raise ValueError("qc: pass_prob + rework_prob must be <= 1 (remainder is scrap)")
        if self.rework_prob > 0 and self.rework_output is None:
            raise ValueError("qc: rework_output is required when rework_prob > 0")
        return self

    @property
    def scrap_prob(self) -> float:
        return max(0.0, 1.0 - self.pass_prob - self.rework_prob)


class Station(_Strict):
    id: NodeId
    name: str = Field(max_length=60)
    kind: Literal["machine", "qc"] = "machine"
    servers: int = Field(default=1, ge=1, le=20)
    input: NodeId
    output: NodeId
    process_time: Distribution
    process_time_by_variant: dict[str, Distribution] = Field(default_factory=dict)
    # Applied when a machine switches to a different product variant.
    setup_time: Distribution | None = None
    failure: Failure | None = None
    needs_operator: bool = False
    qc: QualityCheck | None = None
    # 3D appearance only.
    model: Literal["machine", "excavator", "dump"] = "machine"
    pos: Point = (0.0, 0.0)

    @model_validator(mode="after")
    def _check(self) -> Station:
        if self.kind == "qc" and self.qc is None:
            raise ValueError(f"station {self.id}: kind 'qc' requires a qc block")
        if self.kind != "qc" and self.qc is not None:
            raise ValueError(f"station {self.id}: qc block is only valid for kind 'qc'")
        if self.input == self.output:
            raise ValueError(f"station {self.id}: input and output must differ")
        return self

    def ideal_cycle_time(self) -> float:
        """Mean nominal process time, used for OEE performance."""
        return float(self.process_time.mean)


class Resources(_Strict):
    # None = unlimited technicians (repairs start immediately).
    technicians: int | None = Field(default=None, ge=1, le=100)
    operators: int = Field(default=0, ge=0, le=200)


class Sink(_Strict):
    pos: Point = (0.0, 0.0)


class RunDefaults(_Strict):
    horizon: float = Field(default=8 * 3600, gt=0, le=60 * 24 * 3600)
    warmup: float = Field(default=3600, ge=0)
    seed: int = Field(default=1, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _check(self) -> RunDefaults:
        if self.warmup >= self.horizon:
            raise ValueError("run: warmup must be shorter than horizon")
        return self


class Economics(_Strict):
    """Cost model for the optimizer (per hour). Scenarios ship their own defaults."""

    margin_per_part: float = Field(default=180.0, ge=0, le=1e6)  # revenue per unit of output
    machine_cost_per_hour: float = Field(default=45.0, ge=0, le=1e5)
    station_machine_cost_per_hour: dict[str, float] = Field(default_factory=dict, max_length=100)
    buffer_slot_cost_per_hour: float = Field(default=0.5, ge=0, le=1e4)
    technician_cost_per_hour: float = Field(default=48.0, ge=0, le=1e5)
    operator_cost_per_hour: float = Field(default=38.0, ge=0, le=1e5)
    # Per circulating unit in a closed network (e.g. a truck including its driver).
    fleet_cost_per_hour: float = Field(default=0.0, ge=0, le=1e5)


class Scenario(_Strict):
    id: NodeId
    name: str = Field(max_length=80)
    description: str = Field(default="", max_length=2000)
    variants: list[Variant] = Field(min_length=1, max_length=20)
    source: Source
    buffers: list[Buffer] = Field(min_length=1, max_length=100)
    stations: list[Station] = Field(min_length=1, max_length=100)
    resources: Resources = Resources()
    sink: Sink = Sink()
    run: RunDefaults = RunDefaults()
    economics: Economics = Economics()
    # What the moving entities are, for the 3D view and UI wording.
    entity: Literal["part", "truck"] = "part"
    # Closed networks (no sink: a fixed fleet circulates, e.g. trucks) count
    # production when an entity completes this station.
    output_station: NodeId | None = None
    output_unit: str = Field(default="parts", max_length=20)
    units_per_output: float = Field(default=1.0, gt=0, le=1e6)

    @property
    def is_closed(self) -> bool:
        return not any(st.output == SINK_ID for st in self.stations)

    @model_validator(mode="after")
    def _check_graph(self) -> Scenario:
        ids: list[str] = [self.source.id, *(b.id for b in self.buffers), *(s.id for s in self.stations)]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate ids: {sorted(dupes)}")
        if _RESERVED_IDS & set(ids):
            raise ValueError(f"ids {sorted(_RESERVED_IDS)} are reserved")

        buffer_ids = {b.id for b in self.buffers}
        variant_ids = {v.id for v in self.variants}
        if self.source.output not in buffer_ids:
            raise ValueError(f"source output '{self.source.output}' is not a buffer")

        consumers: dict[str, list[str]] = {}
        for st in self.stations:
            if st.input not in buffer_ids:
                raise ValueError(f"station {st.id}: input '{st.input}' is not a buffer")
            if st.output not in buffer_ids | {SINK_ID}:
                raise ValueError(f"station {st.id}: output '{st.output}' is not a buffer or 'sink'")
            if st.qc and st.qc.rework_output and st.qc.rework_output not in buffer_ids:
                raise ValueError(f"station {st.id}: rework_output '{st.qc.rework_output}' is not a buffer")
            unknown = set(st.process_time_by_variant) - variant_ids
            if unknown:
                raise ValueError(f"station {st.id}: unknown variants {sorted(unknown)}")
            if st.needs_operator and self.resources.operators < 1:
                raise ValueError(f"station {st.id}: needs_operator but resources.operators is 0")
            consumers.setdefault(st.input, []).append(st.id)

        # Every buffer must be drained by exactly one station; otherwise parts pile
        # up forever (a configuration bug that would silently corrupt KPIs).
        for b in self.buffers:
            n = len(consumers.get(b.id, []))
            if n != 1:
                raise ValueError(f"buffer {b.id}: must feed exactly one station (found {n})")
        if self.is_closed:
            fleet = self.source.max_arrivals
            if fleet is None:
                raise ValueError(
                    "a closed network (no station outputs to 'sink') needs source.max_arrivals (fleet size)"
                )
            if self.output_station is None or self.output_station not in {s.id for s in self.stations}:
                raise ValueError("a closed network needs output_station set to an existing station")
            # If every buffer could fill up the fleet could jam itself; requiring each
            # buffer to hold the whole fleet makes deadlock impossible.
            small = [b.id for b in self.buffers if b.capacity is not None and b.capacity < fleet]
            if small:
                raise ValueError(f"closed network: buffers {small} must hold the whole fleet ({fleet})")
        elif self.output_station is not None:
            raise ValueError("output_station is only used by closed networks (no station outputs to 'sink')")
        return self

    # Convenience lookups -----------------------------------------------------------
    def buffer(self, buffer_id: str) -> Buffer:
        return next(b for b in self.buffers if b.id == buffer_id)

    def station(self, station_id: str) -> Station:
        return next(s for s in self.stations if s.id == station_id)


# --------------------------------------------------------------------------- overrides

# Only these knobs may be changed through the API / optimizer. Keeping the list
# explicit stops clients from rewriting arbitrary parts of a scenario.
OVERRIDABLE = {
    "buffer.capacity": (1, 10_000),
    "station.servers": (1, 20),
    "resources.technicians": (1, 100),
    "resources.operators": (0, 200),
    "source.interarrival_scale": (0.1, 10.0),
    "source.max_arrivals": (1, 500),
}


class OverrideError(ValueError):
    pass


def apply_overrides(scenario: Scenario, overrides: dict[str, Any]) -> Scenario:
    """Return a new validated scenario with whitelisted overrides applied.

    Keys: ``buffer.<id>.capacity``, ``station.<id>.servers``,
    ``resources.technicians``, ``resources.operators``,
    ``source.interarrival_scale`` (multiplies every time parameter of the
    inter-arrival distribution, i.e. scales demand), ``source.max_arrivals``
    (fleet size in closed networks).
    """
    data = copy.deepcopy(scenario.model_dump(mode="json"))
    for key, raw in overrides.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[0] in ("buffer", "station"):
            kind, node_id, field = parts
            rule = f"{kind}.{field}"
            collection = data["buffers" if kind == "buffer" else "stations"]
            node = next((n for n in collection if n["id"] == node_id), None)
            if node is None:
                raise OverrideError(f"unknown {kind} '{node_id}'")
            target, attr = node, field
        elif key in ("resources.technicians", "resources.operators"):
            rule, target, attr = key, data["resources"], parts[1]
        elif key == "source.interarrival_scale":
            rule, target, attr = key, None, ""
        elif key == "source.max_arrivals":
            rule, target, attr = key, data["source"], "max_arrivals"
        else:
            raise OverrideError(f"override '{key}' is not allowed")
        if rule not in OVERRIDABLE:
            raise OverrideError(f"override '{key}' is not allowed")

        lo, hi = OVERRIDABLE[rule]
        is_float = rule == "source.interarrival_scale"
        try:
            value = float(raw) if is_float else int(raw)
        except (TypeError, ValueError) as exc:
            raise OverrideError(f"override '{key}': value must be numeric") from exc
        if not is_float and float(raw) != value:
            raise OverrideError(f"override '{key}': value must be an integer")
        if not lo <= value <= hi:
            raise OverrideError(f"override '{key}': value must be between {lo} and {hi}")

        if target is None:
            dist = data["source"]["interarrival"]
            for k in ("value", "mean", "std", "low", "mode", "high", "scale"):
                if k in dist:
                    dist[k] = dist[k] * value
        else:
            target[attr] = value
    try:
        return Scenario.model_validate(data)
    except ValueError as exc:
        raise OverrideError(str(exc)) from exc
