import { describe, expect, it } from "vitest";
import type { EventsMsg, HelloMsg, Scenario } from "../types";
import { LineState, SimClock } from "./state";

const scenario = {
  id: "t",
  name: "t",
  description: "",
  variants: [{ id: "A", name: "A", weight: 1, color: "#ffffff" }],
  source: { id: "source", name: "S", interarrival: { type: "constant", value: 1 }, output: "Q", pos: [0, 0] },
  buffers: [
    { id: "Q", name: "Q", capacity: 5, kind: "queue", transit_time: 0, pos: [1, 0], path: null },
    { id: "C", name: "C", capacity: 4, kind: "conveyor", transit_time: 10, pos: [3, 0], path: [[2, 0], [6, 0]] },
  ],
  stations: [
    { id: "M", name: "M", kind: "machine", servers: 1, input: "Q", output: "C", process_time: { type: "constant", value: 1 }, setup_time: null, failure: null, needs_operator: false, qc: null, pos: [2, 0] },
    { id: "N", name: "N", kind: "machine", servers: 1, input: "C", output: "sink", process_time: { type: "constant", value: 1 }, setup_time: null, failure: null, needs_operator: false, qc: null, pos: [8, 0] },
  ],
  resources: { technicians: null, operators: 0 },
  sink: { pos: [10, 0] },
  run: { horizon: 100, warmup: 0, seed: 1 },
} as unknown as Scenario;

function hello(seq = 1): HelloMsg {
  return {
    type: "hello", session: "s", seq, scenario, overrides: {}, seed: 1, status: "paused", speed: 1, max_speed: 100,
    snapshot: {
      t: 5,
      machines: { "M#1": { state: "processing", product: 2 }, "N#1": { state: "starved", product: null } },
      buffers: { Q: [{ product: 3, ready_at: 4 }], C: [] },
      products: { "2": "A", "3": "A" },
    },
  };
}

const events = (seq: number, evs: EventsMsg["events"]): EventsMsg => ({ type: "events", seq, t: evs.at(-1)?.t ?? 0, events: evs });

describe("LineState", () => {
  it("restores locations from a snapshot", () => {
    const s = new LineState();
    s.load(hello());
    expect(s.products.get(2)?.loc).toEqual({ kind: "machine", id: "M#1" });
    expect(s.products.get(3)?.loc.kind).toBe("buffer");
    expect(s.buffers.get("Q")).toEqual([3]);
    expect(s.machines.get("N#1")?.state).toBe("starved");
  });

  it("moves products through enter / take / exit in time order", () => {
    const s = new LineState();
    s.load(hello());
    s.enqueue(events(2, [
      { type: "enter", t: 6, product: 2, buffer: "C", ready_at: 16 },
      { type: "state", t: 6, machine: "M#1", state: "starved" },
      { type: "take", t: 7, product: 3, machine: "M#1" },
      { type: "take", t: 16, product: 2, machine: "N#1" },
      { type: "exit", t: 17, product: 2, machine: "N#1", outcome: "complete" },
    ]));
    s.applyUntil(6.5);
    expect(s.machines.get("M#1")).toMatchObject({ state: "starved", product: null });
    expect(s.buffers.get("C")).toEqual([2]);
    expect(s.products.get(2)?.loc).toEqual({ kind: "buffer", id: "C", enteredAt: 6, readyAt: 16 });
    expect(s.pendingEvents).toBe(3);

    s.applyUntil(20);
    expect(s.buffers.get("Q")).toEqual([]);
    expect(s.machines.get("M#1")?.product).toBe(3);
    expect(s.products.has(2)).toBe(false);
    expect(s.takeDepartures()).toEqual([{ id: 2, variant: "A", machine: "N#1", outcome: "complete" }]);
    expect(s.takeDepartures()).toEqual([]);
  });

  it("drops stale or duplicate batches", () => {
    const s = new LineState();
    s.load(hello(10));
    s.enqueue(events(9, [{ type: "create", t: 6, product: 99, variant: "A" }]));
    s.enqueue(events(11, [{ type: "create", t: 6, product: 50, variant: "A" }]));
    s.enqueue(events(11, [{ type: "create", t: 6, product: 51, variant: "A" }]));
    s.applyUntil(100);
    expect(s.products.has(99)).toBe(false);
    expect(s.products.has(50)).toBe(true);
    expect(s.products.has(51)).toBe(false);
  });

  it("ignores events for unknown products instead of throwing", () => {
    const s = new LineState();
    s.load(hello());
    s.enqueue(events(2, [{ type: "take", t: 6, product: 12345, machine: "M#1" }]));
    expect(() => s.applyUntil(10)).not.toThrow();
  });

  it("bounds the state timeline to the window", () => {
    const s = new LineState();
    s.load(hello());
    const evs: EventsMsg["events"] = [];
    for (let i = 1; i <= 500; i++) evs.push({ type: "state", t: 5 + i * 60, machine: "N#1", state: i % 2 ? "processing" : "starved" });
    s.enqueue(events(2, evs));
    s.applyUntil(1e9);
    const spans = s.timeline.get("N#1")!;
    expect(spans.length).toBeLessThan(130);
    expect(spans[1]!.t).toBeGreaterThan(spans.at(-1)!.t - 2 * 3600);
  });
});

describe("SimClock", () => {
  it("lags behind the server while playing and never runs ahead", () => {
    const c = new SimClock(0.1);
    c.reset(0, 0);
    c.playing = true;
    c.speed = 100;
    c.sync(50, 1000);
    expect(c.now(1000)).toBeCloseTo(40); // 50 - 0.1 s * 100
    expect(c.now(1050)).toBeCloseTo(45);
    expect(c.now(5000)).toBe(50); // clamped at newest data
  });

  it("is monotonic and jumps to the latest time when paused", () => {
    const c = new SimClock(0.1);
    c.reset(0, 0);
    c.playing = true;
    c.speed = 10;
    c.sync(10, 0);
    const a = c.now(500);
    c.sync(9, 600); // stale sync ignored
    expect(c.now(600)).toBeGreaterThanOrEqual(a);
    c.playing = false;
    expect(c.now(600)).toBe(10);
  });
});
