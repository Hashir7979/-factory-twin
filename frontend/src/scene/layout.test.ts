import { describe, expect, it } from "vitest";
import type { Scenario } from "../types";
import { Polyline, buildLayout, conveyorDistance, queueSlot } from "./layout";

describe("Polyline", () => {
  it("measures and interpolates multi-segment paths", () => {
    const p = new Polyline([{ x: 0, z: 0 }, { x: 3, z: 0 }, { x: 3, z: 4 }]);
    expect(p.length).toBe(7);
    expect(p.at(1.5)).toMatchObject({ x: 1.5, z: 0 });
    expect(p.at(5)).toMatchObject({ x: 3, z: 2 });
    expect(p.at(-5)).toMatchObject({ x: 0, z: 0 });
    expect(p.at(99)).toMatchObject({ x: 3, z: 4 });
  });

  it("rejects degenerate input", () => {
    expect(() => new Polyline([{ x: 0, z: 0 }])).toThrow();
  });
});

describe("conveyorDistance", () => {
  const line = new Polyline([{ x: 0, z: 0 }, { x: 8, z: 0 }]);
  it("travels at belt speed until the accumulation point", () => {
    expect(conveyorDistance(line, 4, 0, 0, 10, 5)).toBeCloseTo(4);
    expect(conveyorDistance(line, 4, 0, 0, 10, 10)).toBeCloseTo(7); // front slot: 8 - 0.5 * 2
    expect(conveyorDistance(line, 4, 2, 0, 10, 10)).toBeCloseTo(3); // third in line queues behind
  });
  it("never returns a negative distance", () => {
    expect(conveyorDistance(line, 2, 10, 0, 10, 10)).toBe(0);
  });
});

describe("buildLayout", () => {
  const scenario = {
    stations: [{ id: "S", input: "Q", servers: 3, pos: [10, 0], kind: "machine" }],
    buffers: [{ id: "Q", capacity: 6, kind: "queue", pos: [5, 0], path: null }],
    source: { pos: [0, 0] },
    sink: { pos: [15, 0] },
  } as unknown as Scenario;

  it("spreads parallel machines perpendicular to the flow", () => {
    const layout = buildLayout(scenario);
    const zs = ["S#1", "S#2", "S#3"].map((id) => layout.machines.get(id)!.z);
    expect(zs[1]).toBeCloseTo(0);
    expect(zs[0]).toBeCloseTo(-zs[2]!);
    expect(new Set(["S#1", "S#2", "S#3"].map((id) => layout.machines.get(id)!.x))).toEqual(new Set([10]));
  });

  it("puts the front of a queue nearest its consumer", () => {
    const layout = buildLayout(scenario);
    expect(queueSlot(layout, "Q", 0).x).toBeGreaterThan(queueSlot(layout, "Q", 5).x);
    expect(queueSlot(layout, "Q", 8).stack).toBe(3); // overflow stacks on the last slot
  });
});
