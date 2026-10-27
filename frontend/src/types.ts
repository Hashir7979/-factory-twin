// Wire types shared with the backend (backend/sim/config.py, sim/kpi.py, api/ws.py).
// Keep in sync with the Pydantic models; field names are identical.

export type Point = [number, number];

export interface Distribution {
  type: "constant" | "exponential" | "uniform" | "triangular" | "normal" | "lognormal" | "weibull";
  [param: string]: number | string;
}

export interface Variant {
  id: string;
  name: string;
  weight: number;
  color: string;
}

export interface BufferCfg {
  id: string;
  name: string;
  capacity: number | null;
  kind: "queue" | "conveyor";
  transit_time: number;
  pos: Point;
  path: Point[] | null;
}

export interface StationCfg {
  id: string;
  name: string;
  kind: "machine" | "qc";
  servers: number;
  input: string;
  output: string;
  process_time: Distribution;
  setup_time: Distribution | null;
  failure: { ttf: Distribution; ttr: Distribution } | null;
  needs_operator: boolean;
  qc: { pass_prob: number; rework_prob: number; rework_output: string | null } | null;
  pos: Point;
}

export interface Scenario {
  id: string;
  name: string;
  description: string;
  variants: Variant[];
  source: { id: string; name: string; interarrival: Distribution; output: string; pos: Point };
  buffers: BufferCfg[];
  stations: StationCfg[];
  resources: { technicians: number | null; operators: number };
  sink: { pos: Point };
  run: { horizon: number; warmup: number; seed: number };
}

export interface ScenarioSummary {
  id: string;
  name: string;
  description: string;
  stations: number;
  buffers: number;
}

export type MachineState = "starved" | "waiting_operator" | "setup" | "processing" | "blocked" | "down";

export const MACHINE_STATES: MachineState[] = [
  "processing",
  "setup",
  "starved",
  "waiting_operator",
  "blocked",
  "down",
];

export type Overrides = Record<string, number>;

// --------------------------------------------------------------------------- KPIs

export interface MachineKpi {
  id: string;
  station: string;
  state: MachineState;
  processed: number;
  failures: number;
  state_pct: Record<MachineState, number>;
  utilization: number;
  mean_active_period: number;
}

export interface StationKpi {
  id: string;
  name: string;
  servers: number;
  processed: number;
  utilization: number;
  availability: number;
  performance: number;
  oee: number;
  active_pct: number;
  blocked_pct: number;
  starved_pct: number;
  down_pct: number;
  passed: number;
  reworked: number;
  scrapped: number;
}

export interface BufferKpi {
  id: string;
  name: string;
  capacity: number | null;
  level: number;
  mean_level: number;
  max_level: number;
  mean_wait: number;
  fill_pct: number | null;
}

export interface LineKpi {
  sim_time: number;
  observed_time: number;
  created: number;
  completed: number;
  scrapped: number;
  reworked: number;
  wip: number;
  mean_wip: number;
  throughput_per_hour: number;
  mean_cycle_time: number;
  cycle_time_std: number;
  first_pass_yield: number;
  source_blocked_pct: number;
  line_oee: number;
}

export interface BottleneckReport {
  station: string | null;
  confidence: "clear" | "shifting" | "none";
  ranking: { station: string; name: string; active_pct: number }[];
  upstream_blocked_pct: number | null;
  downstream_starved_pct: number | null;
  explanation: string;
}

export interface Kpis {
  line: LineKpi;
  stations: StationKpi[];
  machines: MachineKpi[];
  buffers: BufferKpi[];
  bottleneck: BottleneckReport;
}

// --------------------------------------------------------------------------- WebSocket

export interface Snapshot {
  t: number;
  machines: Record<string, { state: MachineState; product: number | null }>;
  buffers: Record<string, { product: number; ready_at: number }[]>;
  products: Record<string, string>; // product id → variant
}

