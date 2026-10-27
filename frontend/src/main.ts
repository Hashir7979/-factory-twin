import "./styles.css";
import { ApiError, api } from "./net/api";
import { type ConnInfo, type ConnState, LiveSocket } from "./net/socket";
import { FactoryScene, type Pick } from "./scene/factory";
import { LineState, SimClock } from "./sim/state";
import type { HelloMsg, Kpis, Overrides, Scenario, ServerMsg, SessionStatus } from "./types";
import { fmtClock, fmtSpeed, byId, h } from "./ui/dom";
import { Inspector } from "./ui/inspector";
import { KpiPanel } from "./ui/kpis";
import { TrendChart } from "./ui/charts";
import { SettingsForm } from "./ui/settings";
import { StateTimeline } from "./ui/timeline";
import { initTabs } from "./ui/tabs";
import { toast } from "./ui/toast";

const SPEED_STEPS = 1000;

class App {
  private readonly socket = new LiveSocket();
  private readonly state = new LineState();
  private readonly clock = new SimClock();
  private readonly scene: FactoryScene;
  private readonly kpis: KpiPanel;
  private readonly chart: TrendChart;
  private readonly timeline: StateTimeline;
  private readonly inspector: Inspector;
  private readonly settings: SettingsForm;

  private scenarioId = "";
  private baseScenario: Scenario | null = null;
  private sceneKey = "";
  private modelKey = "";
  private lastHelloT = 0;
  private latestKpis: Kpis | null = null;
  private overrides: Overrides = {};
  private seed = 1;
  private status: SessionStatus = "paused";
  private maxSpeed = 1000;
  private speedTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFrame = performance.now();
  private frame = 0;

  private readonly el = {
    select: byId<HTMLSelectElement>("scenario-select"),
    conn: byId("conn"),
    play: byId<HTMLButtonElement>("play"),
    restart: byId<HTMLButtonElement>("restart"),
    speed: byId<HTMLInputElement>("speed"),
    speedOut: byId<HTMLOutputElement>("speed-out"),
    clock: byId("sim-clock"),
    warmup: byId("warmup-badge"),
    overlay: byId("overlay"),
    overlayTitle: byId("overlay-title"),
    overlayText: byId("overlay-text"),
    overlayRetry: byId<HTMLButtonElement>("overlay-retry"),
  };

  constructor() {
    this.scene = new FactoryScene(byId("scene"));
    this.kpis = new KpiPanel(byId("kpi-cards"), byId("bottleneck"), byId("stations-body"), byId("buffers-body"), byId("legend"));
    this.chart = new TrendChart(byId("trend"));
    this.timeline = new StateTimeline(byId<HTMLCanvasElement>("timeline"));
    this.inspector = new Inspector(byId("inspector"));
    this.settings = new SettingsForm(byId("settings"));
    initTabs(byId("panel"));
    this.bind();
    requestAnimationFrame((t) => this.loop(t));
  }

  // ------------------------------------------------------------------------- startup

  async start(): Promise<void> {
    this.showOverlay("Loading scenarios…", "");
    let list;
    try {
      list = await api.scenarios();
    } catch (err) {
      this.showOverlay("Simulator unavailable", (err as Error).message, () => void this.start());
      return;
    }
    this.el.select.replaceChildren(...list.map((s) => h("option", { value: s.id }, s.name)));
    const wanted = new URLSearchParams(location.search).get("scenario");
    const initial = list.find((s) => s.id === wanted)?.id ?? list[0]?.id;
    if (!initial) {
      this.showOverlay("No scenarios", "The server has no scenarios configured.");
      return;
    }
    this.el.select.value = initial;
    await this.openScenario(initial);
  }

  private async openScenario(id: string): Promise<void> {
    this.scenarioId = id;
    const url = new URL(location.href);
    url.searchParams.set("scenario", id);
    history.replaceState(null, "", url);
    try {
      this.baseScenario = await api.scenario(id);
    } catch (err) {
      this.showOverlay("Could not load scenario", (err as Error).message, () => void this.openScenario(id));
      return;
    }
    this.selectPick(null);
    this.socket.connect(id);
  }

  // ------------------------------------------------------------------------- wiring

