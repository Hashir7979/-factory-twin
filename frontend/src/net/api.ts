import type { Economics, OptVariable, Overrides, Scenario, ScenarioSummary } from "../types";
import { httpBase } from "./config";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId?: string,
  ) {
    super(message);
  }
}

/** Turn FastAPI error bodies (string or validation list) into one readable sentence. */
function errorMessage(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string; loc?: unknown[] };
    const field = first.loc?.slice(1).join(".");
    return `${first.msg ?? "Invalid request"}${field ? ` (${field})` : ""}`;
  }
  if (status === 429) return "The server is busy. Please try again shortly.";
  if (status >= 500) return "The server had a problem. Please try again.";
  return `Request failed (${status}).`;
}

async function request<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${httpBase()}${path}`, {
      ...init,
      signal,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError("Cannot reach the server. Check your connection.", 0);
  }
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    throw new ApiError(errorMessage(body, res.status), res.status, res.headers.get("x-request-id") ?? undefined);
  }
  return body as T;
}

export interface JobState<R> {
  job_id: string;
  kind: string;
  status: "queued" | "running" | "done" | "failed";
  progress: number;
  error: string | null;
  result: R | null;
}

export const api = {
  scenarios: () => request<ScenarioSummary[]>("/api/scenarios"),

  scenario: (id: string) => request<Scenario>(`/api/scenarios/${encodeURIComponent(id)}`),

  optimizerDefaults: (id: string) =>
    request<{ variables: OptVariable[]; economics: Economics; max_trials: number }>(
      `/api/scenarios/${encodeURIComponent(id)}/optimizer-defaults`,
    ),

  startExperiment: (body: {
    scenario_id: string;
    overrides: Overrides;
    replications: number;
    horizon?: number;
    warmup?: number;
    base_seed?: number;
  }) => request<{ job_id: string }>("/api/experiments", { method: "POST", body: JSON.stringify(body) }),

  startComparison: (body: {
    scenario_id: string;
    a_label: string;
    b_label: string;
    a_overrides: Overrides;
    b_overrides: Overrides;
    replications: number;
    horizon?: number;
    warmup?: number;
  }) => request<{ job_id: string }>("/api/comparisons", { method: "POST", body: JSON.stringify(body) }),

  startOptimization: (body: unknown) =>
    request<{ job_id: string }>("/api/optimizations", { method: "POST", body: JSON.stringify(body) }),

  job: <R>(id: string, signal?: AbortSignal) =>
    request<JobState<R>>(`/api/jobs/${encodeURIComponent(id)}`, undefined, signal),

  csvUrl: (id: string) => `${httpBase()}/api/jobs/${encodeURIComponent(id)}/runs.csv`,
};

/** Poll a job until it finishes. Rejects with ApiError on failure; abortable. */
export async function waitForJob<R>(
  id: string,
  onProgress: (p: number) => void,
  signal: AbortSignal,
  intervalMs = 500,
): Promise<R> {
  let consecutiveErrors = 0;
  for (;;) {
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    try {
      const job = await api.job<R>(id, signal);
      consecutiveErrors = 0;
      onProgress(job.progress);
      if (job.status === "done" && job.result) return job.result;
      if (job.status === "failed") throw new ApiError(job.error ?? "The job failed.", 500);
    } catch (err) {
      // Tolerate a few transient network errors while polling.
      if (!(err instanceof ApiError) || err.status !== 0 || ++consecutiveErrors > 5) throw err;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
