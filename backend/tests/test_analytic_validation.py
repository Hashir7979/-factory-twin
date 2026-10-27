"""Validate the engine against closed-form queueing results.

If a DES engine cannot reproduce M/M/1 and M/M/c within statistical error, no
KPI it produces for a real line can be trusted.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sim import FactoryModel, compute_kpis
from tests.conftest import exp, single_station

REPLICATIONS = 8
HORIZON = 60_000.0
WARMUP = 2_000.0


def erlang_c(c: int, lam: float, mu: float) -> tuple[float, float]:
    """Return (Wq, L) for an M/M/c queue."""
    a = lam / mu
    rho = a / c
    tail = a**c / (math.factorial(c) * (1 - rho))
    p_wait = tail / (sum(a**k / math.factorial(k) for k in range(c)) + tail)
    wq = p_wait / (c * mu - lam)
    return wq, lam * (wq + 1 / mu)


def replicate(c: int, lam: float, mu: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    wq, L, thr = [], [], []
    for seed in range(1, REPLICATIONS + 1):
        sc = single_station(exp(1 / lam), exp(1 / mu), servers=c, horizon=HORIZON, warmup=WARMUP, seed=seed)
        model = FactoryModel(sc)
        model.run()
        k = compute_kpis(model)
        wq.append(k.buffers[0].mean_wait)
        L.append(k.line.mean_wip)
        thr.append(k.line.throughput_per_hour / 3600)
    return np.array(wq), np.array(L), np.array(thr)


def assert_within_ci(samples: np.ndarray, expected: float, rel_floor: float = 0.03) -> None:
    """Expected value must lie inside a 99% CI of the replication mean (with a small floor)."""
    mean = samples.mean()
    half = 3.5 * samples.std(ddof=1) / math.sqrt(len(samples))  # t(0.995, 7) ≈ 3.5
    tol = max(half, rel_floor * expected)
    assert abs(mean - expected) <= tol, f"mean {mean:.4f} vs analytic {expected:.4f} (±{tol:.4f})"


@pytest.mark.slow
def test_mm1_matches_theory() -> None:
    lam, mu = 0.7, 1.0
    wq, L, thr = replicate(1, lam, mu)
    rho = lam / mu
    assert_within_ci(wq, rho / (mu - lam))
    assert_within_ci(L, rho / (1 - rho))
    assert_within_ci(thr, lam, rel_floor=0.01)


@pytest.mark.slow
def test_mmc_matches_erlang_c() -> None:
    c, lam, mu = 3, 2.4, 1.0
    wq_expected, L_expected = erlang_c(c, lam, mu)
    wq, L, _ = replicate(c, lam, mu)
    assert_within_ci(wq, wq_expected)
    assert_within_ci(L, L_expected)


def test_littles_law_holds() -> None:
    sc = single_station(exp(1 / 0.8), exp(1.0), horizon=100_000, warmup=2_000, seed=3)
    model = FactoryModel(sc)
    model.run()
    line = compute_kpis(model).line
    lam = line.throughput_per_hour / 3600
    assert line.mean_wip == pytest.approx(lam * line.mean_cycle_time, rel=0.03)