  private bind(): void {
    this.socket.onMessage = (m) => this.onMessage(m);
    this.socket.onState = (s, info) => this.onConn(s, info);
    this.scene.onPick = (p) => this.selectPick(p);
    this.inspector.onClose = () => this.selectPick(null);
    this.inspector.onInjectFailure = (machine, repair) => {
      if (!this.socket.send({ type: "inject_failure", machine, repair_time: repair })) toast("Not connected.", "error");
    };
    this.settings.onApply = (overrides, seed) => {
      if (this.socket.send({ type: "reset", overrides, seed })) toast("Restarting with new settings…", "info", 2500);
      else toast("Not connected.", "error");
    };

    this.el.select.addEventListener("change", () => void this.openScenario(this.el.select.value));
    this.el.play.addEventListener("click", () => this.togglePlay());
    this.el.restart.addEventListener("click", () => {
      this.socket.send({ type: "reset", overrides: this.overrides, seed: this.seed });
    });
    this.el.speed.addEventListener("input", () => {
      const speed = this.speedFromSlider();
      this.el.speedOut.textContent = fmtSpeed(speed);
      if (this.speedTimer) clearTimeout(this.speedTimer);
      this.speedTimer = setTimeout(() => this.socket.send({ type: "speed", value: speed }), 120);
    });
    byId("fit-view").addEventListener("click", () => this.scene.fitCamera());
    this.el.overlayRetry.addEventListener("click", () => this.retry?.());

    document.addEventListener("keydown", (e) => {
      const t = e.target as HTMLElement;
      if (e.ctrlKey || e.metaKey || e.altKey || t.closest("input, select, textarea, button, [role=tab], a")) return;
      if (e.code === "Space") {
        e.preventDefault();
        this.togglePlay();
      } else if (e.key === "f" || e.key === "F") {
        this.scene.fitCamera();
      } else if (e.key === "Escape") {
        this.selectPick(null);
      }
    });
  }

  private retry: (() => void) | null = null;

  private togglePlay(): void {
    if (this.status === "finished") {
      toast("This run has reached its time limit. Restart to run again.", "info");
      return;
    }
    this.socket.send({ type: this.status === "playing" ? "pause" : "play" });
  }

  private selectPick(p: Pick): void {
    this.scene.setSelection(p);
    this.inspector.show(p, this.state.scenario, this.state, this.latestKpis, this.socket.connected);
  }

  // ------------------------------------------------------------------------- server messages

  private onMessage(msg: ServerMsg): void {
    switch (msg.type) {
      case "hello":
        this.onHello(msg);
        break;
      case "events":
        this.state.enqueue(msg);
        this.clock.sync(msg.t, performance.now());
        break;
      case "kpis":
        this.state.observeSeq(msg.seq);
        this.latestKpis = msg.data;
        if (this.state.scenario) this.kpis.update(msg, this.state.scenario);
        this.chart.push(msg.t, msg.rolling_throughput, msg.data.line.wip);
        this.el.warmup.hidden = msg.warmed_up || msg.warmup <= 0;
        this.el.warmup.textContent = `Warm-up until ${fmtClock(msg.warmup)}`;
        this.refreshInspector();
        break;
      case "status":
        this.applyStatus(msg.status, msg.speed, msg.max_speed);
        if (msg.status === "finished") toast("Run finished: the live time limit was reached.", "info");
        break;
      case "error":
        toast(msg.message, "error");
        break;
    }
  }

  private onHello(msg: HelloMsg): void {
    const sceneKey = JSON.stringify(msg.scenario);
    const modelKey = `${sceneKey}|${msg.seed}`;
    const isNewModel = modelKey !== this.modelKey || msg.snapshot.t < this.lastHelloT;
    this.state.load(msg);
    this.clock.reset(msg.snapshot.t, performance.now());
    if (sceneKey !== this.sceneKey) {
      this.scene.build(msg.scenario);
      this.sceneKey = sceneKey;
    } else if (isNewModel) {
      this.scene.resetProducts();
    }
    if (isNewModel) {
      this.chart.reset();
      this.kpis.reset();
      this.latestKpis = null;
    }
    this.modelKey = modelKey;
    this.lastHelloT = msg.snapshot.t;
    this.overrides = msg.overrides;
    this.seed = msg.seed;
    if (this.baseScenario?.id === msg.scenario.id) this.settings.render(this.baseScenario, msg.overrides, msg.seed);
    this.applyStatus(msg.status, msg.speed, msg.max_speed);
    this.selectPick(this.inspector.selected && this.exists(this.inspector.selected) ? this.inspector.selected : null);
  }

