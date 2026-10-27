"""Loading of the bundled scenario files."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .config import Scenario

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def load_scenario_file(path: Path) -> Scenario:
    return Scenario.model_validate(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=32)
def load_scenario(scenario_id: str) -> Scenario:
    """Load a bundled scenario by id. Raises KeyError if it does not exist."""
    # Look the id up in the directory listing instead of joining it onto a path,
    # so a crafted id can never escape SCENARIO_DIR.
    files = {p.stem: p for p in SCENARIO_DIR.glob("*.json")}
    if scenario_id not in files:
        raise KeyError(scenario_id)
    return load_scenario_file(files[scenario_id])


def list_scenarios() -> list[Scenario]:
    return [load_scenario(p.stem) for p in sorted(SCENARIO_DIR.glob("*.json"))]
