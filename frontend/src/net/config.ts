// Backend location. By default the API is same-origin (Vite proxies it in
// development, FastAPI serves the built app in production). VITE_API_URL points
// the frontend at a separately hosted backend instead.
const configured = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "");

export function httpBase(): string {
  return configured ?? "";
}

export function wsBase(): string {
  if (configured) return configured.replace(/^http/, "ws");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
}
