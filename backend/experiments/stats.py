"""Output analysis: confidence intervals, paired comparisons, Welch warm-up."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel
from scipy import stats


class Estimate(BaseModel):
    mean: float
    half_width: float  # 95% CI half width (0 when n < 2)
    std: float
    min: float
    max: float
    n: int

    @property
    def low(self) -> float:
        return self.mean - self.half_width

    @property
    def high(self) -> float:
        return self.mean + self.half_width


def estimate(samples: Sequence[float], level: float = 0.95) -> Estimate:
    x = np.asarray(samples, dtype=float)
    n = len(x)
    if n == 0:
        return Estimate(mean=0.0, half_width=0.0, std=0.0, min=0.0, max=0.0, n=0)
    mean = float(x.mean())
    if n < 2:
        return Estimate(mean=mean, half_width=0.0, std=0.0, min=mean, max=mean, n=1)
    std = float(x.std(ddof=1))
    t = float(stats.t.ppf(0.5 + level / 2, df=n - 1))
    return Estimate(mean=mean, half_width=t * std / math.sqrt(n), std=std, min=float(x.min()), max=float(x.max()), n=n)


class Difference(BaseModel):
    """Paired difference B - A over common-random-number replications."""

    metric: str
    a: Estimate
    b: Estimate
    diff: Estimate
    relative: float | None  # diff.mean / a.mean
    significant: bool  # 95% CI of the difference excludes zero


def paired_difference(metric: str, a: Sequence[float], b: Sequence[float]) -> Difference:
    if len(a) != len(b):
        raise ValueError("paired samples must have equal length")
    ea, eb = estimate(a), estimate(b)
    d = estimate([y - x for x, y in zip(a, b, strict=True)])
    rel = d.mean / ea.mean if ea.mean else None
    significant = d.n >= 2 and (d.low > 0 or d.high < 0)
    return Difference(metric=metric, a=ea, b=eb, diff=d, relative=rel, significant=significant)


class WarmupAnalysis(BaseModel):
    bin_seconds: float
    window: int
    bins: list[float]  # bin end times
    mean_series: list[float | None]  # replication-averaged metric per bin
    smoothed: list[float | None]  # Welch moving average
    suggested_warmup: float | None
    steady_state: float | None


def binned_means(
    times: Sequence[float], values: Sequence[float], horizon: float, bin_seconds: float
) -> list[float | None]:
    n_bins = max(1, math.ceil(horizon / bin_seconds))
    sums = np.zeros(n_bins)
    counts = np.zeros(n_bins)
    for t, v in zip(times, values, strict=True):
        i = min(n_bins - 1, int(t // bin_seconds))
        sums[i] += v
        counts[i] += 1
    return [float(s / c) if c else None for s, c in zip(sums, counts, strict=True)]


def welch_warmup(
    series: Sequence[Sequence[float | None]], bin_seconds: float, window: int = 3, tolerance: float = 0.05
) -> WarmupAnalysis:
    """Welch's graphical method, automated.

    Average the per-bin metric across replications, smooth with a centred moving
    average, take the mean of the second half as the steady-state level, and
    suggest the first bin after which the smoothed curve stays within
    ``tolerance`` of it.
    """
    n_bins = max((len(s) for s in series), default=0)
    mean_series: list[float | None] = []
    for i in range(n_bins):
        vals: list[float] = [x for s in series if i < len(s) and (x := s[i]) is not None]
        mean_series.append(float(np.mean(vals)) if vals else None)

    smoothed: list[float | None] = []
    for i in range(n_bins):
        w = min(window, i, n_bins - 1 - i)
        window_vals: list[float] = [v for v in mean_series[i - w : i + w + 1] if v is not None]
        smoothed.append(float(np.mean(window_vals)) if window_vals else None)

    bins = [(i + 1) * bin_seconds for i in range(n_bins)]
    tail = [v for v in smoothed[n_bins // 2 :] if v is not None]
    if not tail:
        return WarmupAnalysis(
            bin_seconds=bin_seconds, window=window, bins=bins, mean_series=mean_series, smoothed=smoothed,
            suggested_warmup=None, steady_state=None,
        )  # fmt: skip
    steady = float(np.mean(tail))
    suggested: float | None = None
    for i in range(n_bins):
        rest = [v for v in smoothed[i:] if v is not None]
        if rest and all(abs(v - steady) <= tolerance * abs(steady) for v in rest):
            suggested = i * bin_seconds
            break
    return WarmupAnalysis(
        bin_seconds=bin_seconds, window=window, bins=bins, mean_series=mean_series, smoothed=smoothed,
        suggested_warmup=suggested, steady_state=steady,
    )  # fmt: skip
