import type { ClientCmd, ServerMsg } from "../types";
import { wsBase } from "./config";

export type ConnState = "connecting" | "open" | "reconnecting" | "offline";

export interface ConnInfo {
  attempt: number;
  retryInMs?: number;
  reason?: string;
}

// Close codes defined by the backend (api/ws.py).
const FATAL_CLOSE: Record<number, string> = {
  4403: "This page is not allowed to connect to the simulator.",
  4404: "The selected scenario does not exist.",
};
const CLOSE_BUSY = 1013;

export interface LiveSocketOptions {
  baseUrl?: string;
  maxAttempts?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  WebSocketImpl?: typeof WebSocket;
  random?: () => number;
}

/**
 * WebSocket client with exponential back-off and session resume: after a drop
 * it reconnects with the previous session id, so the server hands back the same
 * running simulation instead of starting a new one.
 */
export class LiveSocket {
  onMessage: (msg: ServerMsg) => void = () => {};
  onState: (state: ConnState, info: ConnInfo) => void = () => {};

  private ws: WebSocket | null = null;
  private scenario = "";
  private sessionId: string | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closedByUser = false;
  private readonly opts: Required<LiveSocketOptions>;

  constructor(opts: LiveSocketOptions = {}) {
    this.opts = {
      baseUrl: opts.baseUrl ?? wsBase(),
      maxAttempts: opts.maxAttempts ?? 8,
      baseDelayMs: opts.baseDelayMs ?? 500,
      maxDelayMs: opts.maxDelayMs ?? 15_000,
      WebSocketImpl: opts.WebSocketImpl ?? WebSocket,
      random: opts.random ?? Math.random,
    };
  }

  get connected(): boolean {
    return this.ws?.readyState === 1;
  }

  /** Connect to a scenario. Switching scenario starts a fresh session. */
  connect(scenario: string): void {
    if (scenario !== this.scenario) this.sessionId = null;
    this.scenario = scenario;
    this.closedByUser = false;
    this.attempt = 0;
    this.open();
  }

  send(cmd: ClientCmd): boolean {
    if (!this.ws || this.ws.readyState !== 1) return false;
    this.ws.send(JSON.stringify(cmd));
    return true;
  }

  close(): void {
    this.closedByUser = true;
    this.clearTimer();
    this.ws?.close(1000);
    this.ws = null;
  }

  private open(): void {
    this.clearTimer();
    this.detach();
    const params = new URLSearchParams({ scenario: this.scenario });
    if (this.sessionId) params.set("resume", this.sessionId);
    this.onState(this.attempt === 0 ? "connecting" : "reconnecting", { attempt: this.attempt });

    const ws = new this.opts.WebSocketImpl(`${this.opts.baseUrl}/ws/session?${params}`);
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.onState("open", { attempt: 0 });
    };
    ws.onmessage = (ev: MessageEvent) => {
      let msg: ServerMsg;
      try {
        msg = JSON.parse(String(ev.data)) as ServerMsg;
      } catch {
        return; // never let a malformed frame break the UI
      }
      if (msg.type === "hello") this.sessionId = msg.session;
      this.onMessage(msg);
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws !== ws) return; // superseded by a newer socket
      this.ws = null;
      if (this.closedByUser) return;
      const fatal = FATAL_CLOSE[ev.code];
      if (fatal) {
        this.onState("offline", { attempt: this.attempt, reason: fatal });
        return;
      }
      this.scheduleRetry(ev.code === CLOSE_BUSY ? 4 : 1);
    };
    // onerror is always followed by onclose, which handles retrying.
    ws.onerror = () => {};
  }

  private scheduleRetry(multiplier: number): void {
    this.attempt += 1;
    if (this.attempt > this.opts.maxAttempts) {
      this.onState("offline", { attempt: this.attempt, reason: "Cannot reach the simulation server." });
      return;
    }
    const exp = Math.min(this.opts.maxDelayMs, this.opts.baseDelayMs * 2 ** (this.attempt - 1) * multiplier);
    // Jitter (50-100%) so many clients do not reconnect in lockstep after a restart.
    const delay = Math.round(exp * (0.5 + this.opts.random() / 2));
    this.onState("reconnecting", { attempt: this.attempt, retryInMs: delay });
    this.timer = setTimeout(() => this.open(), delay);
  }

  private detach(): void {
    if (!this.ws) return;
    const old = this.ws;
    this.ws = null;
    old.onclose = null;
    old.onmessage = null;
    old.close(1000);
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}
