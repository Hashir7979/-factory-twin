import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";

// Procedural low-poly models for the earthmoving scenario. Kept separate from the
// scene so FactoryScene only decides *what* to place, not how it is built.

function tinted(geometry: THREE.BufferGeometry, color: string): THREE.BufferGeometry {
  const c = new THREE.Color(color);
  const n = geometry.getAttribute("position").count;
  const colors = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) colors.set([c.r, c.g, c.b], i * 3);
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  return geometry;
}

function placed(geometry: THREE.BufferGeometry, x: number, y: number, z: number): THREE.BufferGeometry {
  geometry.translate(x, y, z);
  return geometry;
}

/**
 * Dump truck facing +x, wheels on y = 0. Vertex colours are white for painted
 * parts and dark for wheels/chassis, so the per-instance colour tints only the
 * body (instance colour multiplies vertex colour).
 */
export function truckGeometry(): THREE.BufferGeometry {
  const body = "#ffffff";
  const dark = "#1f2937";
  const parts = [
    tinted(placed(new THREE.BoxGeometry(2.5, 0.22, 1.0), 0, 0.42, 0), dark), // chassis
    tinted(placed(new THREE.BoxGeometry(1.6, 0.75, 1.2), -0.4, 0.92, 0), body), // dump bed
    tinted(placed(new THREE.BoxGeometry(0.75, 0.85, 1.05), 0.9, 0.95, 0), body), // cab
    tinted(placed(new THREE.BoxGeometry(0.06, 0.4, 0.9), 1.28, 1.05, 0), "#93c5fd"), // windscreen
  ];
  for (const x of [-0.85, 0.85]) {
    for (const z of [-0.5, 0.5]) {
      const wheel = new THREE.CylinderGeometry(0.3, 0.3, 0.22, 12);
      wheel.rotateX(Math.PI / 2);
      parts.push(tinted(placed(wheel, x, 0.3, z), dark));
    }
  }
  const merged = mergeGeometries(parts.map((g) => g.toNonIndexed()));
  parts.forEach((g) => g.dispose());
  return merged;
}

export interface ModelParts {
  group: THREE.Group;
  meshes: THREE.Mesh[]; // pickable / shadow-casting meshes
  lamp: THREE.MeshStandardMaterial;
  ring: THREE.MeshBasicMaterial;
  swing?: THREE.Object3D; // animated while working (excavator upper body)
  labelHeight: number;
}

function statusParts(ringW: number, ringD: number): { lamp: THREE.MeshStandardMaterial; ring: THREE.MeshBasicMaterial; ringMesh: THREE.Mesh } {
  const lamp = new THREE.MeshStandardMaterial({ color: "#94a3b8", emissive: "#94a3b8", emissiveIntensity: 0.9 });
  const ring = new THREE.MeshBasicMaterial({ color: "#94a3b8", transparent: true, opacity: 0.4 });
  const ringMesh = new THREE.Mesh(new THREE.PlaneGeometry(ringW, ringD), ring);
  ringMesh.rotation.x = -Math.PI / 2;
  ringMesh.position.y = 0.012;
  return { lamp, ring, ringMesh };
}

