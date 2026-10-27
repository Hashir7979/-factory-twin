import uPlot from "uplot";
import { api } from "../net/api";
import type { Economics, OptVariable, OptimizationResult, Overrides, Scenario } from "../types";
import { clear, fmtNum, h } from "./dom";
import { JobControl } from "./job";
import { overrideLabel } from "./labels";
import { toast } from "./toast";

interface Context {
  scenario: () => Scenario | null;
  apply: (overrides: Overrides) => void;
}

interface Defaults {
  variables: OptVariable[];
  economics: Economics;
  max_trials: number;
}

const ECON_FIELDS: [keyof Economics, string][] = [
  ["margin_per_part", "Margin per good part (€)"],
  ["machine_cost_per_hour", "Machine cost (€/h each)"],
  ["buffer_slot_cost_per_hour", "Buffer slot cost (€/h)"],
  ["technician_cost_per_hour", "Technician cost (€/h)"],
  ["operator_cost_per_hour", "Operator cost (€/h)"],
];

const MAX_VARIABLES = 12; // backend limit (OptimizationRequest.variables)

const money = (x: number) => `€${Math.round(x).toLocaleString()}`;

/**
 * Simulation-based optimization: Optuna searches buffer sizes, machine counts
 * and staffing for the highest hourly profit; each candidate is simulated with
 * the same seeds so candidates are compared fairly.
 */
export class OptimizerPanel {
  private readonly vars: HTMLElement;
  private readonly econ: HTMLElement;
  private readonly results: HTMLElement;
  private readonly trials: HTMLInputElement;
  private readonly reps: HTMLInputElement;
  private readonly hours: HTMLInputElement;
  private readonly job: JobControl<OptimizationResult>;
  private rows: { include: HTMLInputElement; low: HTMLInputElement; high: HTMLInputElement; key: string }[] = [];
  private econInputs = new Map<keyof Economics, HTMLInputElement>();
  private loadedFor = "";
  private maxTrials = 60;
  private plot: uPlot | null = null;

  constructor(host: HTMLElement, private readonly ctx: Context) {
    this.vars = h("div");
    this.econ = h("div");
    this.trials = h("input", { id: "opt-trials", type: "number", min: 3, max: 60, step: 1, value: 25, inputmode: "numeric" }) as HTMLInputElement;
    this.reps = h("input", { id: "opt-reps", type: "number", min: 1, max: 10, step: 1, value: 3, inputmode: "numeric" }) as HTMLInputElement;
    this.hours = h("input", { id: "opt-hours", type: "number", min: 1, max: 720, step: 0.5, value: 9, inputmode: "decimal" }) as HTMLInputElement;
    this.job = new JobControl("Run optimization", () => this.start(), (r) => this.show(r));
    this.results = h("div", { "aria-live": "polite" });
    host.append(
      h(
        "section",
        { class: "card", "aria-labelledby": "h-opt" },
        h("h2", { id: "h-opt", class: "card-title" }, "Optimize line configuration"),
        h("p", { class: "muted small" }, "Searches the ranges below for the highest hourly profit (good parts × margin − machine, buffer and staff costs) using Bayesian optimization (Optuna TPE)."),
        h("h3", { class: "subhead" }, "Decision variables"),
        this.vars,
        h("h3", { class: "subhead" }, "Economics"),
        this.econ,
        h("h3", { class: "subhead" }, "Search budget"),
        h("div", { class: "field" }, h("label", { for: "opt-trials" }, "Trials"), this.trials),
        h("div", { class: "field" }, h("label", { for: "opt-reps" }, "Replications per trial"), this.reps),
        h("div", { class: "field" }, h("label", { for: "opt-hours" }, "Run length (h)"), this.hours),
        this.job.el,
      ),
      this.results,
    );
  }

  async refresh(): Promise<void> {
    const sc = this.ctx.scenario();
    if (!sc || this.loadedFor === sc.id) return;
    this.loadedFor = sc.id;
    clear(this.results);
    this.hours.value = String(sc.run.horizon / 3600);
    let defaults: Defaults;
    try {
      defaults = await api.optimizerDefaults(sc.id);
    } catch {
      this.loadedFor = "";
      clear(this.vars);
      this.vars.append(h("p", { class: "muted" }, "Could not load optimizer settings. Open this tab again to retry."));
      return;
    }
    this.maxTrials = defaults.max_trials;
    this.trials.max = String(defaults.max_trials);
    this.renderVariables(sc, defaults.variables);
    this.renderEconomics(defaults.economics);
  }

