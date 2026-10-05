from __future__ import annotations

from collections import defaultdict


class RunMetrics:
    """Per-run, per-carrier counters + latency. Emitted as one structured log line per run."""

    def __init__(self):
        self.counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.latency: dict[str, list[float]] = defaultdict(list)

    def count(self, carrier: str, name: str, n: int = 1) -> None:
        self.counters[carrier][name] += n

    def observe(self, carrier: str, seconds: float) -> None:
        self.latency[carrier].append(seconds)

    def summary(self) -> dict:
        out = {}
        for c, ctr in self.counters.items():
            lat = sorted(self.latency.get(c, []))
            out[c] = dict(ctr)
            if lat:
                out[c]["latency_p50_s"] = round(lat[len(lat) // 2], 2)
                out[c]["latency_max_s"] = round(lat[-1], 2)
        return out