export type SessionStatus = "paused" | "playing" | "finished";

export interface HelloMsg {
  type: "hello";
  session: string;
  seq: number;
  scenario: Scenario;
  overrides: Overrides;
  seed: number;
  snapshot: Snapshot;
  status: SessionStatus;
  speed: number;
  max_speed: number;
}

export type SimEvent =
  | { type: "create"; t: number; product: number; variant: string }
  | { type: "enter"; t: number; product: number; buffer: string; ready_at: number }
  | { type: "take"; t: number; product: number; machine: string }
  | { type: "state"; t: number; machine: string; state: MachineState }
  | { type: "exit"; t: number; product: number; machine: string; outcome: "complete" | "scrap" };

export interface EventsMsg {
  type: "events";
  seq: number;
  t: number;
  events: SimEvent[];
}

export interface KpisMsg {
  type: "kpis";
  seq: number;
  t: number;
  warmup: number;
  warmed_up: boolean;
  rolling_throughput: number;
  data: Kpis;
}

export interface StatusMsg {
  type: "status";
  status: SessionStatus;
  speed: number;
  max_speed: number;
}

export interface ErrorMsg {
  type: "error";
  code: string;
  message: string;
}

export type ServerMsg = HelloMsg | EventsMsg | KpisMsg | StatusMsg | ErrorMsg;

export type ClientCmd =
  | { type: "play" }
  | { type: "pause" }
  | { type: "speed"; value: number }
  | { type: "reset"; overrides: Overrides; seed?: number }
  | { type: "inject_failure"; machine: string; repair_time?: number }
  | { type: "snapshot" };

// --------------------------------------------------------------------------- experiments

export interface Estimate {
  mean: number;
  half_width: number;
  std: number;
  min: number;
  max: number;
  n: number;
}

export interface WarmupAnalysis {
  bin_seconds: number;
  window: number;
  bins: number[];
  mean_series: (number | null)[];
  smoothed: (number | null)[];
  suggested_warmup: number | null;
  steady_state: number | null;
}

export interface ExperimentResult {
  scenario_id: string;
  overrides: Overrides;
  horizon: number;
  warmup: number;
  seeds: number[];
  metrics: Record<string, Estimate>;
  stations: Record<string, Record<string, Estimate>>;
  buffers: Record<string, Record<string, Estimate>>;
  bottleneck_frequency: Record<string, number>;
  warmup_analysis: WarmupAnalysis;
  runs: LineKpi[];
}

export interface Difference {
  metric: string;
  a: Estimate;
  b: Estimate;
  diff: Estimate;
  relative: number | null;
  significant: boolean;
}

export interface ComparisonResult {
  a: ExperimentResult;
  b: ExperimentResult;
  differences: Difference[];
  labels: { a: string; b: string };
}

export interface OptVariable {
  key: string;
  low: number;
  high: number;
}

export interface Economics {
  margin_per_part: number;
  machine_cost_per_hour: number;
  station_machine_cost_per_hour: Record<string, number>;
  buffer_slot_cost_per_hour: number;
  technician_cost_per_hour: number;
  operator_cost_per_hour: number;
}

export interface Evaluation {
  params: Record<string, number>;
  throughput: Estimate;
  revenue_per_hour: number;
  cost_per_hour: number;
  profit: Estimate;
  costs: { machines: number; buffers: number; technicians: number; operators: number };
  bottleneck: string | null;
}

export interface OptimizationResult {
  scenario_id: string;
  variables: OptVariable[];
  economics: Economics;
  seeds: number[];
  baseline: Evaluation;
  best: Evaluation;
  trials: { number: number; evaluation: Evaluation; cached: boolean }[];
  pareto: number[];
  confirmation: {
    seeds: number[];
    baseline: Evaluation;
    candidates: Evaluation[];
    profit_difference: Difference | null;
    significant: boolean;
  };
  improvement_per_hour: number;
}
