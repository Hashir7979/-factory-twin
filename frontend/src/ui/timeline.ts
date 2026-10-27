import { STATE_COLORS } from "../scene/factory";
import { type LineState, TIMELINE_WINDOW } from "../sim/state";

const ROW = 16;
const GAP = 4;
const LABEL_W = 92;

/**
 * Machine state timeline (Gantt) for the last two simulated hours. Drawn on a
 * canvas; the same information is available as text in the stations table.
 */
export class StateTimeline {
  private readonly ctx: CanvasRenderingContext2D | null;

  constructor(private readonly canvas: HTMLCanvasElement) {
    this.ctx = canvas.getContext("2d");
  }

  draw(state: LineState, now: number): void {
    const ctx = this.ctx;
    if (!ctx) return;
    const ids = [...state.machines.keys()];
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = this.canvas.clientWidth || 300;
    const height = ids.length * (ROW + GAP) + 18;
    if (this.canvas.width !== Math.round(width * dpr) || this.canvas.height !== Math.round(height * dpr)) {
      this.canvas.width = Math.round(width * dpr);
      this.canvas.height = Math.round(height * dpr);
      this.canvas.style.height = `${height}px`;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const styles = getComputedStyle(document.documentElement);
    const text = styles.getPropertyValue("--muted").trim() || "#94a3b8";
    const start = Math.max(0, now - TIMELINE_WINDOW);
    const span = Math.max(1, now - start);
    const plotW = width - LABEL_W - 4;
    const x = (t: number) => LABEL_W + ((Math.max(t, start) - start) / span) * plotW;

    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ids.forEach((id, row) => {
      const y = row * (ROW + GAP);
      ctx.fillStyle = text;
      ctx.fillText(id.replace("#", " "), 0, y + ROW / 2, LABEL_W - 6);
      const spans = state.timeline.get(id) ?? [];
      for (let i = 0; i < spans.length; i++) {
        const s = spans[i]!;
        const end = i + 1 < spans.length ? spans[i + 1]!.t : now;
        if (end < start) continue;
        ctx.fillStyle = STATE_COLORS[s.state];
        ctx.fillRect(x(s.t), y, Math.max(1, x(end) - x(s.t)), ROW);
      }
    });
    ctx.fillStyle = text;
    const axisY = ids.length * (ROW + GAP) + 8;
    ctx.fillText(now > TIMELINE_WINDOW ? "−2 h" : "0", LABEL_W, axisY);
    ctx.textAlign = "right";
    ctx.fillText("now", width - 2, axisY);
    ctx.textAlign = "left";
  }
}