/** Hydraulic excavator; the dig face is local +x and the truck loads at local +z. */
export function excavator(): ModelParts {
  const group = new THREE.Group();
  const yellow = new THREE.MeshStandardMaterial({ color: "#f59e0b", roughness: 0.6 });
  const dark = new THREE.MeshStandardMaterial({ color: "#27272a", roughness: 0.8 });
  const glass = new THREE.MeshStandardMaterial({ color: "#1e3a5f", roughness: 0.2 });
  const { lamp, ring, ringMesh } = statusParts(4.4, 3.6);
  const meshes: THREE.Mesh[] = [];
  const add = (parent: THREE.Object3D, m: THREE.Mesh) => {
    parent.add(m);
    meshes.push(m);
    return m;
  };

  for (const z of [-0.8, 0.8]) add(group, new THREE.Mesh(new THREE.BoxGeometry(3.2, 0.6, 0.7), dark)).position.set(0, 0.3, z);

  const swing = new THREE.Group();
  swing.position.y = 0.65;
  group.add(swing);
  add(swing, new THREE.Mesh(new THREE.BoxGeometry(2.2, 0.9, 2.0), yellow)).position.set(-0.4, 0.45, 0);
  add(swing, new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.95, 0.85), glass)).position.set(0.35, 1.3, -0.5);
  add(swing, new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.4, 0.4), lamp)).position.set(0.35, 1.95, -0.5);

  const boom = new THREE.Group();
  boom.position.set(0.7, 1.0, 0.4);
  boom.rotation.z = 0.65;
  swing.add(boom);
  add(boom, new THREE.Mesh(new THREE.BoxGeometry(2.9, 0.32, 0.32), yellow)).position.x = 1.45;
  const stick = new THREE.Group();
  stick.position.x = 2.9;
  stick.rotation.z = -1.75;
  boom.add(stick);
  add(stick, new THREE.Mesh(new THREE.BoxGeometry(2.0, 0.26, 0.26), yellow)).position.x = 1.0;
  add(stick, new THREE.Mesh(new THREE.BoxGeometry(0.6, 0.55, 0.75), dark)).position.set(2.05, -0.1, 0);

  group.add(ringMesh);
  for (const m of meshes) m.castShadow = true;
  return { group, meshes, lamp, ring, swing, labelHeight: 4.6 };
}

/** Dump and spread area: a pad, the fill pile beyond it (local +x) and a dozer. */
export function dumpSite(): ModelParts {
  const group = new THREE.Group();
  const soil = new THREE.MeshStandardMaterial({ color: "#8b5e34", roughness: 1 });
  const pad = new THREE.MeshStandardMaterial({ color: "#57534e", roughness: 1 });
  const yellow = new THREE.MeshStandardMaterial({ color: "#eab308", roughness: 0.6 });
  const dark = new THREE.MeshStandardMaterial({ color: "#27272a", roughness: 0.8 });
  const { lamp, ring, ringMesh } = statusParts(5.4, 4.4);
  const meshes: THREE.Mesh[] = [];

  const base = new THREE.Mesh(new THREE.BoxGeometry(5, 0.06, 4), pad);
  base.position.y = 0.03;
  base.receiveShadow = true;
  const pile = new THREE.Mesh(new THREE.ConeGeometry(3.2, 1.9, 20), soil);
  pile.position.set(4.6, 0.95, 0);
  const dozer = new THREE.Mesh(new THREE.BoxGeometry(1.5, 0.8, 1.1), yellow);
  dozer.position.set(3.0, 0.4, 2.7);
  const blade = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.6, 1.5), dark);
  blade.position.set(3.85, 0.3, 2.7);
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 2.4, 8), dark);
  pole.position.set(-2.2, 1.2, -1.7);
  const light = new THREE.Mesh(new THREE.SphereGeometry(0.25, 12, 8), lamp);
  light.position.set(-2.2, 2.5, -1.7);
  meshes.push(base, pile, dozer, blade, pole, light);
  group.add(...meshes, ringMesh);
  for (const m of meshes) m.castShadow = m !== base;
  return { group, meshes, lamp, ring, labelHeight: 3.4 };
}

/** Haul road: a flat strip with a dashed centre line along each segment. */
export function roadSegment(length: number): { group: THREE.Group; surface: THREE.Mesh } {
  const group = new THREE.Group();
  const surface = new THREE.Mesh(
    new THREE.BoxGeometry(length + 2.6, 0.06, 2.8),
    new THREE.MeshStandardMaterial({ color: "#44403c", roughness: 1 }),
  );
  surface.position.y = 0.03;
  surface.receiveShadow = true;
  group.add(surface);
  const dash = new THREE.MeshBasicMaterial({ color: "#d6d3d1" });
  const dashes = Math.max(1, Math.floor(length / 2));
  for (let i = 0; i < dashes; i++) {
    const d = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.02, 0.12), dash);
    d.position.set(-length / 2 + (i + 0.5) * (length / dashes), 0.07, 0);
    group.add(d);
  }
  return { group, surface };
}
