import type { EventsMsg, HelloMsg, MachineState, Scenario, SimEvent } from "../types";

export type ProductLoc =
  | { kind: "pending" }
  | { kind: "buffer"; id: string; enteredAt: number; readyAt: number }
  | { kind: "machine"; id: string };

export interface ProductView {
  id: number;
  variant: string;
  loc: ProductLoc;
}

export interface Departure {
  id: number;
  variant: string;
  machine: string;
  outcome: "complete" | "scrap";
}

export interface MachineView {
  id: string;
  station: string;
  state: MachineState;
  product: number | null;
}

export interface StateSpan {
  t: number;
  state: MachineState;
}

/** How much simulated history the state timeline keeps per machine. */
export const TIMELINE_WINDOW = 2 * 3600;

/**
 * Client-side mirror of the line, rebuilt from a `hello` snapshot and kept
 * current by applying events in timestamp order. The 3D scene and the
 * timeline read from it; they never mutate it.
 */
export class LineState {
  scenario: Scenario | null = null;
  readonly products = new Map<number, ProductView>();
  readonly buffers = new Map<string, number[]>(); // product ids, front (oldest) first
  readonly machines = new Map<string, MachineView>();
  readonly timeline = new Map<string, StateSpan[]>();
  departures: Departure[] = [];
  /** Sim time up to which events have been applied. */
  t = 0;

  private queue: SimEvent[] = [];
  private lastSeq = 0;

  load(hello: HelloMsg): void {
    this.scenario = hello.scenario;
    this.products.clear();
    this.buffers.clear();
    this.machines.clear();
    this.timeline.clear();
    this.departures = [];
    this.queue = [];
    this.lastSeq = hello.seq;
    const snap = hello.snapshot;
    this.t = snap.t;

    for (const [pid, variant] of Object.entries(snap.products)) {
      this.products.set(Number(pid), { id: Number(pid), variant, loc: { kind: "pending" } });
    }
    const transit = new Map(hello.scenario.buffers.map((b) => [b.id, b.transit_time]));
    for (const b of hello.scenario.buffers) {
      const items = snap.buffers[b.id] ?? [];
      this.buffers.set(
        b.id,
        items.map((it) => it.product),
      );
      for (const it of items) {
        const p = this.products.get(it.product);
        // Older servers omit entered_at; fall back to the constant transit time.
        const enteredAt = it.entered_at ?? it.ready_at - (transit.get(b.id) ?? 0);
        if (p) p.loc = { kind: "buffer", id: b.id, enteredAt, readyAt: it.ready_at };
      }
    }
    for (const [mid, m] of Object.entries(snap.machines)) {
      this.machines.set(mid, { id: mid, station: mid.split("#")[0] ?? mid, state: m.state, product: m.product });
      this.timeline.set(mid, [{ t: snap.t, state: m.state }]);
      if (m.product !== null) {
        const p = this.products.get(m.product);
        if (p) p.loc = { kind: "machine", id: mid };
      }
    }
  }

  /** Queue a batch of events. Out-of-order or duplicate batches are dropped. */
  enqueue(msg: EventsMsg): void {
    if (msg.seq <= this.lastSeq) return;
    this.lastSeq = msg.seq;
    for (const ev of msg.events) this.queue.push(ev);
  }

  /** Accept the seq of a non-event message so ordering stays monotonic. */
  observeSeq(seq: number): void {
    if (seq > this.lastSeq) this.lastSeq = seq;
  }

  get pendingEvents(): number {
    return this.queue.length;
  }

  /** Apply queued events with timestamps up to `time`. */
  applyUntil(time: number): void {
    let i = 0;
    while (i < this.queue.length && this.queue[i]!.t <= time) {
      this.apply(this.queue[i]!);
      i++;
    }
    if (i) this.queue.splice(0, i);
    if (time > this.t) this.t = time;
  }

  private removeFromCurrent(p: ProductView): void {
    if (p.loc.kind === "buffer") {
      const q = this.buffers.get(p.loc.id);
      const idx = q?.indexOf(p.id) ?? -1;
      if (q && idx >= 0) q.splice(idx, 1);
    } else if (p.loc.kind === "machine") {
      const m = this.machines.get(p.loc.id);
      if (m && m.product === p.id) m.product = null;
    }
  }

  private apply(ev: SimEvent): void {
    switch (ev.type) {
      case "create":
        this.products.set(ev.product, { id: ev.product, variant: ev.variant, loc: { kind: "pending" } });
        break;
      case "enter": {
        const p = this.products.get(ev.product);
        if (!p) return;
        this.removeFromCurrent(p);
        p.loc = { kind: "buffer", id: ev.buffer, enteredAt: ev.t, readyAt: ev.ready_at };
        this.buffers.get(ev.buffer)?.push(ev.product);
        break;
      }
      case "take": {
        const p = this.products.get(ev.product);
        if (!p) return;
        this.removeFromCurrent(p);
        p.loc = { kind: "machine", id: ev.machine };
        const m = this.machines.get(ev.machine);
        if (m) m.product = ev.product;
        break;
      }
      case "state": {
        const m = this.machines.get(ev.machine);
        if (m) m.state = ev.state;
        const spans = this.timeline.get(ev.machine);
        if (spans) {
          spans.push({ t: ev.t, state: ev.state });
          // Keep one span that starts before the window so the bar is filled from its left edge.
          const cutoff = ev.t - TIMELINE_WINDOW;
          let drop = 0;
          while (drop + 1 < spans.length && spans[drop + 1]!.t <= cutoff) drop++;
          if (drop) spans.splice(0, drop);
        }
        break;
      }
      case "exit": {
        const p = this.products.get(ev.product);
        if (!p) return;
        this.removeFromCurrent(p);
        this.products.delete(ev.product);
        this.departures.push({ id: p.id, variant: p.variant, machine: ev.machine, outcome: ev.outcome });
        break;
      }
    }
  }

  /** Hand departures to the renderer (each departure is consumed once). */
  takeDepartures(): Departure[] {
    const out = this.departures;
    this.departures = [];
    return out;
  }
}

/**
 * Display clock for the simulation.
 *
 * The server streams batches every ~50 ms. Playing events back slightly behind
 * the newest known server time (`lagSeconds` of real time) lets each event be
 * shown at its true simulated moment instead of in 50 ms bursts, while never
 * running ahead of data that has actually arrived.
 */
export class SimClock {
  private serverT = 0;
  private receivedAtMs = 0;
  private shown = 0;
  playing = false;
  speed = 1;

  constructor(private readonly lagSeconds = 0.12) {}

  reset(t: number, nowMs: number): void {
    this.serverT = t;
    this.shown = t;
    this.receivedAtMs = nowMs;
  }

  sync(t: number, nowMs: number): void {
    if (t < this.serverT) return;
    this.serverT = t;
    this.receivedAtMs = nowMs;
  }

  get latest(): number {
    return this.serverT;
  }

  now(nowMs: number): number {
    let est: number;
    if (!this.playing) {
      est = this.serverT;
    } else {
      const elapsed = Math.max(0, (nowMs - this.receivedAtMs) / 1000);
      est = this.serverT + (elapsed - this.lagSeconds) * this.speed;
    }
    est = Math.min(est, this.serverT);
    if (est > this.shown) this.shown = est;
    return this.shown;
  }
}
