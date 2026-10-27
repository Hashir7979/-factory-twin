"""Reproducible case study: ``python -m experiments.case_study > ../docs/findings.md``.

Every number in docs/findings.md comes from this script (fixed seeds), so the
write-up can be regenerated after any model change.
"""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from sim import Scenario, apply_overrides, load_scenario
from sim.config import Scenario as ScenarioModel

from .optimizer import Variable, optimize
from .runner import ExperimentResult, ReplicationResult, compare, run_replication, summarize
from .stats import estimate

REPS = 20


def _pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max(1, (os.cpu_count() or 2)), mp_context=multiprocessing.get_context("spawn"))


def replicate(
    pool: ProcessPoolExecutor, sc: Scenario, seeds: list[int], horizon: float, warmup: float
) -> list[ReplicationResult]:
    futures = [pool.submit(run_replication, sc, s, horizon, warmup) for s in seeds]
    return [f.result() for f in futures]


def study(pool: ProcessPoolExecutor, sc: Scenario, overrides: dict[str, Any], seeds: list[int]) -> ExperimentResult:
    """Replications using the scenario's own run length and warm-up."""
    variant = apply_overrides(sc, overrides) if overrides else sc
    h, w = sc.run.horizon, sc.run.warmup
    return summarize(variant, overrides, h, w, replicate(pool, variant, seeds, h, w))


def ci(e: Any, digits: int = 1, scale: float = 1.0) -> str:
    return f"{e.mean * scale:.{digits}f} ± {e.half_width * scale:.{digits}f}"


def signed(mean: float, half: float) -> str:
    """Paired difference; extra decimals so tiny effects do not print as ±0.0."""
    digits = 2 if abs(mean) < 0.1 and half < 0.1 else 1
    return f"{mean:+.{digits}f} ± {half:.{digits}f}"


def single_station(lam: float, mu: float, c: int, seed: int) -> ScenarioModel:
    return ScenarioModel.model_validate({
        "id": "mmc", "name": "M/M/c",
        "variants": [{"id": "A", "name": "A", "weight": 1}],
        "source": {"interarrival": {"type": "exponential", "mean": 1 / lam}, "output": "Q"},
        "buffers": [{"id": "Q", "name": "Q"}],
        "stations": [{"id": "S", "name": "S", "servers": c, "input": "Q", "output": "sink",
                      "process_time": {"type": "exponential", "mean": 1 / mu}}],
        "run": {"horizon": 60_000, "warmup": 2_000, "seed": seed},
    })  # fmt: skip


def erlang_c(c: int, lam: float, mu: float) -> tuple[float, float]:
    a = lam / mu
    rho = a / c
    tail = a**c / (math.factorial(c) * (1 - rho))
    p_wait = tail / (sum(a**k / math.factorial(k) for k in range(c)) + tail)
    wq = p_wait / (c * mu - lam)
    return wq, lam * (wq + 1 / mu)


def validation_section(pool: ProcessPoolExecutor) -> list[str]:
    out = ["## 1. Engine validation against queueing theory", "",
           "Ten replications of 60 000 time units (2 000 warm-up) per model. "
           "The analytic value should fall inside the simulated 95% confidence interval.", "",
           "| Model | Metric | Analytic | Simulated (95% CI) | Error |", "|---|---|---:|---:|---:|"]  # fmt: skip
    from .stats import estimate

    for name, c, lam, mu in (("M/M/1, ρ=0.7", 1, 0.7, 1.0), ("M/M/3, ρ=0.8", 3, 2.4, 1.0)):
        wq_a, l_a = erlang_c(c, lam, mu)
        res = [pool.submit(run_replication, single_station(lam, mu, c, s), s, 60_000, 2_000) for s in range(1, 11)]
        rows = [r.result() for r in res]
        wq = estimate([r.kpis.buffers[0].mean_wait for r in rows])
        L = estimate([r.kpis.line.mean_wip for r in rows])
        for metric, analytic, sim in (("Wq (queue wait)", wq_a, wq), ("L (in system)", l_a, L)):
            err = abs(sim.mean - analytic) / analytic
            inside = "✓" if abs(sim.mean - analytic) <= sim.half_width else "✗"
            out.append(
                f"| {name} | {metric} | {analytic:.3f} | {sim.mean:.3f} ± {sim.half_width:.3f} {inside} | {err:.1%} |"
            )
    out.append("")
    return out