  private renderVariables(sc: Scenario, variables: OptVariable[]): void {
    clear(this.vars);
    this.rows = [];
    const body = h("tbody");
    for (const v of variables) {
      const id = `opt-${v.key.replace(/[^A-Za-z0-9]/g, "_")}`;
      const isServers = v.key.startsWith("station.");
      // Machines and staff by default; buffers are opt-in to keep the default search small.
      const include = h("input", { type: "checkbox", id, checked: !v.key.startsWith("buffer.") }) as HTMLInputElement;
      const max = isServers ? 20 : v.key.startsWith("buffer.") ? 10000 : 100;
      const low = h("input", { type: "number", min: 1, max, step: 1, value: v.low, "aria-label": `${overrideLabel(sc, v.key)} minimum` }) as HTMLInputElement;
      const high = h("input", { type: "number", min: 1, max, step: 1, value: v.high, "aria-label": `${overrideLabel(sc, v.key)} maximum` }) as HTMLInputElement;
      this.rows.push({ include, low, high, key: v.key });
      body.append(h("tr", {}, h("td", {}, include), h("th", { scope: "row" }, h("label", { for: id }, overrideLabel(sc, v.key))), h("td", { class: "range-cell" }, low), h("td", { class: "range-cell" }, high)));
    }
    this.vars.append(
      h("table", { class: "data-table opt-vars" }, h("thead", {}, h("tr", {}, h("th", {}, h("span", { class: "visually-hidden" }, "Include")), h("th", {}, "Setting"), h("th", {}, "Min"), h("th", {}, "Max"))), body),
    );
  }

  private renderEconomics(e: Economics): void {
    clear(this.econ);
    this.econInputs.clear();
    for (const [key, label] of ECON_FIELDS) {
      const id = `econ-${key}`;
      const input = h("input", { id, type: "number", min: 0, step: "any", value: e[key] as number, inputmode: "decimal" }) as HTMLInputElement;
      this.econInputs.set(key, input);
      this.econ.append(h("div", { class: "field" }, h("label", { for: id }, label), input));
    }
  }

  private start(): Promise<{ job_id: string }> | null {
    const sc = this.ctx.scenario();
    if (!sc) return null;
    const variables: OptVariable[] = [];
    for (const r of this.rows) {
      if (!r.include.checked) continue;
      const low = Number(r.low.value);
      const high = Number(r.high.value);
      if (!Number.isInteger(low) || !Number.isInteger(high) || low < 1 || high < low || high > Number(r.high.max)) {
        r.low.focus();
        toast(`${overrideLabel(sc, r.key)}: enter whole numbers with min ≤ max (up to ${r.high.max}).`, "error");
        return null;
      }
      variables.push({ key: r.key, low, high });
    }
    if (!variables.length || variables.length > MAX_VARIABLES) {
      toast(`Select between 1 and ${MAX_VARIABLES} settings to optimize.`, "error");
      return null;
    }
    const economics: Partial<Economics> = {};
    for (const [key, input] of this.econInputs) {
      const v = Number(input.value);
      if (input.value.trim() === "" || !(v >= 0)) {
        input.focus();
        toast("Economics values must be numbers ≥ 0.", "error");
        return null;
      }
      (economics as Record<string, number>)[key] = v;
    }
    const trials = Number(this.trials.value);
    const reps = Number(this.reps.value);
    const hours = Number(this.hours.value);
    if (!Number.isInteger(trials) || trials < 3 || trials > this.maxTrials) {
      toast(`Trials must be a whole number from 3 to ${this.maxTrials}.`, "error");
      return null;
    }
    if (!Number.isInteger(reps) || reps < 1 || reps > 10) {
      toast("Replications per trial must be from 1 to 10.", "error");
      return null;
    }
    const warmup = Math.min(sc.run.warmup, hours * 3600 * 0.25);
    if (!(hours > 0 && hours <= 720)) {
      toast("Run length must be between 0 and 720 hours.", "error");
      return null;
    }
    clear(this.results);
    return api.startOptimization({ scenario_id: sc.id, variables, economics, trials, replications: reps, horizon: hours * 3600, warmup });
  }

