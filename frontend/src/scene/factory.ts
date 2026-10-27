import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import type { LineState } from "../sim/state";
import type { MachineState, Scenario, StationCfg } from "../types";
import { type Layout, buildLayout, conveyorDistance, productAnchor, queueSlot, travelSlots } from "./layout";
import { type ModelParts, dumpSite, excavator, roadSegment, truckGeometry } from "./models";

// Colour convention follows common DES tools (green = working, yellow = blocked, red = failed).
export const STATE_COLORS: Record<MachineState, string> = {
  processing: "#22c55e",
  setup: "#06b6d4",
  starved: "#94a3b8",
  waiting_operator: "#a855f7",
  blocked: "#eab308",
  down: "#ef4444",
};

export const STATE_LABELS: Record<MachineState, string> = {
  processing: "Working",
  setup: "Setup",
  starved: "Starved",
  waiting_operator: "Wait operator",
  blocked: "Blocked",
  down: "Down",
};

export type Pick = { kind: "machine"; id: string } | { kind: "buffer"; id: string } | null;

const PRODUCT_Y = { queue: 0.2, conveyor: 0.98, road: 0.06, machine: 1.42, stack: 0.34 };
const LEAVE_SECONDS = 0.7;
const INITIAL_INSTANCES = 256;

interface MachineVisual {
  group: THREE.Group;
  lamp: THREE.MeshStandardMaterial;
  ring: THREE.MeshBasicMaterial;
  label: HTMLElement;
  state: MachineState | null;
  swing?: THREE.Object3D | undefined;
}

interface ProductVisual {
  pos: THREE.Vector3;
  angle: number;
  seen: number;
}

interface Leaving {
  id: number;
  variant: string;
  from: THREE.Vector3;
  to: THREE.Vector3;
  age: number;
}

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

export class FactoryScene {
  onPick: (pick: Pick) => void = () => {};

  private readonly renderer: THREE.WebGLRenderer;
  private readonly labels: CSS2DRenderer;
  private readonly scene = new THREE.Scene();
  private readonly camera = new THREE.PerspectiveCamera(45, 1, 0.1, 500);
  private readonly controls: OrbitControls;
  private readonly root = new THREE.Group();
  private readonly raycaster = new THREE.Raycaster();
  private readonly resizeObserver: ResizeObserver;
  private readonly reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  private layout: Layout | null = null;
  private scenario: Scenario | null = null;
  private machineVisuals = new Map<string, MachineVisual>();
  private bufferLabels = new Map<string, HTMLElement>();
  private pickables: THREE.Object3D[] = [];
  private products: THREE.InstancedMesh;
  private productVisuals = new Map<number, ProductVisual>();
  private leaving: Leaving[] = [];
  private variantColors = new Map<string, THREE.Color>();
  private selection: THREE.Mesh;
  private frame = 0;
  private elapsed = 0;
  private stationModels = new Map<string, StationCfg["model"]>();
  private entity: Scenario["entity"] = "part";
  private readonly tmpMatrix = new THREE.Matrix4();
  private readonly tmpQuat = new THREE.Quaternion();
  private readonly tmpScale = new THREE.Vector3(1, 1, 1);
  private readonly up = new THREE.Vector3(0, 1, 0);

  constructor(private readonly container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.renderer.domElement.setAttribute("aria-hidden", "true");
    container.appendChild(this.renderer.domElement);

    this.labels = new CSS2DRenderer();
    this.labels.domElement.className = "scene-labels";
    container.appendChild(this.labels.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.47;
    this.controls.minDistance = 6;
    this.controls.maxDistance = 140;

    this.scene.add(this.root);
    this.addLights();

    this.products = this.makeProductMesh(INITIAL_INSTANCES);
    this.scene.add(this.products);

    this.selection = new THREE.Mesh(
      new THREE.RingGeometry(1.75, 2.0, 48),
      new THREE.MeshBasicMaterial({ color: "#60a5fa", side: THREE.DoubleSide, transparent: true, opacity: 0.9 }),
    );
    this.selection.rotation.x = -Math.PI / 2;
    this.selection.position.y = 0.03;
    this.selection.visible = false;
    this.scene.add(this.selection);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.bindPointer();
    this.applyTheme();
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.applyTheme());
    this.resize();
  }

  // ------------------------------------------------------------------------- build

