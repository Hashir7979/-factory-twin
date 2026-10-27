import type { Overrides, Scenario } from "../types";
import { clear, h } from "./dom";

interface Field {
  key: string;
  label: string;
  group: string;
  base: number;
  min: number;
  max: number;
  step: number;
  integer: boolean;
}

/** Same limits as backend/sim/config.py OVERRIDABLE; the server re-validates. */
export function overrideFields(base: Scenario): Field[] {
  const fields: Field[] = [];
  for (const st of base.stations) {
    fields.push({ key: `station.${st.id}.servers`, label: st.name, group: "Machines per station", base: st.servers, min: 1, max: 20, step: 1, integer: true });
  }
  for (const b of base.buffers) {
    if (b.capacity !== null) {
      fields.push({ key: `buffer.${b.id}.capacity`, label: b.name, group: "Buffer capacity", base: b.capacity, min: 1, max: 10000, step: 1, integer: true });
    }
  }
  if (base.resources.technicians !== null) {
    fields.push({ key: "resources.technicians", label: "Maintenance technicians", group: "People", base: base.resources.technicians, min: 1, max: 100, step: 1, integer: true });
  }
  if (base.stations.some((s) => s.needs_operator)) {
    fields.push({ key: "resources.operators", label: "Operators", group: "People", base: base.resources.operators, min: 1, max: 200, step: 1, integer: true });
  }
  fields.push({ key: "source.interarrival_scale", label: "Arrival interval ×", group: "Demand", base: 1, min: 0.1, max: 10, step: 0.05, integer: false });
  return fields;
}

export function validate(field: Field, raw: string): string | null {
  if (raw.trim() === "") return "Required";
  const v = Number(raw);
  if (!Number.isFinite(v)) return "Must be a number";
  if (field.integer && !Number.isInteger(v)) return "Must be a whole number";
  if (v < field.min || v > field.max) return `Between ${field.min} and ${field.max}`;
  return null;
}

/** Overrides = only the fields that differ from the base scenario. */
export function diffOverrides(fields: Field[], values: Map<string, number>): Overrides {
  const out: Overrides = {};
  for (const f of fields) {
    const v = values.get(f.key);
    if (v !== undefined && Math.abs(v - f.base) > 1e-9) out[f.key] = v;
  }
  return out;
}

/** "What-if" form: edit capacities, staffing and demand, then restart the live run. */
export class SettingsForm {
  onApply: (overrides: Overrides, seed: number) => void = () => {};

  private fields: Field[] = [];
  private inputs = new Map<string, HTMLInputElement>();

  constructor(private readonly host: HTMLElement) {}

  render(base: Scenario, current: Overrides, seed: number): void {
    clear(this.host);
    this.inputs.clear();
    this.fields = overrideFields(base);
    const form = h("form", { class: "settings-form", novalidate: true });
    const groups = new Map<string, HTMLElement>();
    for (const f of this.fields) {
      let fs = groups.get(f.group);
      if (!fs) {
        fs = h("fieldset", {}, h("legend", {}, f.group));
        groups.set(f.group, fs);
        form.append(fs);
      }
      const id = `ov-${f.key.replace(/[^A-Za-z0-9_-]/g, "_")}`;
      const err = h("span", { class: "field-error", id: `${id}-err`, "aria-live": "polite" });
      const input = h("input", {
        id, name: f.key, type: "number", min: f.min, max: f.max, step: f.step,
        value: current[f.key] ?? f.base, inputmode: f.integer ? "numeric" : "decimal", "aria-describedby": `${id}-err`,
      }) as HTMLInputElement;
      input.addEventListener("input", () => this.check());
      this.inputs.set(f.key, input);
      fs.append(h("div", { class: "field" }, h("label", { for: id }, f.label), input, err));
    }
    const seedInput = h("input", { id: "ov-seed", type: "number", min: 0, max: 2147483647, step: 1, value: seed, inputmode: "numeric", "aria-describedby": "ov-seed-err" }) as HTMLInputElement;
    form.append(
      h("fieldset", {}, h("legend", {}, "Randomness"), h("div", { class: "field" }, h("label", { for: "ov-seed" }, "Random seed"), seedInput, h("span", { class: "field-error", id: "ov-seed-err" }))),
    );
    seedInput.addEventListener("input", () => this.check());

    const apply = h("button", { type: "submit", class: "btn btn-primary" }, "Apply & restart");
    const defaults = h("button", { type: "button", class: "btn" }, "Restore defaults");
    form.append(h("div", { class: "form-actions" }, apply, defaults));
    defaults.addEventListener("click", () => {
      for (const f of this.fields) this.inputs.get(f.key)!.value = String(f.base);
      seedInput.value = String(base.run.seed);
      this.check();
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const values = this.check();
      const seedValue = Number(seedInput.value);
      if (!values || !Number.isInteger(seedValue) || seedValue < 0) return;
      this.onApply(diffOverrides(this.fields, values), seedValue);
    });
    this.host.append(form);
  }

  /** Current overrides if the form is valid (used by the experiments tab). */
  currentOverrides(): Overrides | null {
    const values = this.check();
    return values ? diffOverrides(this.fields, values) : null;
  }

  private check(): Map<string, number> | null {
    const values = new Map<string, number>();
    let ok = true;
    for (const f of this.fields) {
      const input = this.inputs.get(f.key)!;
      const msg = validate(f, input.value);
      const err = document.getElementById(`${input.id}-err`);
      if (err) err.textContent = msg ?? "";
      input.setAttribute("aria-invalid", msg ? "true" : "false");
      if (msg) ok = false;
      else values.set(f.key, Number(input.value));
    }
    const seed = document.getElementById("ov-seed") as HTMLInputElement | null;
    if (seed) {
      const v = Number(seed.value);
      const bad = seed.value.trim() === "" || !Number.isInteger(v) || v < 0 || v > 2147483647;
      const err = document.getElementById("ov-seed-err");
      if (err) err.textContent = bad ? "Whole number ≥ 0" : "";
      seed.setAttribute("aria-invalid", bad ? "true" : "false");
      if (bad) ok = false;
    }
    const submit = this.host.querySelector<HTMLButtonElement>("button[type=submit]");
    if (submit) submit.disabled = !ok;
    return ok ? values : null;
  }
}
