"""Jittered schedule. Slots are anchored (anchor + k*interval) and each slot gets its own
deterministic jitter in [-j, +j], so (a) the average cadence stays exactly `interval` (no drift),
(b) gaps between runs vary (interval-2j .. interval+2j), and (c) a restart doesn't re-roll the
current slot (jitter is seeded by slot number)."""
from __future__ import annotations

import random


class JitterSchedule:
    def __init__(self, anchor: float, interval_s: float, jitter_s: float, seed: str = "tracker"):
        assert jitter_s * 2 < interval_s, "jitter must be < half the interval"
        self.anchor, self.interval, self.jitter, self.seed = anchor, interval_s, jitter_s, seed

    def slot_time(self, k: int) -> float:
        rng = random.Random(f"{self.seed}:{k}")
        return self.anchor + k * self.interval + rng.uniform(-self.jitter, self.jitter)

    def next_after(self, now: float) -> float:
        k = max(0, int((now - self.anchor) // self.interval) - 1)
        while True:
            t = self.slot_time(k)
            if t > now:
                return t
            k += 1
