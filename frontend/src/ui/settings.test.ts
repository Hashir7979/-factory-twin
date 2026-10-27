import { describe, expect, it } from "vitest";
import type { Scenario } from "../types";
import { diffOverrides, overrideFields, validate } from "./settings";

const scenario = {
  stations: [{ id: "weld", name: "Weld", servers: 1, needs_operator: true }],
  buffers: [{ id: "B2", name: "B2", capacity: 6 }, { id: "U", name: "U", capacity: null }],
  resources: { technicians: 1, operators: 2 },
  run: { seed: 1 },
} as unknown as Scenario;

describe("what-if settings", () => {
  const fields = overrideFields(scenario);

  it("offers only overridable fields", () => {
    expect(fields.map((f) => f.key)).toEqual([
      "station.weld.servers",
      "buffer.B2.capacity",
      "resources.technicians",
      "resources.operators",
      "source.interarrival_scale",
    ]);
  });

  it("validates ranges and integers", () => {
    const servers = fields[0]!;
    expect(validate(servers, "2")).toBeNull();
    expect(validate(servers, "")).toBe("Required");
    expect(validate(servers, "1.5")).toMatch(/whole/);
    expect(validate(servers, "99")).toMatch(/Between/);
    expect(validate(fields.at(-1)!, "1.25")).toBeNull();
  });

  it("sends only values that differ from the base", () => {
    const values = new Map(fields.map((f) => [f.key, f.base]));
    values.set("station.weld.servers", 2);
    expect(diffOverrides(fields, values)).toEqual({ "station.weld.servers": 2 });
  });
});
