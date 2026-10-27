import uPlot from "uplot";
import { api } from "../net/api";
import type { ComparisonResult, ExperimentResult, Overrides, Scenario } from "../types";
import { clear, fmtClock, fmtPct, h } from "./dom";
import { JobControl } from "./job";
import { METRICS, describeOverrides, fmtEstimate } from "./labels";
import { toast } from "./toast";

interface Context {
  scenario: () => Scenario | null;
  overrides: () => Overrides | null;
}

/**
 * Replication studies: run N independent replications for statistically sound
 * KPIs (mean ± 95% CI), or compare baseline vs the what-if settings using
 * common random numbers (paired seeds).
 */
export class ExperimentsPanel {
  private readonly form: HTMLElement;
  private readonly results: HTMLElement;
  private readonly settingsSummary: HTMLElement;
  private readonly job: JobControl<ExperimentResult | ComparisonResult>;
  private readonly inputs: { reps: HTMLInputElement; hours: HTMLInputElement; warmup: HTMLInputElement; compare: HTMLInputElement };
  private warmupPlot: uPlot | null = null;
  private mode: "single" | "compare" = "compare";
  private scenarioId = "";

  constructor(host: HTMLElement, private readonly ctx: Context) {
    const num = (id: string, value: number, min: number, max: number, step: number) =>
      h("input", { id, type: "number", value, min, max, step, inputmode: "decimal" }) as HTMLInputElement;
    this.inputs = {
      reps: num("exp-reps", 10, 2, 50, 1),
      hours: num("exp-hours", 9, 1, 720, 0.5),
      warmup: num("exp-warmup", 1, 0, 100, 0.25),
      compare: h("input", { id: "exp-compare", type: "checkbox", checked: true }) as HTMLInputElement,
    };
    this.settingsSummary = h("p", { class: "muted small" });
    this.job = new JobControl("Run experiment", () => this.start(), (r, id) => this.show(r, id));
    this.form = h(
      "section",
      { class: "card", "aria-labelledby": "h-exp" },
      h("h2", { id: "h-exp", class: "card-title" }, "Replication study"),
      h("p", { class: "muted small" }, "Live runs show one random sample. A study runs many independent replications in parallel and reports each KPI with a 95% confidence interval."),
      h("div", { class: "field" }, h("label", { for: "exp-reps" }, "Replications"), this.inputs.reps),
      h("div", { class: "field" }, h("label", { for: "exp-hours" }, "Run length (h)"), this.inputs.hours),
      h("div", { class: "field" }, h("label", { for: "exp-warmup" }, "Warm-up (h)"), this.inputs.warmup),
      h("div", { class: "field checkbox" }, h("label", { for: "exp-compare" }, "Compare baseline vs what-if settings"), this.inputs.compare),
      this.settingsSummary,
      this.job.el,
    );
    this.results = h("div", { class: "results", "aria-live": "polite" });
    host.append(this.form, this.results);
    this.inputs.compare.addEventListener("change", () => this.refresh());
    // Keep user-entered run lengths when the tab is revisited; defaults follow the scenario otherwise.
    for (const el of [this.inputs.hours, this.inputs.warmup]) el.addEventListener("input", () => (el.dataset.touched = "1"));
  }

  /** Re-read scenario defaults and current what-if settings (called when the tab opens). */
  refresh(): void {
    const sc = this.ctx.scenario();
    const ov = this.ctx.overrides();
    if (!sc) return;
    if (sc.id !== this.scenarioId) {
      this.scenarioId = sc.id;
      delete this.inputs.hours.dataset.touched;
      delete this.inputs.warmup.dataset.touched;
      clear(this.results);
    }
    if (!this.inputs.hours.dataset.touched) this.inputs.hours.value = String(sc.run.horizon / 3600);
    if (!this.inputs.warmup.dataset.touched) this.inputs.warmup.value = String(sc.run.warmup / 3600);
    this.mode = this.inputs.compare.checked ? "compare" : "single";
    if (!ov) this.settingsSummary.textContent = "Fix the invalid what-if settings first.";
    else if (this.mode === "compare" && !Object.keys(ov).length)
      this.settingsSummary.textContent = "Change something in the What-if tab to compare it against the baseline.";
    else this.settingsSummary.textContent = `${this.mode === "compare" ? "What-if" : "Settings"}: ${describeOverrides(sc, ov)}.`;
    this.job.disabled = !ov || (this.mode === "compare" && !Object.keys(ov).length);
  }

