import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

const MAX_POINTS = 1500;

function cssVar(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** Live line chart of rolling throughput and WIP against simulated hours. */
export class TrendChart {
  private plot: uPlot | null = null;
  private wipLabel = "WIP";
  private data: [number[], (number | null)[], (number | null)[]] = [[], [], []];
  private readonly observer: ResizeObserver;

  constructor(private readonly host: HTMLElement) {
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
    this.create();
  }

  private create(): void {
    const axis = { stroke: cssVar("--muted", "#94a3b8"), grid: { stroke: cssVar("--border", "#1e293b"), width: 1 }, ticks: { show: false } };
    this.plot?.destroy();
    this.plot = new uPlot(
      {
        width: Math.max(200, this.host.clientWidth),
        height: 170,
        legend: { show: true, live: false },
        cursor: { drag: { x: false, y: false } },
        scales: { x: { time: false } },
        series: [
          { label: "Sim hour", value: (_u, v) => (v === null ? "–" : `${v.toFixed(2)} h`) },
          { label: "Throughput /h", stroke: "#3b82f6", width: 2, scale: "tp", value: (_u, v) => (v === null ? "–" : v.toFixed(1)) },
          { label: this.wipLabel, stroke: "#f59e0b", width: 2, scale: "wip", value: (_u, v) => (v === null ? "–" : v.toFixed(0)) },
        ],
        axes: [
          { ...axis, values: (_u, vals) => vals.map((v) => `${v}h`) },
          { ...axis, scale: "tp", size: 44 },
          { ...axis, scale: "wip", side: 1, size: 36, grid: { show: false } },
        ],
      },
      this.data,
      this.host,
    );
  }

  push(simSeconds: number, throughput: number, wip: number): void {
    const [xs, tp, wp] = this.data;
    const x = simSeconds / 3600;
    if (xs.length && x <= xs[xs.length - 1]!) return;
    xs.push(x);
    tp.push(throughput);
    wp.push(wip);
    if (xs.length > MAX_POINTS) {
      // Thin the history by half rather than dropping it, so long runs keep their shape.
      this.data = [xs.filter((_, i) => i % 2 === 0), tp.filter((_, i) => i % 2 === 0), wp.filter((_, i) => i % 2 === 0)];
    }
    this.plot?.setData(this.data);
  }

  /** Rename the second series (e.g. "Trucks" for a fleet, where WIP is the fleet size). */
  setWipLabel(label: string): void {
    if (label === this.wipLabel) return;
    this.wipLabel = label;
    this.create();
  }

  reset(): void {
    this.data = [[], [], []];
    this.plot?.setData(this.data);
  }

  private resize(): void {
    this.plot?.setSize({ width: Math.max(200, this.host.clientWidth), height: 170 });
  }
}
