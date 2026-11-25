"""The L4 evaluation plane: common metrics collected in one place.

Metrics are intentionally simple and honest.  Throughput is computed over
the whole process lifetime (or an injected window), latency percentiles use
the nearest-rank method on observed samples, and message counts come from
the simulated network, which counts every send attempt.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


def percentile(samples: list[float], p: float) -> float:
    """Nearest-rank percentile (p in [0, 100]); returns 0.0 for no samples."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if p <= 0:
        return ordered[0]
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


@dataclass
class MetricsSnapshot:
    started_at: float
    uptime_s: float
    counters: dict[str, int]
    latencies_ms: dict[str, dict[str, float]]
    throughput_records_per_s: float
    proof: dict[str, Any]
    messages: dict[str, dict[str, int]]
    messages_per_entry: dict[str, dict[str, float]]
    ledger: dict[str, Any]
    audit: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)


class MetricsCollector:
    """Thread-safe-ish collector (the app is single-event-loop)."""

    def __init__(self, max_samples: int = 200_000) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        self._counters: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, list[float]] = defaultdict(list)
        self._max_samples = max_samples
        self._messages: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._entry_messages: dict[str, list[int]] = defaultdict(list)
        self._proof_sizes: list[int] = []
        self._proof_verify_ms: list[float] = []
        self._audit_delays_ms: list[float] = []
        self._ledger: dict[str, Any] = {}
        self._extra: dict[str, Any] = {}

    # -- counters ---------------------------------------------------------
    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def set_gauge(self, name: str, value: Any) -> None:
        with self._lock:
            self._extra[name] = value

    # -- latency ----------------------------------------------------------
    def record_latency(self, name: str, latency_ms: float) -> None:
        with self._lock:
            bucket = self._latencies[name]
            if len(bucket) < self._max_samples:
                bucket.append(max(0.0, latency_ms))

    def record_message(self, protocol: str, kind: str = "sent") -> None:
        with self._lock:
            self._messages[protocol][kind] += 1

    def record_entry_messages(self, data_class: str, count: int) -> None:
        """Record protocol messages observed for one committed entry."""
        with self._lock:
            self._entry_messages[data_class].append(int(count))

    def record_proof(self, proof_bytes: int, verify_ms: float) -> None:
        with self._lock:
            self._proof_sizes.append(proof_bytes)
            self._proof_verify_ms.append(verify_ms)

    def record_audit_delay(self, delay_ms: float) -> None:
        with self._lock:
            self._audit_delays_ms.append(max(0.0, delay_ms))

    def set_ledger(self, **values: Any) -> None:
        with self._lock:
            self._ledger.update(values)

    # -- output -----------------------------------------------------------
    def _latency_summary(self, samples: list[float]) -> dict[str, float]:
        return {
            "count": float(len(samples)),
            "p50": percentile(samples, 50),
            "p95": percentile(samples, 95),
            "p99": percentile(samples, 99),
            "mean": (sum(samples) / len(samples)) if samples else 0.0,
        }

    def snapshot(self) -> MetricsSnapshot:
        now = time.time()
        with self._lock:
            counters = dict(self._counters)
            latencies = {k: self._latency_summary(v) for k, v in self._latencies.items()}
            proof = {
                "count": len(self._proof_sizes),
                "avg_hash_count": (sum(self._proof_sizes) / len(self._proof_sizes) / 32)
                if self._proof_sizes
                else 0.0,
                "avg_bytes": (sum(self._proof_sizes) / len(self._proof_sizes))
                if self._proof_sizes
                else 0.0,
                "verify_ms": self._latency_summary(self._proof_verify_ms),
            }
            messages = {k: dict(v) for k, v in self._messages.items()}
            messages_per_entry = {
                k: self._latency_summary([float(x) for x in v]) for k, v in self._entry_messages.items()
            }
            audit = self._latency_summary(self._audit_delays_ms)
            ledger = dict(self._ledger)
            extra = dict(self._extra)
            accepted = counters.get("records_accepted", 0)
            uptime = max(now - self._started, 1e-9)
        return MetricsSnapshot(
            started_at=self._started,
            uptime_s=uptime,
            counters=counters,
            latencies_ms=latencies,
            throughput_records_per_s=accepted / uptime,
            proof=proof,
            messages=messages,
            messages_per_entry=messages_per_entry,
            ledger=ledger,
            audit=audit,
            extra=extra,
        )

    def as_dict(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "uptime_s": round(snap.uptime_s, 3),
            "started_at": snap.started_at,
            "counters": snap.counters,
            "throughput_records_per_s": round(snap.throughput_records_per_s, 3),
            "latencies_ms": snap.latencies_ms,
            "proof": snap.proof,
            "messages": snap.messages,
            "messages_per_entry": snap.messages_per_entry,
            "ledger": snap.ledger,
            "audit": snap.audit,
            "extra": snap.extra,
        }