  private start(): Promise<{ job_id: string }> | null {
    const sc = this.ctx.scenario();
    const ov = this.ctx.overrides();
    const reps = Number(this.inputs.reps.value);
    const hours = Number(this.inputs.hours.value);
    const warm = Number(this.inputs.warmup.value);
    if (!sc || !ov) return null;
    if (!Number.isInteger(reps) || reps < 2 || reps > 50) return this.invalid("Replications must be a whole number from 2 to 50.");
    if (!(hours > 0 && hours <= 720)) return this.invalid("Run length must be between 0 and 720 hours.");
    if (!(warm >= 0 && warm < hours)) return this.invalid("Warm-up must be shorter than the run length.");
    const common = { scenario_id: sc.id, replications: reps, horizon: hours * 3600, warmup: warm * 3600 };
    clear(this.results);
    if (this.mode === "compare") {
      return api.startComparison({ ...common, a_label: "Baseline", b_label: "What-if", a_overrides: {}, b_overrides: ov });
    }
    return api.startExperiment({ ...common, overrides: ov });
  }

  private invalid(msg: string): null {
    toast(msg, "error");
    return null;
  }

  private show(result: ExperimentResult | ComparisonResult, jobId: string): void {
    clear(this.results);
    const sc = this.ctx.scenario();
    if (!sc) return;
    if ("differences" in result) this.showComparison(result, sc);
    else this.showSingle(result, sc);
    this.results.append(h("p", {}, h("a", { href: api.csvUrl(jobId), download: "" }, "Download per-replication data (CSV)")));
  }

  private showSingle(r: ExperimentResult, sc: Scenario): void {
    const rows = Object.entries(METRICS).map(([key, m]) => {
      const e = r.metrics[key]!;
      return h("tr", {}, h("th", { scope: "row" }, m.label), h("td", { class: "num" }, fmtEstimate(e, m.fmt)), h("td", { class: "num muted" }, `${m.fmt(e.min)} – ${m.fmt(e.max)}`));
    });
    this.results.append(
      h(
        "section",
        { class: "card" },
        h("h2", { class: "card-title" }, `Results · ${r.seeds.length} replications`),
        h("p", { class: "muted small" }, describeOverrides(sc, r.overrides)),
        h("table", { class: "data-table result-table" }, h("thead", {}, h("tr", {}, h("th", {}, "KPI"), h("th", { class: "num" }, "Mean ± 95% CI"), h("th", { class: "num" }, "Range"))), h("tbody", {}, ...rows)),
      ),
      this.bottleneckCard(r, sc),
      this.warmupCard(r),
    );
  }

