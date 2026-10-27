"""Request and message schemas for the HTTP and WebSocket API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from experiments.optimizer import Economics, Variable

OverrideValue = Annotated[float, Field(allow_inf_nan=False)]
Overrides = Annotated[dict[Annotated[str, Field(max_length=80)], OverrideValue], Field(max_length=50)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- WebSocket commands


class PlayCmd(_Strict):
    type: Literal["play"]


class PauseCmd(_Strict):
    type: Literal["pause"]


class SpeedCmd(_Strict):
    type: Literal["speed"]
    value: float = Field(gt=0, allow_inf_nan=False)


class ResetCmd(_Strict):
    type: Literal["reset"]
    overrides: Overrides = Field(default_factory=dict)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class InjectFailureCmd(_Strict):
    type: Literal["inject_failure"]
    machine: str = Field(max_length=64)
    repair_time: float | None = Field(default=None, gt=0, le=24 * 3600)


class SnapshotCmd(_Strict):
    type: Literal["snapshot"]


Command = Annotated[
    PlayCmd | PauseCmd | SpeedCmd | ResetCmd | InjectFailureCmd | SnapshotCmd,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- REST bodies


class RunRequest(_Strict):
    scenario_id: str = Field(max_length=40)
    overrides: Overrides = Field(default_factory=dict)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    horizon: float | None = Field(default=None, gt=0)
    warmup: float | None = Field(default=None, ge=0)


class ExperimentRequest(_Strict):
    scenario_id: str = Field(max_length=40)
    overrides: Overrides = Field(default_factory=dict)
    replications: int = Field(default=10, ge=2)
    base_seed: int = Field(default=1, ge=0, le=2**31 - 1_000)
    horizon: float | None = Field(default=None, gt=0)
    warmup: float | None = Field(default=None, ge=0)


class ComparisonRequest(_Strict):
    scenario_id: str = Field(max_length=40)
    a_label: str = Field(default="Baseline", max_length=40)
    b_label: str = Field(default="Alternative", max_length=40)
    a_overrides: Overrides = Field(default_factory=dict)
    b_overrides: Overrides = Field(default_factory=dict)
    replications: int = Field(default=10, ge=2)
    base_seed: int = Field(default=1, ge=0, le=2**31 - 1_000)
    horizon: float | None = Field(default=None, gt=0)
    warmup: float | None = Field(default=None, ge=0)


class OptimizationRequest(_Strict):
    scenario_id: str = Field(max_length=40)
    variables: list[Variable] = Field(min_length=1, max_length=12)
    # Omitted → the scenario's own cost model.
    economics: Economics | None = None
    trials: int = Field(default=30, ge=3)
    replications: int = Field(default=3, ge=1, le=10)
    base_seed: int = Field(default=1, ge=0, le=2**31 - 1_000)
    horizon: float | None = Field(default=None, gt=0)
    warmup: float | None = Field(default=None, ge=0)


class JobAccepted(BaseModel):
    job_id: str
    status: str


class ScenarioSummary(BaseModel):
    id: str
    name: str
    description: str
    stations: int
    buffers: int