def kpi_table(r: ExperimentResult, unit: str) -> list[str]:
    m = r.metrics
    return [
        "| KPI | Mean ± 95% CI |", "|---|---:|",
        f"| Throughput ({unit}/h) | {ci(m['throughput_per_hour'])} |",
        f"| Cycle time (min) | {ci(m['mean_cycle_time'], scale=1 / 60)} |",
        f"| WIP (parts) | {ci(m['mean_wip'])} |",
        f"| Line OEE (%) | {ci(m['line_oee'], scale=100)} |",
        f"| First-pass yield (%) | {ci(m['first_pass_yield'], scale=100)} |",
    ]  # fmt: skip


def station_table(r: ExperimentResult, sc: Scenario) -> list[str]:
    out = ["| Station | Utilization | Blocked | Starved | Down | OEE |", "|---|---:|---:|---:|---:|---:|"]
    for st in sc.stations:
        s = r.stations[st.id]
        out.append(
            f"| {st.name} | {s['utilization'].mean:.0%} | {s['blocked_pct'].mean:.0%} | {s['starved_pct'].mean:.0%} "
            f"| {s['down_pct'].mean:.1%} | {s['oee'].mean:.0%} |"
        )
    return out


def comparison_rows(
    pool: ProcessPoolExecutor, sc: Scenario, base: ExperimentResult, cases: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    out = ["| Change | Throughput Δ (/h) | Cycle time Δ (min) | WIP Δ | Throughput change significant? | Bottleneck after |",
           "|---|---:|---:|---:|:---:|---|"]  # fmt: skip
    for label, ov in cases:
        alt = study(pool, sc, ov, base.seeds)
        d = {x.metric: x for x in compare(base, alt).differences}
        tp, ct, wip = d["throughput_per_hour"], d["mean_cycle_time"], d["mean_wip"]
        bn = max(alt.bottleneck_frequency, key=alt.bottleneck_frequency.get) if alt.bottleneck_frequency else "–"  # type: ignore[arg-type]
        bn_name = next((s.name for s in sc.stations if s.id == bn), bn)
        out.append(
            f"| {label} | {signed(tp.diff.mean, tp.diff.half_width)} ({(tp.relative or 0):+.1%}) "
            f"| {signed(ct.diff.mean / 60, ct.diff.half_width / 60)} | {signed(wip.diff.mean, wip.diff.half_width)} "
            f"| {'yes' if tp.significant else 'no'} | {bn_name} |"
        )
    return out


async def _optimize(pool: ProcessPoolExecutor, sc: Scenario) -> Any:
    async def evaluate(cand: Scenario, seeds: list[int]) -> list[ReplicationResult]:
        loop = asyncio.get_running_loop()
        futures = [loop.run_in_executor(pool, run_replication, cand, s, sc.run.horizon, sc.run.warmup) for s in seeds]
        return list(await asyncio.gather(*futures))

    variables = [
        Variable(key=f"station.{st.id}.servers", low=max(1, st.servers - 1), high=st.servers + 1) for st in sc.stations
    ]
    variables.append(Variable(key="resources.technicians", low=1, high=3))
    return await optimize(
        sc, variables, sc.economics, trials=30, seeds=[1, 2, 3], evaluate_replications=evaluate, sampler_seed=1
    )


def earthmoving_section(pool: ProcessPoolExecutor, seeds: list[int]) -> list[str]:
    """Fleet-size sweep: the classic 'how many trucks per excavator?' analysis."""
    from collections import Counter

    from .optimizer import cost_per_hour

    em = load_scenario("earthmoving")
    econ = em.economics
    load_t = em.station("load").process_time.mean
    loop_t = load_t + sum(b.mean_transit() for b in em.buffers) + em.station("dump").process_time.mean
    rows = []
    for n in range(2, 11):
        sc = apply_overrides(em, {"source.max_arrivals": n})
        runs = replicate(pool, sc, seeds, sc.run.horizon, sc.run.warmup)
        tp = estimate([r.kpis.line.throughput_per_hour for r in runs])
        util = sum(next(x for x in r.kpis.stations if x.id == "load").utilization for r in runs) / len(runs)
        queue = sum(next(b for b in r.kpis.buffers if b.id == "R_return").mean_queue_delay for r in runs) / len(runs)
        cost = cost_per_hour(sc, econ).total
        label = Counter(r.kpis.bottleneck.label for r in runs).most_common(1)[0][0]
        rows.append((n, tp, util, queue, cost, label))

    def per_m3(row: Any) -> float:
        return float(row[4] / (row[1].mean * em.units_per_output))

    def profit(row: Any) -> float:
        return float(row[1].mean * econ.margin_per_part - row[4])

    best_cost = min(rows, key=per_m3)[0]
    best_profit = max(rows, key=profit)[0]
    by_n = {row[0]: row for row in rows}
    # Profit noise of the best row: the 95% CI half-width of throughput times revenue per load.
    noise = by_n[best_profit][1].half_width * econ.margin_per_part
    gap = profit(by_n[best_profit]) - profit(by_n[best_cost])
    if best_profit == best_cost:
        conclusion = f"**{best_cost} trucks** gives both the lowest cost per m³ and the highest profit."
    elif gap < noise:
        conclusion = (
            f"Cost per m³ is lowest at **{best_cost} trucks**. {best_profit} trucks earns €{gap:.0f}/h more, "
            f"which is inside the replication noise (±€{noise:.0f}/h), so {best_cost} is the robust choice; the "
            f"extra truck mostly buys cover for excavator breakdowns."
        )
    else:
        conclusion = (
            f"Cost per m³ is lowest at {best_cost} trucks, but {best_profit} trucks earns €{gap:.0f}/h more "
            f"(beyond the ±€{noise:.0f}/h noise), so the extra output pays for the truck."
        )
    conclusion += (
        f" Beyond that, every extra truck adds €{econ.fleet_cost_per_hour:.0f}/h and only lengthens the queue "
        "at the excavator."
    )
    table = ["| Trucks | Match factor | Production (m³/h) | Excavator utilization | Truck queue at excavator (min) "
             "| Cost (€/h) | Cost per m³ (€) | Profit (€/h) | Constraint |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---|"]  # fmt: skip
    for row in rows:
        n, tp, util, queue, cost, label = row
        mark = " ◀ lowest cost/m³" if n == best_cost else ""
        mark += " ◀ highest profit" if n == best_profit else ""
        table.append(
            f"| {n} | {n * load_t / loop_t:.2f} | {tp.mean * em.units_per_output:.0f} ± {tp.half_width * em.units_per_output:.0f} "
            f"| {util:.0%} | {queue / 60:.1f} | {cost:.0f} | {per_m3(row):.2f} | {profit(row):.0f}{mark} | {label} |"
        )
    return [
        "## 7. Third scenario: earthmoving fleet sizing (closed network)", "",
        "A fleet of 18 m³ trucks cycles excavator → loaded haul → dump → empty return. Unlike the lines above, "
        "nothing leaves the system: production counts each dump, and cycle time is a truck's round trip. "
        f"Truck €{econ.fleet_cost_per_hour:.0f}/h, excavator €{econ.station_machine_cost_per_hour['load']:.0f}/h, "
        f"dozer €{econ.station_machine_cost_per_hour['dump']:.0f}/h, revenue €{econ.margin_per_part:.0f} per load. "
        f"{len(seeds)} replications per fleet size.", "",
        *table, "",
        "The **match factor** (trucks × loading time ÷ uncongested truck cycle) is the textbook planning estimate: "
        "below 1 the excavator waits for trucks, above 1 trucks wait for the excavator. The simulation adds what "
        "the formula misses: random haul times and excavator breakdowns make trucks bunch, so the excavator "
        "still idles at a match factor near 1 and output keeps rising until about one truck beyond it.", "",
        conclusion, "",
    ]  # fmt: skip


def main() -> int:
    started = time.time()
    lines: list[str] = []
    seeds = list(range(1, REPS + 1))
    with _pool() as pool:
        battery = load_scenario("battery_line")
        lines += [
            "# Findings: EV battery-module line case study", "",
            "_Generated by `python -m experiments.case_study` (backend/). All runs use fixed seeds, so the numbers "
            "are reproducible. Times are simulated._", "",
            f"Setup unless noted: {REPS} replications per configuration with each scenario's run length and warm-up, "
            "95% confidence intervals (Student t). What-if comparisons use common random numbers (identical seeds "
            "for both arms) and report paired differences.", "",
        ]  # fmt: skip
        lines += validation_section(pool)

        h, wu = battery.run.horizon, battery.run.warmup
        base = study(pool, battery, {}, seeds)
        bn = max(base.bottleneck_frequency, key=base.bottleneck_frequency.get)  # type: ignore[arg-type]
        w = base.warmup_analysis
        lines += ["## 2. Warm-up period", "",
                  "The line starts empty and the supply releases cells faster than welding can process them, so "
                  "buffers fill for hours before the system reaches steady state. MSER truncation on the "
                  f"replication-averaged cycle time puts the end of that transient at **{(w.suggested_warmup or 0) / 3600:.2f} h**. "
                  "The original model used a 1 h warm-up, which biased every KPI towards the empty-line start; "
                  f"the scenario now uses {wu / 3600:.0f} h of warm-up and measures {(h - wu) / 3600:.0f} h (one shift).", ""]  # fmt: skip

        lines += ["## 3. Baseline performance", "", *kpi_table(base, "modules"), "", *station_table(base, battery), "",
                  f"**Bottleneck:** {battery.station(bn).name} in {base.bottleneck_frequency[bn]:.0%} of replications. "
                  "Upstream stations are blocked and downstream stations starved, the signature of a true constraint.", ""]  # fmt: skip

        cases = [
            ("Add a 2nd laser welder", {"station.weld.servers": 2}),
            ("Double weld infeed buffer (6 → 12)", {"buffer.B2.capacity": 12}),
            ("Add a 2nd maintenance technician", {"resources.technicians": 2}),
            ("2nd welder + 3rd stacking cell", {"station.weld.servers": 2, "station.stack.servers": 3}),
        ]
        lines += ["## 4. What-if comparisons (vs baseline, paired)", "", *comparison_rows(pool, battery, base, cases), "",
                  "Reading:", "",
                  "- **A bigger buffer in front of the bottleneck does not raise output.** Welding is never starved, "
                  "so the extra slots only hold more parts: WIP and lead time rise and throughput stays flat "
                  "(Little's Law: L = λW).",
                  "- **A second technician does nothing** because simultaneous breakdowns are rare.",
                  "- **A second welder lifts output** until stacking becomes the constraint. Relieving both stations "
                  "moves the bottleneck upstream to cell prep: the classic shifting-bottleneck pattern.", ""]  # fmt: skip

        opt = asyncio.run(_optimize(pool, battery))
        changed = {k: v for k, v in opt.best.params.items() if opt.baseline.params.get(k) != v}
        d = opt.confirmation.profit_difference
        # Contribution of each recommended change: revert it alone (paired, confirmation seeds).
        contrib = ["| Change | Profit effect of this change alone (€/h) | Significant? |", "|---|---:|:---:|"]
        if changed:
            best_sc = apply_overrides(battery, dict(opt.best.params))
            cs = opt.confirmation.seeds
            best_runs = replicate(pool, best_sc, cs, h, wu)
            best_profit = [r.kpis.line.throughput_per_hour * opt.economics.margin_per_part for r in best_runs]
            from .optimizer import cost_per_hour
            from .stats import paired_difference

            best_cost = cost_per_hour(best_sc, opt.economics).total
            for key, value in changed.items():
                reverted = dict(opt.best.params)
                reverted[key] = opt.baseline.params[key]
                rev_sc = apply_overrides(battery, reverted)
                rev_runs = replicate(pool, rev_sc, cs, h, wu)
                rev_cost = cost_per_hour(rev_sc, opt.economics).total
                diff = paired_difference(
                    key,
                    [r.kpis.line.throughput_per_hour * opt.economics.margin_per_part - rev_cost for r in rev_runs],
                    [p - best_cost for p in best_profit],
                )
                contrib.append(
                    f"| `{key}` {opt.baseline.params[key]} → {value} | {diff.diff.mean:+.0f} ± {diff.diff.half_width:.0f} | "
                    f"{'yes' if diff.significant else 'no'} |"
                )
        lines += ["## 5. Optimization (Optuna TPE + confirmation)", "",
                  "Decision variables: machines per station (current ± 1) and technicians (1–3). Objective: hourly "
                  f"profit with margin €{opt.economics.margin_per_part:.0f}/module, €{opt.economics.machine_cost_per_hour:.0f}/h "
                  f"per machine, €{opt.economics.technician_cost_per_hour:.0f}/h per technician. 30 trials × 3 replications, "
                  f"then the top candidates were re-run (and trimmed) on {len(opt.confirmation.seeds)} fresh seeds "
                  "to remove the optimistic bias of picking the best of many noisy estimates.", "",
                  "| | Baseline | Best |", "|---|---:|---:|",
                  f"| Profit (€/h) | {ci(opt.baseline.profit, 0)} | {ci(opt.best.profit, 0)} |",
                  f"| Throughput (/h) | {ci(opt.baseline.throughput)} | {ci(opt.best.throughput)} |",
                  f"| Cost (€/h) | {opt.baseline.cost_per_hour:.0f} | {opt.best.cost_per_hour:.0f} |", "",
                  "**Changes:** " + (", ".join(f"`{k}` {opt.baseline.params[k]} → {v}" for k, v in changed.items()) or "none") + ".", "",
                  (f"**Confirmed gain:** {d.diff.mean:+.0f} ± {d.diff.half_width:.0f} €/h "
                   f"({'statistically significant' if opt.confirmation.significant else 'not significant'})."
                   if d else "**Result:** no candidate beat the baseline on confirmation."), "",
                  "Contribution of each change (best configuration vs the same configuration with only that change "
                  "undone, paired on the confirmation seeds):", "", *contrib, "",
                  "The welder and the stacking cell carry the gain. The extra technician is marginal: positive on "
                  "average because the optimized line has more machines that can fail at the same time, but not "
                  "statistically distinguishable from zero. A manager could reasonably skip it.", ""]  # fmt: skip

        fc = load_scenario("fulfillment_center")
        fbase = study(pool, fc, {}, seeds)
        fbn = max(fbase.bottleneck_frequency, key=fbase.bottleneck_frequency.get)  # type: ignore[arg-type]
        fw = fbase.warmup_analysis
        lines += ["## 6. Second scenario: e-commerce fulfillment center", "",
                  "Same engine and schema, different industry and layout (U-shaped flow, L-shaped conveyor).", "",
                  *kpi_table(fbase, "orders"), "",
                  f"**Bottleneck:** {fc.station(fbn).name} ({fbase.bottleneck_frequency[fbn]:.0%} of replications). "
                  f"MSER warm-up estimate: {(fw.suggested_warmup or 0) / 3600:.2f} h (configured {fc.run.warmup / 3600:.0f} h).", "",
                  *comparison_rows(pool, fc, fbase, [("Add a 4th packing bench", {"station.pack.servers": 4}),
                                                     ("Add a 5th picker", {"station.pick.servers": 5})]), "",
                  "Reading: the center ships about as fast as orders arrive (180/h), so an extra packing bench adds "
                  "little throughput. It does cut order lead time and WIP sharply by removing packing queues; "
                  "an extra picker changes nothing because picking is not the constraint.", ""]  # fmt: skip

        lines += earthmoving_section(pool, seeds)

    lines.append(f"_Runtime: {time.time() - started:.0f} s on {os.cpu_count()} CPU cores._")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