  build(scenario: Scenario): void {
    this.clear();
    this.scenario = scenario;
    if (scenario.entity !== this.entity) {
      // Trucks and parts use different instanced geometry.
      this.entity = scenario.entity;
      this.replaceProductMesh(this.products.instanceMatrix.count);
    }
    this.stationModels = new Map(scenario.stations.map((s) => [s.id, s.model ?? "machine"]));
    const layout = buildLayout(scenario);
    this.layout = layout;
    this.variantColors = new Map(scenario.variants.map((v) => [v.id, new THREE.Color(v.color)]));

    const { minX, maxX, minZ, maxZ } = layout.bounds;
    const size = Math.max(maxX - minX, maxZ - minZ) + 12;
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(size, size),
      new THREE.MeshStandardMaterial({ color: cssVar("--floor", "#1f2937"), roughness: 0.95 }),
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set((minX + maxX) / 2, 0, (minZ + maxZ) / 2);
    floor.receiveShadow = true;
    floor.name = "floor";
    this.root.add(floor);
    const grid = new THREE.GridHelper(size, Math.round(size / 2), 0x475569, 0x334155);
    grid.position.set((minX + maxX) / 2, 0.005, (minZ + maxZ) / 2);
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.35;
    this.root.add(grid);

    const stationById = new Map(scenario.stations.map((s) => [s.id, s]));
    for (const [id, spot] of layout.machines) {
      const st = stationById.get(spot.station)!;
      const model = st.model ?? "machine";
      const title = st.servers > 1 ? `${st.name} ${id.split("#")[1]}` : st.name;
      const visual =
        model === "machine" ? this.makeMachine(id, st.name, st.kind === "qc", st.servers > 1) : this.makeModel(id, title, model === "excavator" ? excavator() : dumpSite());
      visual.group.position.set(spot.x, 0, spot.z);
      visual.group.rotation.y = spot.angle;
      this.root.add(visual.group);
      this.machineVisuals.set(id, visual);
    }

    for (const b of scenario.buffers) {
      const line = layout.conveyors.get(b.id);
      if (line && b.kind === "road") this.makeRoad(b.id, b.name, line.points);
      else if (line) this.makeConveyor(b.id, b.name, line.points);
      else this.makeQueueArea(b.id, b.name);
    }
    const closed = !scenario.stations.some((s) => s.output === "sink");
    this.makeLandmark(layout.source, scenario.source.name, "#64748b", closed ? 0.15 : 2.2);
    if (!closed) {
      // Closed loops (e.g. truck fleets) have no shipping or scrap area.
      this.makeLandmark(layout.sink, "Shipping", "#0f766e", 1.4);
      this.makeLandmark(layout.scrap, "Scrap", "#b91c1c", 0.9);
    }

    this.fitCamera();
  }

  private clear(): void {
    for (const obj of [...this.root.children]) {
      this.root.remove(obj);
      obj.traverse((o) => {
        if (o instanceof CSS2DObject) o.element.remove();
        if (o instanceof THREE.Mesh) {
          o.geometry.dispose();
          const mats = Array.isArray(o.material) ? o.material : [o.material];
          mats.forEach((m) => m.dispose());
        }
      });
    }
    this.machineVisuals.clear();
    this.bufferLabels.clear();
    this.pickables = [];
    this.productVisuals.clear();
    this.leaving = [];
    this.selection.visible = false;
  }

  private label(text: string, cls: string): { obj: CSS2DObject; el: HTMLElement } {
    const el = document.createElement("div");
    el.className = `scene-label ${cls}`;
    el.textContent = text;
    return { obj: new CSS2DObject(el), el };
  }

