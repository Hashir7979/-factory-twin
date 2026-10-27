import type { BufferCfg, Point, Scenario, StationCfg } from "../types";

// Pure geometry: where every machine, buffer slot and conveyor position is on
// the floor. Kept free of Three.js so it can be unit tested.

export interface XZ {
  x: number;
  z: number;
}

export const MACHINE_SPACING = 3.2;
export const SLOT_PITCH = 0.95;
export const MAX_DISPLAY_SLOTS = 24;

const add = (a: XZ, b: XZ, k = 1): XZ => ({ x: a.x + b.x * k, z: a.z + b.z * k });
const toXZ = (p: Point): XZ => ({ x: p[0], z: p[1] });

function direction(from: XZ, to: XZ): XZ {
  const dx = to.x - from.x;
  const dz = to.z - from.z;
  const len = Math.hypot(dx, dz);
  return len < 1e-6 ? { x: 1, z: 0 } : { x: dx / len, z: dz / len };
}

const perpendicular = (d: XZ): XZ => ({ x: -d.z, z: d.x });

export class Polyline {
  readonly points: XZ[];
  readonly cumulative: number[];
  readonly length: number;

  constructor(points: XZ[]) {
    if (points.length < 2) throw new Error("polyline needs at least two points");
    this.points = points;
    this.cumulative = [0];
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1]!;
      const b = points[i]!;
      this.cumulative.push(this.cumulative[i - 1]! + Math.hypot(b.x - a.x, b.z - a.z));
    }
    this.length = this.cumulative[this.cumulative.length - 1]!;
  }

  /** Point and heading (radians around +y) at `distance` along the line. */
  at(distance: number): XZ & { angle: number } {
    const d = Math.min(Math.max(distance, 0), this.length);
    let i = 1;
    while (i < this.cumulative.length - 1 && this.cumulative[i]! < d) i++;
    const a = this.points[i - 1]!;
    const b = this.points[i]!;
    const seg = this.cumulative[i]! - this.cumulative[i - 1]!;
    const t = seg > 0 ? (d - this.cumulative[i - 1]!) / seg : 0;
    return { x: a.x + (b.x - a.x) * t, z: a.z + (b.z - a.z) * t, angle: Math.atan2(-(b.z - a.z), b.x - a.x) };
  }
}

export interface MachineSpot extends XZ {
  station: string;
  angle: number;
}

export interface Layout {
  machines: Map<string, MachineSpot>;
  queues: Map<string, { origin: XZ; dir: XZ; perp: XZ; slots: number }>;
  conveyors: Map<string, Polyline>;
  source: XZ;
  sink: XZ;
  scrap: XZ;
  bounds: { minX: number; maxX: number; minZ: number; maxZ: number };
}

export function displaySlots(b: BufferCfg): number {
  return Math.min(b.capacity ?? MAX_DISPLAY_SLOTS, MAX_DISPLAY_SLOTS);
}

