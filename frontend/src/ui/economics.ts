import type { Economics, Scenario } from "../types";

// Pure helpers for the optimizer's cost form (no DOM or chart imports, so unit-testable).

export type EconScalar = Exclude<keyof Economics, "station_machine_cost_per_hour">;

/** Cost inputs relevant to this scenario; station-specific machine costs use "station:<id>" keys. */
export function economicsFields(sc: Scenario, e: Economics): [key: string, label: string, value: number][] {
  const closed = sc.output_station !== null && sc.output_station !== undefined;
  const fields: [string, string, number][] = [
    ["margin_per_part", closed ? `Revenue per ${sc.entity === "truck" ? "load" : "cycle"} (€)` : "Margin per good part (€)", e.margin_per_part],
  ];
  const perStation = e.station_machine_cost_per_hour ?? {};
  for (const st of sc.stations) {
    if (st.id in perStation) fields.push([`station:${st.id}`, `${st.name} (€/h per machine)`, perStation[st.id]!]);
  }
  if (sc.stations.some((st) => !(st.id in perStation))) {
    fields.push(["machine_cost_per_hour", Object.keys(perStation).length ? "Other machines (€/h each)" : "Machine cost (€/h each)", e.machine_cost_per_hour]);
  }
  if (!closed) fields.push(["buffer_slot_cost_per_hour", "Buffer slot cost (€/h)", e.buffer_slot_cost_per_hour]);
  if (sc.resources.technicians !== null) fields.push(["technician_cost_per_hour", "Technician cost (€/h)", e.technician_cost_per_hour]);
  if (sc.stations.some((st) => st.needs_operator)) fields.push(["operator_cost_per_hour", "Operator cost (€/h)", e.operator_cost_per_hour]);
  if (closed) {
    const noun = sc.entity === "truck" ? "Truck cost (€/h each, incl. driver)" : "Fleet unit cost (€/h each)";
    fields.push(["fleet_cost_per_hour", noun, e.fleet_cost_per_hour ?? 0]);
  }
  return fields;
}
