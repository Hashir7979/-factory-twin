"""Random number streams and distribution samplers.

Each stochastic element gets its own independent stream derived from the run
seed and a stable name (e.g. ``"weld#1:process"``). This gives *common random
numbers*: two scenarios run with the same seed see the same random draws for
the same element, so differences between them come from the design change and
not from sampling noise. That sharpens A/B comparisons considerably.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable

import numpy as np

from .config import (
    Constant,
    Distribution,
    Exponential,
    Lognormal,
    Normal,
    Triangular,
    Uniform,
    Weibull,
)

Sampler = Callable[[], float]

_MAX_RESAMPLES = 1000


class Streams:
    def __init__(self, seed: int) -> None:
        self.seed = seed

    def rng(self, name: str) -> np.random.Generator:
        # crc32 (not hash()) so stream assignment is stable across processes/runs.
        key = zlib.crc32(name.encode("utf-8"))
        return np.random.default_rng(np.random.SeedSequence([self.seed, key]))


def make_sampler(dist: Distribution, rng: np.random.Generator) -> Sampler:
    if isinstance(dist, Constant):
        value = dist.value
        return lambda: value
    if isinstance(dist, Exponential):
        mean = dist.mean
        return lambda: float(rng.exponential(mean))
    if isinstance(dist, Uniform):
        lo, hi = dist.low, dist.high
        return lambda: float(rng.uniform(lo, hi))
    if isinstance(dist, Triangular):
        lo, mode, hi = dist.low, dist.mode, dist.high
        return lambda: float(rng.triangular(lo, mode, hi))
    if isinstance(dist, Normal):
        mean, std, lo = dist.mean, dist.std, dist.min

        def normal() -> float:
            for _ in range(_MAX_RESAMPLES):
                x = float(rng.normal(mean, std))
                if x >= lo:
                    return x
            return max(lo, mean)  # pathological parameters; never loop forever

        return normal
    if isinstance(dist, Lognormal):
        # Convert the desired mean/std of X into mu/sigma of ln(X).
        sigma2 = math.log(1 + (dist.std / dist.mean) ** 2)
        mu = math.log(dist.mean) - sigma2 / 2
        sigma = math.sqrt(sigma2)
        return lambda: float(rng.lognormal(mu, sigma))
    if isinstance(dist, Weibull):
        shape, scale = dist.shape, dist.scale
        return lambda: float(scale * rng.weibull(shape))
    raise TypeError(f"unsupported distribution: {type(dist).__name__}")  # pragma: no cover
