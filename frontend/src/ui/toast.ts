import { byId, h } from "./dom";

type Tone = "info" | "error" | "success";

const recent = new Map<string, number>();

/** Transient notification, announced to screen readers via the live region. */
export function toast(message: string, tone: Tone = "info", ms = 4500): void {
  // Collapse identical messages fired in quick succession (e.g. repeated errors).
  const now = Date.now();
  if ((recent.get(message) ?? 0) > now - 1500) return;
  recent.set(message, now);

  const host = byId("toasts");
  const close = h("button", { class: "toast-close", type: "button", "aria-label": "Dismiss" }, "×");
  const el = h("div", { class: `toast toast-${tone}`, role: tone === "error" ? "alert" : "status" }, h("span", {}, message), close);
  const remove = () => el.remove();
  close.addEventListener("click", remove);
  host.append(el);
  setTimeout(remove, ms);
}
