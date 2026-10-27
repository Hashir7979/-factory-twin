import { STATE_COLORS, STATE_LABELS } from "../scene/factory";
import type { Kpis, KpisMsg, MachineState, Scenario } from "../types";
import { MACHINE_STATES } from "../types";
import { clear, fmtDuration, fmtNum, fmtPct, h } from "./dom";

interface Card {
  value: HTMLElement;
  sub: HTMLElement;
}

const CARDS = [
  ["throughput", "Throughput", "parts / hour (since warm-up)"],
  ["rolling", "Last hour", "parts / hour"],
  ["cycle", "Cycle time", "mean, source → shipping"],
  ["wip", "WIP", "parts in system (time-avg)"],
  ["oee", "Line OEE", "bottleneck A × P × Q"],
  ["fpy", "First-pass yield", "passed first inspection"],
] as const;

export class KpiPanel {
  private cards = new Map<string, Card>();
  private bottleneck: HTMLElement;
  private stationsBody: HTMLElement;
  private buffersBody: HTMLElement;
  private legend: HTMLElement;

  constructor(cardsHost: HTMLElement, bottleneckHost: HTMLElement, stationsHost: HTMLElement, buffersHost: HTMLElement, legendHost: HTMLElement) {
    for (const [key, label, hint] of CARDS) {
      const value = h("div", { class: "kpi-value" }, "–");
      const sub = h("div", { class: "kpi-sub" }, hint);
      cardsHost.append(h("div", { class: "kpi-card" }, h("div", { class: "kpi-label" }, label), value, sub));
      this.cards.set(key, { value, sub });
    }
    this.bottleneck = bottleneckHost;
    this.stationsBody = stationsHost;
    this.buffersBody = buffersHost;
    this.legend = legendHost;
    this.renderLegend();
  }

  private renderLegend(): void {
    clear(this.legend);
    for (const s of MACHINE_STATES) {
      this.legend.append(
        h("span", { class: "legend-item" }, h("span", { class: "swatch", style: `background:${STATE_COLORS[s]}` }), STATE_LABELS[s]),
      );
    }
  }

  reset(): void {
    for (const c of this.cards.values()) c.value.textContent = "–";
    clear(this.stationsBody);
    clear(this.buffersBody);
    this.bottleneck.textContent = "Collecting data…";
  }

  update(msg: KpisMsg, scenario: Scenario): void {
    const k = msg.data;
    const line = k.line;
    const observed = line.observed_time > 0;
    this.set("throughput", observed ? fmtNum(line.throughput_per_hour) : "–");
    this.set("rolling", msg.t > 0 ? fmtNum(msg.rolling_throughput) : "–");
    this.set("cycle", line.completed ? fmtDuration(line.mean_cycle_time) : "–");
    this.set("wip", observed ? fmtNum(line.mean_wip) : String(line.wip));
    this.set("oee", observed ? fmtPct(line.line_oee) : "–");
    this.set("fpy", line.completed ? fmtPct(line.first_pass_yield, 1) : "–");
    this.renderBottleneck(k);
    this.renderStations(k);
    this.renderBuffers(k, scenario);
  }

  private set(key: string, value: string): void {
    const c = this.cards.get(key);
    if (c && c.value.textContent !== value) c.value.textContent = value;
  }

  private renderBottleneck(k: Kpis): void {
    const b = k.bottleneck;
    clear(this.bottleneck);
    if (!b.station) {
      this.bottleneck.append(h("p", { class: "muted" }, b.explanation));
      return;
    }
    const name = b.ranking[0]?.name ?? b.station;
    const badge = b.confidence === "clear" ? "Bottleneck" : "Shifting bottleneck";
    this.bottleneck.append(
      h("div", { class: "bn-head" }, h("span", { class: `badge badge-${b.confidence}` }, badge), h("strong", {}, name)),
      h("p", {}, b.explanation),
    );
    const evidence: string[] = [];
    if (b.upstream_blocked_pct !== null) evidence.push(`upstream blocked ${fmtPct(b.upstream_blocked_pct)}`);
    if (b.downstream_starved_pct !== null) evidence.push(`downstream starved ${fmtPct(b.downstream_starved_pct)}`);
    if (evidence.length) this.bottleneck.append(h("p", { class: "muted small" }, `Evidence: ${evidence.join(", ")}.`));
  }

  private renderStations(k: Kpis): void {
    clear(this.stationsBody);
    for (const st of k.stations) {
      const machines = k.machines.filter((m) => m.station === st.id);
      const pct = Object.fromEntries(
        MACHINE_STATES.map((s) => [s, machines.reduce((a, m) => a + (m.state_pct[s] ?? 0), 0) / Math.max(1, machines.length)]),
      ) as Record<MachineState, number>;
      const summary = MACHINE_STATES.filter((s) => pct[s] > 0.005)
        .map((s) => `${STATE_LABELS[s]} ${fmtPct(pct[s])}`)
        .join(", ");
      const bar = h("div", { class: "state-bar", role: "img", "aria-label": summary || "no data" });
      for (const s of MACHINE_STATES) {
        if (pct[s] <= 0) continue;
        bar.append(h("span", { style: `width:${(pct[s] * 100).toFixed(2)}%;background:${STATE_COLORS[s]}`, title: `${STATE_LABELS[s]} ${fmtPct(pct[s], 1)}` }));
      }
      const isBn = k.bottleneck.station === st.id;
      this.stationsBody.append(
        h(
          "tr",
          { class: isBn ? "is-bottleneck" : "" },
          h("th", { scope: "row" }, st.name, st.servers > 1 ? h("span", { class: "muted small" }, ` ×${st.servers}`) : null),
          h("td", { class: "num" }, fmtPct(st.utilization)),
          h("td", { class: "num" }, fmtPct(st.oee)),
          h("td", { class: "bar-cell" }, bar),
        ),
      );
    }
  }

  private renderBuffers(k: Kpis, scenario: Scenario): void {
    clear(this.buffersBody);
    for (const b of k.buffers) {
      const cfg = scenario.buffers.find((x) => x.id === b.id);
      const fill = b.fill_pct ?? 0;
      this.buffersBody.append(
        h(
          "tr",
          {},
          h("th", { scope: "row" }, b.name),
          h("td", { class: "num" }, `${b.level}${b.capacity ? `/${b.capacity}` : ""}`),
          h("td", { class: "num" }, fmtNum(b.mean_level)),
          h("td", { class: "num" }, fmtDuration(b.mean_wait)),
          h(
            "td",
            { class: "bar-cell" },
            b.capacity
              ? h(
                  "div",
                  { class: "fill-bar", role: "img", "aria-label": `average fill ${fmtPct(fill)}`, title: cfg?.kind === "conveyor" ? "Conveyor" : "Queue" },
                  h("span", { style: `width:${Math.min(100, fill * 100).toFixed(1)}%`, class: fill > 0.85 ? "hot" : "" }),
                )
              : h("span", { class: "muted small" }, "unbounded"),
          ),
        ),
      );
    }
  }
}