export function buildLayout(scenario: Scenario): Layout {
  const bufferPos = new Map(scenario.buffers.map((b) => [b.id, toXZ(b.pos)]));
  const consumer = new Map(scenario.stations.map((s) => [s.input, s]));

  const machines = new Map<string, MachineSpot>();
  for (const st of scenario.stations) {
    const centre = toXZ(st.pos);
    const dir = direction(bufferPos.get(st.input) ?? add(centre, { x: -1, z: 0 }), centre);
    const perp = perpendicular(dir);
    for (let i = 0; i < st.servers; i++) {
      const offset = (i - (st.servers - 1) / 2) * MACHINE_SPACING;
      machines.set(`${st.id}#${i + 1}`, { ...add(centre, perp, offset), station: st.id, angle: Math.atan2(-dir.z, dir.x) });
    }
  }

  const queues: Layout["queues"] = new Map();
  const conveyors = new Map<string, Polyline>();
  for (const b of scenario.buffers) {
    if ((b.kind === "conveyor" || b.kind === "road") && b.path) {
      conveyors.set(b.id, new Polyline(b.path.map(toXZ)));
      continue;
    }
    const origin = toXZ(b.pos);
    const target = consumer.get(b.id);
    const dir = direction(origin, target ? toXZ(target.pos) : add(origin, { x: 1, z: 0 }));
    queues.set(b.id, { origin, dir, perp: perpendicular(dir), slots: displaySlots(b) });
  }

  const qc = scenario.stations.find((s) => s.kind === "qc");
  const scrap = qc ? { x: qc.pos[0], z: qc.pos[1] - 4 } : add(toXZ(scenario.sink.pos), { x: 0, z: -3 });

  const pts: XZ[] = [
    ...machines.values(),
    ...[...queues.values()].map((q) => q.origin),
    ...[...conveyors.values()].flatMap((c) => c.points),
    toXZ(scenario.source.pos),
    toXZ(scenario.sink.pos),
    scrap,
  ];
  const bounds = {
    minX: Math.min(...pts.map((p) => p.x)) - 3,
    maxX: Math.max(...pts.map((p) => p.x)) + 3,
    minZ: Math.min(...pts.map((p) => p.z)) - 3,
    maxZ: Math.max(...pts.map((p) => p.z)) + 3,
  };
  return { machines, queues, conveyors, source: toXZ(scenario.source.pos), sink: toXZ(scenario.sink.pos), scrap, bounds };
}

/** Floor position of the product at queue index `k` (0 = front, next to be picked). */
export function queueSlot(layout: Layout, bufferId: string, k: number): XZ & { stack: number } {
  const q = layout.queues.get(bufferId);
  if (!q) return { x: 0, z: 0, stack: 0 };
  const cols = Math.ceil(q.slots / 2);
  const slot = Math.min(k, q.slots - 1);
  const col = Math.floor(slot / 2);
  const row = slot % 2;
  const along = ((cols - 1) / 2 - col) * SLOT_PITCH;
  const across = q.slots > 1 ? (row - 0.5) * SLOT_PITCH : 0;
  const p = add(add(q.origin, q.dir, along), q.perp, across);
  // Parts beyond the displayed slots (unbounded queues) stack on the last slot.
  return { ...p, stack: Math.max(0, k - (q.slots - 1)) };
}

/**
 * Position along a conveyor: a part travels from the start at belt speed and
 * accumulates behind the parts in front of it (index `k`, 0 = front).
 */
export function conveyorDistance(
  line: Polyline,
  capacity: number,
  k: number,
  enteredAt: number,
  readyAt: number,
  now: number,
): number {
  const pitch = line.length / Math.max(1, capacity);
  const maxDist = Math.max(0, line.length - (k + 0.5) * pitch);
  const travel = readyAt > enteredAt ? Math.min(1, Math.max(0, (now - enteredAt) / (readyAt - enteredAt))) : 1;
  return Math.min(travel * line.length, maxDist);
}

/** Spacing between queued trucks on a road, in metres of road. */
export const TRUCK_PITCH = 3.2;

/** Slots used to lay out entities along a conveyor or road. */
export function travelSlots(b: BufferCfg, line: Polyline): number {
  const capacity = b.capacity ?? 1;
  // Trucks are long: never squeeze more onto a road than fit bumper to bumper.
  return b.kind === "road" ? Math.max(1, Math.min(capacity, Math.floor(line.length / TRUCK_PITCH))) : capacity;
}

/**
 * Where an entity sits while a station works on it. Parts sit on top of a
 * machine; trucks park beside the excavator (local +z) or on the dump pad.
 */
export function productAnchor(model: StationCfg["model"], spot: MachineSpot): XZ & { y: number; angle: number } {
  if (model === "excavator") {
    const d = 3.4;
    return { x: spot.x + d * Math.sin(spot.angle), z: spot.z + d * Math.cos(spot.angle), y: 0, angle: spot.angle };
  }
  if (model === "dump") return { x: spot.x, z: spot.z, y: 0.06, angle: spot.angle + Math.PI };
  return { x: spot.x, z: spot.z, y: 1.42, angle: spot.angle };
}