  private show(r: OptimizationResult): void {
    const sc = this.ctx.scenario();
    if (!sc) return;
    clear(this.results);
    const changed = Object.entries(r.best.params).filter(([k, v]) => r.baseline.params[k] !== v);
    const bestOverrides: Overrides = Object.fromEntries(changed);
    const improved = r.improvement_per_hour > 0 && changed.length > 0;

    const rows = r.variables.map((v) =>
      h(
        "tr",
        {},
        h("th", { scope: "row" }, overrideLabel(sc, v.key)),
        h("td", { class: "num" }, String(r.baseline.params[v.key])),
        h("td", { class: `num ${r.best.params[v.key] !== r.baseline.params[v.key] ? "good" : ""}` }, String(r.best.params[v.key])),
      ),
    );
    const applyBtn = h("button", { type: "button", class: "btn btn-primary", disabled: !improved }, "Apply best to live run");
    applyBtn.addEventListener("click", () => {
      if (!r.confirmation.significant && !window.confirm("The improvement is not statistically significant. Apply it to the live run anyway?")) return;
      this.ctx.apply(bestOverrides);
    });
    const chart = h("div", { class: "chart" });

    const conf = r.confirmation;
    const d = conf.profit_difference;
    const headline = improved
      ? `Best configuration earns ${money(r.best.profit.mean)}/h vs ${money(r.baseline.profit.mean)}/h today: +${money(r.improvement_per_hour)}/h.`
      : "No configuration in the search space beat the current one.";
    const verdict = !improved
      ? `Checked the top ${conf.candidates.length} candidates on ${conf.seeds.length} fresh replications.`
      : conf.significant && d
        ? `Confirmed on ${conf.seeds.length} fresh replications: +${money(d.diff.mean)} ± ${money(d.diff.half_width)}/h (95% CI). Statistically significant.`
        : `On ${conf.seeds.length} fresh replications the gain${d ? ` (+${money(d.diff.mean)} ± ${money(d.diff.half_width)}/h)` : ""} is not statistically significant. Run more trials or replications before acting on it.`;
    this.results.append(
      h(
        "section",
        { class: "card" },
        h("h2", { class: "card-title" }, `Result · ${r.trials.length} trials + confirmation`),
        h("p", {}, h("strong", {}, headline)),
        h("p", { class: `small ${improved ? (conf.significant ? "good-text" : "warn-text") : "muted"}` }, verdict),
        h(
          "p",
          { class: "muted small" },
          `Throughput ${fmtNum(r.baseline.throughput.mean)} → ${fmtNum(r.best.throughput.mean)} parts/h; cost ${money(r.baseline.cost_per_hour)} → ${money(r.best.cost_per_hour)}/h. Bottleneck after change: ${sc.stations.find((s) => s.id === r.best.bottleneck)?.name ?? "–"}.`,
        ),
        h("table", { class: "data-table result-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Setting"), h("th", { class: "num" }, "Current"), h("th", { class: "num" }, "Best"))), h("tbody", {}, ...rows)),
        h("div", { class: "form-actions spaced" }, applyBtn),
      ),
      h("section", { class: "card" }, h("h2", { class: "card-title" }, "Search progress"), chart),
    );

    requestAnimationFrame(() => {
      this.plot?.destroy();
      const xs = [0, ...r.trials.map((t) => t.number)];
      const profit = [r.baseline.profit.mean, ...r.trials.map((t) => t.evaluation.profit.mean)];
      let best = -Infinity;
      const bestSoFar = profit.map((p) => (best = Math.max(best, p)));
      this.plot = new uPlot(
        {
          width: Math.max(220, chart.clientWidth),
          height: 170,
          scales: { x: { time: false } },
          series: [
            { label: "Trial" },
            { label: "Profit €/h", stroke: "#94a3b8", width: 0, points: { show: true, size: 6, fill: "#94a3b8" } },
            { label: "Best so far", stroke: "#22c55e", width: 2 },
          ],
          axes: [{ stroke: "#94a3b8" }, { stroke: "#94a3b8", size: 56 }],
        },
        [xs, profit, bestSoFar],
        chart,
      );
    });
  }
}
