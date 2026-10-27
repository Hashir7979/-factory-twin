import { STATE_COLORS, STATE_LABELS, type Pick } from "../scene/factory";
import type { LineState } from "../sim/state";
import type { BufferCfg, Kpis, Scenario } from "../types";
import { MACHINE_STATES } from "../types";
import { clear, fmtDuration, fmtNum, fmtPct, h } from "./dom";

function meanTransit(b: BufferCfg): number {
  const t = b.transit;
  if (!t) return b.transit_time;
  if (typeof t.mean === "number") return t.mean;
  if (typeof t.value === "number") return t.value;
  return b.transit_time;
}

/** Details card for the selected machine or buffer, with a failure-injection control. */
export class Inspector {
  onInjectFailure: (machine: string, repairSeconds: number) => void = () => {};
  onClose: () => void = () => {};

  private pick: Pick = null;
  private repairMinutes = 5;

  constructor(private readonly host: HTMLElement) {}

  get selected(): Pick {
    return this.pick;
  }

  show(pick: Pick, scenario: Scenario | null, state: LineState, kpis: Kpis | null, canControl: boolean): void {
    const changed = JSON.stringify(pick) !== JSON.stringify(this.pick);
    this.pick = pick;
    if (!pick || !scenario) {
      this.host.hidden = true;
      clear(this.host);
      return;
    }
    if (changed && pick.kind === "machine") {
      const st = scenario.stations.find((s) => s.id === pick.id.split("#")[0]);
      const ttr = st?.failure?.ttr;
      const mean = ttr && typeof ttr.mean === "number" ? ttr.mean : ttr && typeof ttr.value === "number" ? ttr.value : 300;
      this.repairMinutes = Math.max(1, Math.round(mean / 60));
    }
    this.host.hidden = false;
    this.render(pick, scenario, state, kpis, canControl, changed);
  }

  private render(pick: NonNullable<Pick>, scenario: Scenario, state: LineState, kpis: Kpis | null, canControl: boolean, focus: boolean): void {
    // Preserve focus on the repair input across re-renders.
    const active = document.activeElement;
    const hadInputFocus = active instanceof HTMLInputElement && this.host.contains(active);
    clear(this.host);
    const closeBtn = h("button", { type: "button", class: "icon-btn", "aria-label": "Close details" }, "×");
    closeBtn.addEventListener("click", () => this.onClose());

    if (pick.kind === "machine") {
      const m = state.machines.get(pick.id);
      const st = scenario.stations.find((s) => s.id === pick.id.split("#")[0]);
      const mk = kpis?.machines.find((x) => x.id === pick.id);
      if (!m || !st) return;
      const product = m.product !== null ? state.products.get(m.product) : undefined;
      const variant = product ? scenario.variants.find((v) => v.id === product.variant) : undefined;
      const title = st.servers > 1 ? `${st.name} ${pick.id.split("#")[1]}` : st.name;

      const input = h("input", {
        id: "repair-minutes", type: "number", min: 1, max: 1440, step: 1, value: this.repairMinutes, inputmode: "numeric",
      }) as HTMLInputElement;
      input.addEventListener("input", () => {
        const v = Number(input.value);
        if (Number.isFinite(v) && v >= 1 && v <= 1440) this.repairMinutes = Math.round(v);
      });
      const isDown = m.state === "down";
      const breakBtn = h(
        "button",
        { type: "button", class: "btn btn-danger", disabled: !canControl || isDown },
        isDown ? "Under repair" : "Break down now",
      );
      breakBtn.addEventListener("click", () => this.onInjectFailure(pick.id, this.repairMinutes * 60));

      const bar = h("div", { class: "state-bar", role: "img", "aria-label": "time in state" });
      if (mk) {
        for (const s of MACHINE_STATES) {
          const p = mk.state_pct[s] ?? 0;
          if (p > 0) bar.append(h("span", { style: `width:${(p * 100).toFixed(2)}%;background:${STATE_COLORS[s]}`, title: `${STATE_LABELS[s]} ${fmtPct(p, 1)}` }));
        }
      }
      this.host.append(
        h("div", { class: "inspector-head" }, h("h3", { id: "inspector-title" }, title), closeBtn),
        h("div", { class: "state-chip", style: `--chip:${STATE_COLORS[m.state]}` }, STATE_LABELS[m.state]),
        h(
          "dl",
          { class: "facts" },
          h("dt", {}, "Holding"),
          h("dd", {}, product ? `#${product.id} · ${variant?.name ?? product.variant}` : "nothing"),
          h("dt", {}, "Processed"),
          h("dd", {}, mk ? String(mk.processed) : "–"),
          h("dt", {}, "Utilization"),
          h("dd", {}, fmtPct(mk?.utilization)),
          h("dt", {}, "Failures"),
          h("dd", {}, mk ? String(mk.failures) : "–"),
          h("dt", {}, "Avg active period"),
          h("dd", {}, mk ? fmtDuration(mk.mean_active_period) : "–"),
        ),
        bar,
        h(
          "div",
          { class: "inject" },
          h("label", { for: "repair-minutes" }, "Repair time (min)"),
          input,
          breakBtn,
        ),
      );
      if (!canControl) this.host.append(h("p", { class: "muted small" }, "Reconnect to control the simulation."));
      if (hadInputFocus) input.focus();
    } else {
      const b = scenario.buffers.find((x) => x.id === pick.id);
      const bk = kpis?.buffers.find((x) => x.id === pick.id);
      if (!b) return;
      const level = state.buffers.get(b.id)?.length ?? 0;
      this.host.append(
        h("div", { class: "inspector-head" }, h("h3", { id: "inspector-title" }, b.name), closeBtn),
        h(
          "dl",
          { class: "facts" },
          h("dt", {}, "Type"),
          h("dd", {}, b.kind === "queue" ? "Queue" : `${b.kind === "road" ? "Road" : "Conveyor"} · ${fmtDuration(meanTransit(b))} travel`),
          h("dt", {}, "Contents"),
          h("dd", {}, `${level}${b.capacity ? ` / ${b.capacity}` : ""}`),
          h("dt", {}, "Avg level"),
          h("dd", {}, fmtNum(bk?.mean_level)),
          h("dt", {}, "Max level"),
          h("dd", {}, fmtNum(bk?.max_level, 0)),
          h("dt", {}, "Avg wait"),
          h("dd", {}, bk ? fmtDuration(bk.mean_wait) : "–"),
          ...(b.kind === "queue"
            ? []
            : [h("dt", {}, "Avg queueing after travel"), h("dd", {}, bk ? fmtDuration(bk.mean_queue_delay) : "–")]),
        ),
      );
    }
    if (focus) closeBtn.focus({ preventScroll: true });
  }
}
