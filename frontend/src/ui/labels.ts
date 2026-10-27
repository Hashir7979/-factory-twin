import type { Estimate, Overrides, Scenario } from "../types";
import { fmtDuration, fmtNum, fmtPct } from "./dom";

export function overrideLabel(s: Scenario, key: string): string {
  const [kind, id, field] = key.split(".");
  if (kind === "station" && field === "servers") return `${s.stations.find((x) => x.id === id)?.name ?? id} machines`;
  if (kind === "buffer" && field === "capacity") return `${s.buffers.find((x) => x.id === id)?.name ?? id} capacity`;
  if (key === "resources.technicians") return "Technicians";
  if (key === "resources.operators") return "Operators";
  if (key === "source.interarrival_scale") return "Arrival interval ×";
  if (key === "source.max_arrivals") return s.entity === "truck" ? "Trucks" : "Fleet size";
  return key;
}

export function describeOverrides(s: Scenario, o: Overrides): string {
  const parts = Object.entries(o).map(([k, v]) => `${overrideLabel(s, k)} = ${v}`);
  return parts.length ? parts.join(", ") : "baseline settings (no changes)";
}

interface MetricInfo {
  label: string;
  higherIsBetter: boolean;
  fmt: (x: number) => string;
}

export const METRICS: Record<string, MetricInfo> = {
  throughput_per_hour: { label: "Throughput (/h)", higherIsBetter: true, fmt: (x) => fmtNum(x, 1) },
  mean_cycle_time: { label: "Cycle time", higherIsBetter: false, fmt: (x) => fmtDuration(x) },
  mean_wip: { label: "WIP (avg parts)", higherIsBetter: false, fmt: (x) => fmtNum(x, 1) },
  line_oee: { label: "Line OEE", higherIsBetter: true, fmt: (x) => fmtPct(x, 1) },
  first_pass_yield: { label: "First-pass yield", higherIsBetter: true, fmt: (x) => fmtPct(x, 1) },
  scrapped: { label: "Scrapped parts", higherIsBetter: false, fmt: (x) => fmtNum(x, 1) },
};

export function fmtEstimate(e: Estimate, fmt: (x: number) => string): string {
  return e.n > 1 ? `${fmt(e.mean)} ± ${fmt(e.half_width)}` : fmt(e.mean);
}

/** Metrics worth showing for a scenario: WIP, yield and scrap are constant in a closed fleet loop. */
export function metricsFor(s: Scenario): [string, MetricInfo][] {
  const closed = s.output_station !== null && s.output_station !== undefined;
  const skip = closed ? new Set(["mean_wip", "first_pass_yield", "scrapped"]) : new Set<string>();
  return Object.entries(METRICS).filter(([k]) => !skip.has(k));
}