  private exists(p: NonNullable<Pick>): boolean {
    return p.kind === "machine" ? this.state.machines.has(p.id) : this.state.buffers.has(p.id);
  }

  private applyStatus(status: SessionStatus, speed: number, maxSpeed: number): void {
    this.status = status;
    this.maxSpeed = maxSpeed;
    this.clock.playing = status === "playing";
    this.clock.speed = speed;
    this.el.play.textContent = status === "playing" ? "Pause" : "Play";
    this.el.play.setAttribute("aria-pressed", String(status === "playing"));
    this.el.play.disabled = status === "finished";
    if (document.activeElement !== this.el.speed) {
      this.el.speed.value = String(this.sliderFromSpeed(speed));
      this.el.speedOut.textContent = fmtSpeed(speed);
    }
  }

  private speedFromSlider(): number {
    const pos = Number(this.el.speed.value) / SPEED_STEPS;
    return Math.max(1, Math.round(10 ** (pos * Math.log10(this.maxSpeed)) * 10) / 10);
  }

  private sliderFromSpeed(speed: number): number {
    return Math.round((Math.log10(Math.max(1, speed)) / Math.log10(this.maxSpeed)) * SPEED_STEPS);
  }

  // ------------------------------------------------------------------------- connection UI

  private onConn(state: ConnState, info: ConnInfo): void {
    const text = this.el.conn.querySelector(".conn-text")!;
    this.el.conn.dataset.state = state;
    const controls = [this.el.play, this.el.restart];
    controls.forEach((b) => (b.disabled = state !== "open" || (b === this.el.play && this.status === "finished")));
    switch (state) {
      case "connecting":
        text.textContent = "Connecting…";
        this.showOverlay("Connecting to simulator…", "");
        break;
      case "open":
        text.textContent = "Live";
        this.hideOverlay();
        break;
      case "reconnecting":
        text.textContent = "Reconnecting…";
        this.showOverlay(
          "Connection lost",
          info.retryInMs ? `Reconnecting in ${Math.ceil(info.retryInMs / 1000)} s (attempt ${info.attempt}). Your simulation is kept for a minute.` : "Reconnecting…",
          () => this.socket.connect(this.scenarioId),
        );
        break;
      case "offline":
        text.textContent = "Offline";
        this.showOverlay("Simulator unavailable", info.reason ?? "The connection was closed.", () => this.socket.connect(this.scenarioId));
        break;
    }
    this.refreshInspector();
  }

  private showOverlay(title: string, text: string, retry?: () => void): void {
    this.el.overlay.hidden = false;
    this.el.overlayTitle.textContent = title;
    this.el.overlayText.textContent = text;
    this.retry = retry ?? null;
    this.el.overlayRetry.hidden = !retry;
  }

  private hideOverlay(): void {
    this.el.overlay.hidden = true;
    this.retry = null;
  }

  private refreshInspector(): void {
    if (this.inspector.selected) this.inspector.show(this.inspector.selected, this.state.scenario, this.state, this.latestKpis, this.socket.connected);
  }

  // ------------------------------------------------------------------------- render loop

  private loop(ts: number): void {
    const dt = Math.min(0.1, (ts - this.lastFrame) / 1000);
    this.lastFrame = ts;
    this.frame++;
    const now = this.clock.now(ts);
    this.state.applyUntil(now);
    this.scene.update(this.state, now, dt);
    this.scene.render();
    if (this.frame % 6 === 0) this.el.clock.textContent = fmtClock(now);
    if (this.frame % 15 === 0) {
      this.timeline.draw(this.state, now);
      if (this.inspector.selected?.kind === "machine") this.refreshInspector();
    }
    requestAnimationFrame((t) => this.loop(t));
  }
}

function hasWebGL(): boolean {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") ?? c.getContext("webgl"));
  } catch {
    return false;
  }
}

if (!hasWebGL()) {
  byId("overlay").hidden = false;
  byId("overlay-title").textContent = "3D view unavailable";
  byId("overlay-text").textContent = "Your browser or device does not support WebGL, which the 3D view needs.";
} else {
  const app = new App();
  app.start().catch((err: unknown) => {
    console.error(err);
    toast(err instanceof ApiError ? err.message : "Something went wrong while starting the app.", "error");
  });
}
