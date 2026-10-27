import { describe, expect, it } from "vitest";
import type { Economics, Scenario } from "../types";
import { economicsFields } from "./economics";

const econ: Economics = {
  margin_per_part: 99,
  machine_cost_per_hour: 0,
  station_machine_cost_per_hour: { load: 165, dump: 95 },
  buffer_slot_cost_per_hour: 0,
  technician_cost_per_hour: 55,
  operator_cost_per_hour: 0,
  fleet_cost_per_hour: 88,
};

describe("economicsFields", () => {
  it("shows per-station and truck costs for a fleet scenario", () => {
    const sc = {
      output_station: "dump",
      entity: "truck",
      stations: [
        { id: "load", name: "Excavator", needs_operator: false },
        { id: "dump", name: "Dump", needs_operator: false },
      ],
      resources: { technicians: 1, operators: 0 },
    } as unknown as Scenario;
    const keys = economicsFields(sc, econ).map(([k]) => k);
    expect(keys).toEqual(["margin_per_part", "station:load", "station:dump", "technician_cost_per_hour", "fleet_cost_per_hour"]);
    expect(economicsFields(sc, econ)[0]![1]).toMatch(/per load/);
  });

  it("keeps the generic production-line fields otherwise", () => {
    const sc = {
      output_station: null,
      entity: "part",
      stations: [{ id: "weld", name: "Weld", needs_operator: true }],
      resources: { technicians: 1, operators: 2 },
    } as unknown as Scenario;
    const keys = economicsFields(sc, { ...econ, station_machine_cost_per_hour: {} }).map(([k]) => k);
    expect(keys).toEqual([
      "margin_per_part",
      "machine_cost_per_hour",
      "buffer_slot_cost_per_hour",
      "technician_cost_per_hour",
      "operator_cost_per_hour",
    ]);
  });
});