  private showComparison(r: ComparisonResult, sc: Scenario): void {
    const rows = r.differences
      .filter((d) => METRICS[d.metric])
      .map((d) => {
        const m = METRICS[d.metric]!;
        const better = d.diff.mean === 0 ? null : (d.diff.mean > 0) === m.higherIsBetter;
        const cls = !d.significant || better === null ? "" : better ? "good" : "bad";
        const verdict = d.significant ? (better ? "better" : "worse") : "no clear difference";
        return h(
          "tr",
          {},
          h("th", { scope: "row" }, m.label),
          h("td", { class: "num" }, m.fmt(d.a.mean)),
          h("td", { class: "num" }, m.fmt(d.b.mean)),
          h("td", { class: `num ${cls}` }, `${d.diff.mean >= 0 ? "+" : "−"}${m.fmt(Math.abs(d.diff.mean))} ± ${m.fmt(d.diff.half_width)}`),
          h("td", { class: `small ${cls}` }, verdict),
        );
      });
    const tp = r.differences.find((d) => d.metric === "throughput_per_hour");
    const headline = tp
      ? tp.significant
        ? `Throughput ${tp.diff.mean > 0 ? "rises" : "falls"} by ${Math.abs(tp.diff.mean).toFixed(1)} parts/h (${tp.relative !== null ? fmtPct(Math.abs(tp.relative), 1) : "–"}), 95% CI ±${tp.diff.half_width.toFixed(1)}. The difference is statistically significant.`
        : `No statistically significant throughput difference (${tp.diff.mean >= 0 ? "+" : ""}${tp.diff.mean.toFixed(1)} ± ${tp.diff.half_width.toFixed(1)} parts/h). Try more replications if you expect a small effect.`
      : "";
    this.results.append(
      h(
        "section",
        { class: "card" },
        h("h2", { class: "card-title" }, `Baseline vs what-if · ${r.a.seeds.length} paired replications`),
        h("p", {}, h("strong", {}, headline)),
        h("p", { class: "muted small" }, `What-if: ${describeOverrides(sc, r.b.overrides)}. Both arms use identical random seeds, so the difference column is a paired estimate.`),
        h(
          "table",
          { class: "data-table result-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "KPI"), h("th", { class: "num" }, "Baseline"), h("th", { class: "num" }, "What-if"), h("th", { class: "num" }, "Δ ± 95% CI"), h("th", {}, ""))),
          h("tbody", {}, ...rows),
        ),
      ),
      this.bottleneckCard(r.b, sc, "What-if bottleneck"),
    );
  }

  private bottleneckCard(r: ExperimentResult, sc: Scenario, title = "Bottleneck frequency"): HTMLElement {
    const items = Object.entries(r.bottleneck_frequency).map(([id, f]) =>
      h("li", {}, `${sc.stations.find((s) => s.id === id)?.name ?? id}: `, h("strong", {}, fmtPct(f)), " of replications"),
    );
    return h("section", { class: "card" }, h("h2", { class: "card-title" }, title), items.length ? h("ul", { class: "plain-list" }, ...items) : h("p", { class: "muted" }, "No bottleneck identified."));
  }

  private warmupCard(r: ExperimentResult): HTMLElement {
    const w = r.warmup_analysis;
    const chart = h("div", { class: "chart" });
    const text =
      w.suggested_warmup !== null
        ? `Cycle time settles after about ${fmtClock(w.suggested_warmup)} (steady state ≈ ${((w.steady_state ?? 0) / 60).toFixed(1)} min). You used a warm-up of ${fmtClock(r.warmup)}${w.suggested_warmup > r.warmup ? ", which looks too short." : ", which is enough."}`
        : "Not enough data to suggest a warm-up period.";
    const card = h("section", { class: "card" }, h("h2", { class: "card-title" }, "Warm-up analysis (Welch / MSER)"), h("p", { class: "small" }, text), chart);
    requestAnimationFrame(() => {
      this.warmupPlot?.destroy();
      const xs = w.bins.map((b) => b / 3600);
      const toMin = (v: (number | null)[]) => v.map((x) => (x === null ? null : x / 60));
      this.warmupPlot = new uPlot(
        {
          width: Math.max(220, chart.clientWidth),
          height: 160,
          scales: { x: { time: false } },
          series: [
            { label: "Sim hour" },
            { label: "Cycle time (min)", stroke: "#94a3b8", width: 1 },
            { label: "Smoothed", stroke: "#3b82f6", width: 2 },
          ],
          axes: [{ stroke: "#94a3b8", values: (_u, v) => v.map((x) => `${x}h`) }, { stroke: "#94a3b8", size: 40 }],
        },
        [xs, toMin(w.mean_series), toMin(w.smoothed)],
        chart,
      );
    });
    return card;
  }
}
