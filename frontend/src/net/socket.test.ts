import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveSocket } from "./socket";

class FakeWS {
  static instances: FakeWS[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {
    FakeWS.instances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  message(obj: unknown) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
}

const make = () =>
  new LiveSocket({ baseUrl: "ws://x", WebSocketImpl: FakeWS as unknown as typeof WebSocket, random: () => 1, baseDelayMs: 100, maxAttempts: 3 });

describe("LiveSocket", () => {
  beforeEach(() => {
    FakeWS.instances = [];
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it("reconnects with back-off and resumes the same session", () => {
    const s = make();
    const states: string[] = [];
    s.onState = (st) => states.push(st);
    s.connect("line");
    const first = FakeWS.instances[0]!;
    expect(first.url).toBe("ws://x/ws/session?scenario=line");
    first.open();
    first.message({ type: "hello", session: "abc" });
    first.drop();
    expect(states.at(-1)).toBe("reconnecting");
    vi.advanceTimersByTime(99);
    expect(FakeWS.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWS.instances[1]!.url).toBe("ws://x/ws/session?scenario=line&resume=abc");
  });

  it("gives up after max attempts and on fatal close codes", () => {
    const s = make();
    const states: string[] = [];
    s.onState = (st) => states.push(st);
    s.connect("line");
    for (let i = 0; i < 4; i++) {
      FakeWS.instances.at(-1)!.drop();
      vi.advanceTimersByTime(10_000);
    }
    expect(states.at(-1)).toBe("offline");

    const t = make();
    let reason = "";
    t.onState = (st, info) => st === "offline" && (reason = info.reason ?? "");
    t.connect("missing");
    FakeWS.instances.at(-1)!.drop(4404);
    expect(reason).toMatch(/does not exist/);
  });

  it("only sends while open and ignores malformed frames", () => {
    const s = make();
    const got: unknown[] = [];
    s.onMessage = (m) => got.push(m);
    s.connect("line");
    expect(s.send({ type: "play" })).toBe(false);
    const ws = FakeWS.instances[0]!;
    ws.open();
    expect(s.send({ type: "play" })).toBe(true);
    expect(ws.sent).toEqual(['{"type":"play"}']);
    ws.onmessage?.({ data: "{not json" });
    expect(got).toHaveLength(0);
  });

  it("switching scenario drops the resume token", () => {
    const s = make();
    s.connect("a");
    FakeWS.instances[0]!.open();
    FakeWS.instances[0]!.message({ type: "hello", session: "tok" });
    s.connect("b");
    expect(FakeWS.instances.at(-1)!.url).toBe("ws://x/ws/session?scenario=b");
  });
});
