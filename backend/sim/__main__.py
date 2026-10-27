"""Command line: ``python -m sim run battery_line --seed 7``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .kpi import compute_kpis
from .model import FactoryModel
from .scenarios import load_scenario, load_scenario_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sim")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one replication and print KPIs")
    run.add_argument("scenario", help="bundled scenario id or path to a .json file")
    run.add_argument("--seed", type=int)
    run.add_argument("--horizon", type=float)
    run.add_argument("--warmup", type=float)
    run.add_argument("--json", action="store_true", help="print the full KPI document as JSON")
    args = parser.parse_args(argv)

    path = Path(args.scenario)
    scenario = load_scenario_file(path) if path.suffix == ".json" else load_scenario(args.scenario)
    model = FactoryModel(scenario, seed=args.seed)
    model.run(horizon=args.horizon, warmup=args.warmup)
    kpis = compute_kpis(model)

    if args.json:
        print(kpis.model_dump_json(indent=2))
        return 0
    line = kpis.line
    print(f"{scenario.name}  (seed {model.seed}, observed {line.observed_time / 3600:.1f} h)")
    print(f"  throughput  {line.throughput_per_hour:6.1f} /h   completed {line.completed}  scrapped {line.scrapped}")
    print(
        f"  cycle time  {line.mean_cycle_time / 60:6.1f} min  WIP {line.mean_wip:.1f}  FPY {line.first_pass_yield:.1%}"
    )
    print(f"  line OEE    {line.line_oee:6.1%}")
    print("  station                   util   avail  perf   OEE   active blocked starved down")
    for s in kpis.stations:
        print(
            f"  {s.name:<24} {s.utilization:5.1%} {s.availability:6.1%} {s.performance:5.1%} {s.oee:5.1%}"
            f"  {s.active_pct:5.1%} {s.blocked_pct:6.1%} {s.starved_pct:6.1%} {s.down_pct:5.1%}"
        )
    print(f"  bottleneck: {kpis.bottleneck.station} ({kpis.bottleneck.confidence}) — {kpis.bottleneck.explanation}")
    print(json.dumps({b.id: round(b.mean_level, 2) for b in kpis.buffers}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
