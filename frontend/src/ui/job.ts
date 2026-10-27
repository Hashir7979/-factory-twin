import { ApiError, waitForJob } from "../net/api";
import { h } from "./dom";
import { toast } from "./toast";

/**
 * Run-button + progress bar for a background job. Prevents double submission,
 * lets the user stop waiting, and reports failures in plain language.
 */
export class JobControl<R> {
  readonly el: HTMLElement;
  private readonly button: HTMLButtonElement;
  private readonly cancel: HTMLButtonElement;
  private readonly bar: HTMLElement;
  private readonly fill: HTMLElement;
  private readonly status: HTMLElement;
  private abort: AbortController | null = null;

  constructor(
    label: string,
    private readonly start: () => Promise<{ job_id: string }> | null,
    private readonly done: (result: R, jobId: string) => void,
  ) {
    this.button = h("button", { type: "button", class: "btn btn-primary" }, label);
    this.cancel = h("button", { type: "button", class: "btn", hidden: true }, "Stop waiting");
    this.fill = h("span", { style: "width:0%" });
    this.bar = h("div", { class: "progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": 0, hidden: true }, this.fill);
    this.status = h("p", { class: "muted small", "aria-live": "polite" });
    this.el = h("div", { class: "job-control" }, h("div", { class: "form-actions" }, this.button, this.cancel), this.bar, this.status);
    this.button.addEventListener("click", () => void this.run());
    this.cancel.addEventListener("click", () => this.abort?.abort());
  }

  set disabled(v: boolean) {
    if (!this.abort) this.button.disabled = v;
  }

  private setProgress(p: number): void {
    const pct = Math.round(p * 100);
    this.fill.style.width = `${pct}%`;
    this.bar.setAttribute("aria-valuenow", String(pct));
    this.status.textContent = `Running… ${pct}%`;
  }

  private async run(): Promise<void> {
    if (this.abort) return;
    const pending = this.start();
    if (!pending) return; // the caller reported a validation problem
    this.abort = new AbortController();
    this.button.disabled = true;
    this.cancel.hidden = false;
    this.bar.hidden = false;
    this.setProgress(0);
    this.status.textContent = "Starting…";
    try {
      const { job_id } = await pending;
      const result = await waitForJob<R>(job_id, (p) => this.setProgress(p), this.abort.signal);
      this.status.textContent = "Done.";
      this.done(result, job_id);
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        this.status.textContent = "Stopped waiting. The server finishes the job in the background.";
      } else {
        const msg = err instanceof ApiError ? err.message : "Something went wrong.";
        this.status.textContent = msg;
        toast(msg, "error");
      }
    } finally {
      this.abort = null;
      this.button.disabled = false;
      this.cancel.hidden = true;
      this.bar.hidden = true;
    }
  }
}