  private makeMachine(id: string, name: string, isQc: boolean, showIndex: boolean): MachineVisual {
    const group = new THREE.Group();
    group.userData = { pick: { kind: "machine", id } };
    // Low metalness: without an environment map, metallic surfaces render almost black.
    const steel = new THREE.MeshStandardMaterial({ color: isQc ? "#8aa0b8" : "#a3b1c2", metalness: 0.15, roughness: 0.55 });
    const dark = new THREE.MeshStandardMaterial({ color: "#334155", metalness: 0.1, roughness: 0.7 });

    const base = new THREE.Mesh(new THREE.BoxGeometry(2.6, 0.25, 2.2), dark);
    base.position.y = 0.125;
    const body = new THREE.Mesh(new THREE.BoxGeometry(2.2, 1.0, 1.8), steel);
    body.position.y = 0.75;
    const gantry = new THREE.Mesh(new THREE.BoxGeometry(0.25, 1.4, 1.9), steel);
    gantry.position.set(-0.85, 1.95, 0);
    const beam = new THREE.Mesh(new THREE.BoxGeometry(1.9, 0.22, 0.3), steel);
    beam.position.set(0.05, 2.55, 0);
    const lampMat = new THREE.MeshStandardMaterial({ color: "#94a3b8", emissive: "#94a3b8", emissiveIntensity: 0.9 });
    const lamp = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.16, 0.5, 16), lampMat);
    lamp.position.set(-0.85, 2.9, 0.7);
    const ringMat = new THREE.MeshBasicMaterial({ color: "#94a3b8", transparent: true, opacity: 0.45 });
    const ring = new THREE.Mesh(new THREE.PlaneGeometry(3.0, 2.6), ringMat);
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.012;

    for (const m of [base, body, gantry, beam, lamp]) {
      m.castShadow = true;
      m.receiveShadow = true;
      m.userData = group.userData;
      this.pickables.push(m);
    }
    group.add(base, body, gantry, beam, lamp, ring);

    const { obj, el } = this.label(showIndex ? `${name} ${id.split("#")[1]}` : name, "machine-label");
    obj.position.set(0, 3.5, 0);
    group.add(obj);
    return { group, lamp: lampMat, ring: ringMat, label: el, state: null };
  }

  /** Earthmoving equipment built in models.ts, wired up like a machine. */
  private makeModel(id: string, title: string, parts: ModelParts): MachineVisual {
    parts.group.userData = { pick: { kind: "machine", id } };
    for (const m of parts.meshes) {
      m.userData = parts.group.userData;
      this.pickables.push(m);
    }
    const { obj, el } = this.label(title, "machine-label");
    obj.position.set(0, parts.labelHeight, 0);
    parts.group.add(obj);
    return { group: parts.group, lamp: parts.lamp, ring: parts.ring, label: el, state: null, swing: parts.swing };
  }

  private makeRoad(id: string, name: string, points: { x: number; z: number }[]): void {
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1]!;
      const b = points[i]!;
      const { group, surface } = roadSegment(Math.hypot(b.x - a.x, b.z - a.z));
      group.position.set((a.x + b.x) / 2, 0, (a.z + b.z) / 2);
      group.rotation.y = Math.atan2(-(b.z - a.z), b.x - a.x);
      surface.userData = { pick: { kind: "buffer", id } };
      this.pickables.push(surface);
      this.root.add(group);
    }
    const mid = points[Math.floor((points.length - 1) / 2)]!;
    const next = points[Math.floor((points.length - 1) / 2) + 1]!;
    const { obj, el } = this.label(name, "buffer-label");
    obj.position.set((mid.x + next.x) / 2, 1.2, (mid.z + next.z) / 2);
    this.root.add(obj);
    this.bufferLabels.set(id, el);
  }

  private makeConveyor(id: string, name: string, points: { x: number; z: number }[]): void {
    const belt = new THREE.MeshStandardMaterial({ color: "#111827", roughness: 0.8 });
    const rail = new THREE.MeshStandardMaterial({ color: "#9ca3af", metalness: 0.6, roughness: 0.4 });
    const pick = { pick: { kind: "buffer", id } };
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1]!;
      const b = points[i]!;
      const len = Math.hypot(b.x - a.x, b.z - a.z);
      const seg = new THREE.Group();
      seg.position.set((a.x + b.x) / 2, 0, (a.z + b.z) / 2);
      seg.rotation.y = Math.atan2(-(b.z - a.z), b.x - a.x);
      const top = new THREE.Mesh(new THREE.BoxGeometry(len, 0.12, 1.0), belt);
      top.position.y = 0.76;
      top.receiveShadow = true;
      for (const side of [-0.56, 0.56]) {
        const r = new THREE.Mesh(new THREE.BoxGeometry(len, 0.14, 0.08), rail);
        r.position.set(0, 0.86, side);
        r.castShadow = true;
        seg.add(r);
      }
      const legs = Math.max(2, Math.round(len / 2));
      for (let k = 0; k <= legs; k++) {
        const leg = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.7, 0.9), rail);
        leg.position.set(-len / 2 + (k * len) / legs, 0.35, 0);
        seg.add(leg);
      }
      top.userData = pick;
      this.pickables.push(top);
      seg.add(top);
      this.root.add(seg);
    }
    const mid = points[Math.floor(points.length / 2) - (points.length % 2 === 0 ? 1 : 0)]!;
    const end = points[points.length - 1]!;
    const { obj, el } = this.label(name, "buffer-label");
    obj.position.set((mid.x + end.x) / 2, 1.6, (mid.z + end.z) / 2);
    this.root.add(obj);
    this.bufferLabels.set(id, el);
  }

  private makeQueueArea(id: string, name: string): void {
    const q = this.layout!.queues.get(id)!;
    const cols = Math.ceil(q.slots / 2);
    const mat = new THREE.Mesh(
      new THREE.PlaneGeometry(cols * 0.95 + 0.4, (q.slots > 1 ? 2 : 1) * 0.95 + 0.4),
      new THREE.MeshStandardMaterial({ color: "#334155", roughness: 0.9 }),
    );
    mat.rotation.x = -Math.PI / 2;
    mat.rotation.z = Math.atan2(-q.dir.z, q.dir.x);
    mat.position.set(q.origin.x, 0.02, q.origin.z);
    mat.receiveShadow = true;
    mat.userData = { pick: { kind: "buffer", id } };
    this.pickables.push(mat);
    this.root.add(mat);
    const { obj, el } = this.label(name, "buffer-label");
    obj.position.set(q.origin.x, 1.3, q.origin.z);
    this.root.add(obj);
    this.bufferLabels.set(id, el);
  }

  private makeLandmark(at: { x: number; z: number }, name: string, color: string, height: number): void {
    const m = new THREE.Mesh(
      new THREE.BoxGeometry(2.2, height, 2.2),
      new THREE.MeshStandardMaterial({ color, roughness: 0.7, transparent: true, opacity: 0.85 }),
    );
    m.position.set(at.x, height / 2, at.z);
    m.castShadow = true;
    this.root.add(m);
    const { obj } = this.label(name, "landmark-label");
    obj.position.set(at.x, height + 0.6, at.z);
    this.root.add(obj);
  }

  private makeProductMesh(capacity: number): THREE.InstancedMesh {
    const truck = this.entity === "truck";
    const mesh = new THREE.InstancedMesh(
      truck ? truckGeometry() : new THREE.BoxGeometry(0.72, 0.3, 0.56),
      new THREE.MeshStandardMaterial({ roughness: truck ? 0.6 : 0.4, metalness: 0.2, vertexColors: truck }),
      capacity,
    );
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.castShadow = true;
    mesh.count = 0;
    mesh.frustumCulled = false;
    return mesh;
  }

  private ensureCapacity(n: number): void {
    if (n <= this.products.instanceMatrix.count) return;
    let cap = this.products.instanceMatrix.count;
    while (cap < n) cap *= 2;
    this.replaceProductMesh(cap);
  }

  private replaceProductMesh(capacity: number): void {
    this.scene.remove(this.products);
    this.products.geometry.dispose();
    (this.products.material as THREE.Material).dispose();
    this.products.dispose();
    this.products = this.makeProductMesh(capacity);
    this.scene.add(this.products);
  }

  private addLights(): void {
    this.scene.add(new THREE.HemisphereLight(0xe2e8f0, 0x334155, 1.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(20, 40, 25);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    const s = sun.shadow.camera;
    s.left = -45;
    s.right = 45;
    s.top = 30;
    s.bottom = -30;
    s.far = 120;
    this.scene.add(sun);
  }

  private applyTheme(): void {
    this.scene.background = new THREE.Color(cssVar("--scene-bg", "#0b1220"));
    const floor = this.root.getObjectByName("floor") as THREE.Mesh | undefined;
    if (floor) (floor.material as THREE.MeshStandardMaterial).color.set(cssVar("--floor", "#1f2937"));
  }

  // ------------------------------------------------------------------------- camera / input

  fitCamera(): void {
    if (!this.layout) return;
    const { minX, maxX, minZ, maxZ } = this.layout.bounds;
    const cx = (minX + maxX) / 2;
    const cz = (minZ + maxZ) / 2;
    // Three-quarter view from the front; binary-search the distance at which every
    // corner of the layout's bounding box (floor to machine top) is on screen.
    const dir = new THREE.Vector3(-0.1, 0.55, 0.8).normalize();
    const target = new THREE.Vector3(cx, 0, cz);
    const corners: THREE.Vector3[] = [];
    for (const x of [minX, maxX]) for (const z of [minZ, maxZ]) for (const y of [0, 3.5]) corners.push(new THREE.Vector3(x, y, z));
    const fits = (d: number): boolean => {
      this.camera.position.copy(target).addScaledVector(dir, d);
      this.camera.lookAt(target);
      this.camera.updateMatrixWorld();
      return corners.every((c) => {
        const p = c.clone().project(this.camera);
        return Math.abs(p.x) <= 0.96 && Math.abs(p.y) <= 0.9 && p.z < 1;
      });
    };
    let lo = 2;
    let hi = 400;
    for (let i = 0; i < 30; i++) {
      const mid = (lo + hi) / 2;
      if (fits(mid)) hi = mid;
      else lo = mid;
    }
    this.camera.position.copy(target).addScaledVector(dir, hi);
    this.controls.target.copy(target);
    this.controls.update();
  }

  private resize(): void {
    const w = this.container.clientWidth || 1;
    const h = this.container.clientHeight || 1;
    // Small viewports cannot fit every label; show machine labels only.
    this.container.classList.toggle("compact", w < 700);
    this.renderer.setSize(w, h, false);
    this.labels.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  private bindPointer(): void {
    const el = this.renderer.domElement;
    let down: { x: number; y: number } | null = null;
    el.addEventListener("pointerdown", (e) => (down = { x: e.clientX, y: e.clientY }));
    el.addEventListener("pointerup", (e) => {
      if (!down) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
      down = null;
      if (moved < 6) this.onPick(this.pickAt(e.clientX, e.clientY));
    });
    let pending = false;
    el.addEventListener("pointermove", (e) => {
      if (pending || e.buttons) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        el.style.cursor = this.pickAt(e.clientX, e.clientY) ? "pointer" : "grab";
      });
    });
  }

  private pickAt(clientX: number, clientY: number): Pick {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const hit = this.raycaster.intersectObjects(this.pickables, false)[0];
    return (hit?.object.userData.pick as Pick) ?? null;
  }

  setSelection(pick: Pick): void {
    if (pick?.kind === "machine") {
      const spot = this.layout?.machines.get(pick.id);
      if (spot) {
        this.selection.position.set(spot.x, 0.03, spot.z);
        this.selection.visible = true;
        return;
      }
    }
    this.selection.visible = false;
  }

  // ------------------------------------------------------------------------- per frame

  update(state: LineState, now: number, dt: number): void {
    if (!this.layout || !this.scenario) return;
    this.frame++;
    this.elapsed += dt;
    for (const [id, m] of state.machines) {
      const v = this.machineVisuals.get(id);
      if (v?.swing) {
        // Excavator swings between the dig face and the truck while loading.
        const target = m.state === "processing" && !this.reducedMotion.matches ? -(Math.PI / 4) * (1 - Math.cos(this.elapsed * 2.4)) : 0;
        v.swing.rotation.y += (target - v.swing.rotation.y) * Math.min(1, dt * 8);
      }
      if (!v || v.state === m.state) continue;
      v.state = m.state;
      const c = STATE_COLORS[m.state];
      v.lamp.color.set(c);
      v.lamp.emissive.set(c);
      v.ring.color.set(c);
      v.label.dataset.state = m.state;
      v.label.title = STATE_LABELS[m.state];
    }
    // Labels are DOM; refreshing them a few times a second is plenty.
    if (this.frame % 10 === 0) {
      for (const b of this.scenario.buffers) {
        const el = this.bufferLabels.get(b.id);
        const n = state.buffers.get(b.id)?.length ?? 0;
        if (el) el.textContent = `${b.name} · ${n}${b.capacity ? `/${b.capacity}` : ""}`;
      }
    }
    this.updateProducts(state, now, dt);
  }

  private productTarget(state: LineState, id: number, now: number): { x: number; y: number; z: number; angle: number } {
    const layout = this.layout!;
    const p = state.products.get(id)!;
    const loc = p.loc;
    if (loc.kind === "machine") {
      const m = layout.machines.get(loc.id);
      if (m) return productAnchor(this.stationModels.get(m.station) ?? "machine", m);
    } else if (loc.kind === "buffer") {
      const k = state.buffers.get(loc.id)?.indexOf(id) ?? 0;
      const line = layout.conveyors.get(loc.id);
      if (line) {
        const cfg = this.scenario!.buffers.find((b) => b.id === loc.id)!;
        const d = conveyorDistance(line, travelSlots(cfg, line), Math.max(0, k), loc.enteredAt, loc.readyAt, now);
        const at = line.at(d);
        return { x: at.x, y: cfg.kind === "road" ? PRODUCT_Y.road : PRODUCT_Y.conveyor, z: at.z, angle: at.angle };
      }
      const slot = queueSlot(layout, loc.id, Math.max(0, k));
      const q = layout.queues.get(loc.id)!;
      return { x: slot.x, y: PRODUCT_Y.queue + slot.stack * PRODUCT_Y.stack, z: slot.z, angle: Math.atan2(-q.dir.z, q.dir.x) };
    }
    return { x: layout.source.x, y: 2.4, z: layout.source.z, angle: 0 };
  }

  private updateProducts(state: LineState, now: number, dt: number): void {
    const layout = this.layout!;
    const snap = this.reducedMotion.matches;
    // Exponential smoothing: frame-rate independent, ~95% of the way in 0.3 s.
    const k = snap ? 1 : 1 - Math.exp(-dt * 10);

    for (const dep of state.takeDepartures()) {
      const from = this.productVisuals.get(dep.id)?.pos.clone() ?? new THREE.Vector3(layout.sink.x, 1, layout.sink.z);
      const dest = dep.outcome === "scrap" ? layout.scrap : layout.sink;
      this.leaving.push({ id: dep.id, variant: dep.variant, from, to: new THREE.Vector3(dest.x, 1.2, dest.z), age: 0 });
      this.productVisuals.delete(dep.id);
    }
    this.leaving = this.leaving.filter((l) => (l.age += dt) < LEAVE_SECONDS);

    this.ensureCapacity(state.products.size + this.leaving.length);
    const mesh = this.products;
    let i = 0;
    for (const id of state.products.keys()) {
      const target = this.productTarget(state, id, now);
      let v = this.productVisuals.get(id);
      if (!v) {
        v = { pos: new THREE.Vector3(target.x, target.y, target.z), angle: target.angle, seen: this.frame };
        if (state.products.get(id)!.loc.kind === "buffer") v.pos.set(layout.source.x, 2.4, layout.source.z);
        this.productVisuals.set(id, v);
      }
      v.seen = this.frame;
      v.pos.x += (target.x - v.pos.x) * k;
      v.pos.y += (target.y - v.pos.y) * k;
      v.pos.z += (target.z - v.pos.z) * k;
      v.angle = target.angle;
      this.tmpQuat.setFromAxisAngle(this.up, v.angle);
      this.tmpMatrix.compose(v.pos, this.tmpQuat, this.tmpScale);
      mesh.setMatrixAt(i, this.tmpMatrix);
      mesh.setColorAt(i, this.variantColors.get(state.products.get(id)!.variant) ?? new THREE.Color("#ffffff"));
      i++;
    }
    for (const l of this.leaving) {
      const t = l.age / LEAVE_SECONDS;
      const pos = l.from.clone().lerp(l.to, snap ? 1 : t);
      const s = Math.max(0.05, 1 - t * t);
      this.tmpQuat.identity();
      this.tmpMatrix.compose(pos, this.tmpQuat, new THREE.Vector3(s, s, s));
      mesh.setMatrixAt(i, this.tmpMatrix);
      mesh.setColorAt(i, this.variantColors.get(l.variant) ?? new THREE.Color("#ffffff"));
      i++;
    }
    // Forget visuals for products removed by a reset/snapshot.
    if (this.frame % 60 === 0) {
      for (const [id, v] of this.productVisuals) if (v.seen < this.frame) this.productVisuals.delete(id);
    }
    mesh.count = i;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }

  render(): void {
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }

  /** Discard per-product visuals (after a reset the ids start over). */
  resetProducts(): void {
    this.productVisuals.clear();
    this.leaving = [];
  }

  dispose(): void {
    this.resizeObserver.disconnect();
    this.clear();
    this.renderer.dispose();
  }
}
