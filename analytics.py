"""
analytics.py

Persists every closed BTC bot cycle (both legs, per-coin net, decision taken)
to `btc_bot_history.json` and derives running statistics: completed cycles
and sequences, max multiplier step reached, average cycle duration, total
fees/funding, cumulative net per coin and combined, max drawdown on the
combined cumulative net curve.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Union


@dataclass(frozen=True)
class LegResult:
    leg: str
    symbol: str
    side: str
    settle_coin: str
    qty: float
    entry_price: float
    exit_price: float
    notional: float
    gross_pnl: float
    open_fee: float
    close_fee: float
    funding: float
    net_pnl: float


@dataclass
class CycleRecord:
    cycle_id: int
    sequence_id: int
    step: int
    start_ts_ms: int
    end_ts_ms: int
    tp_leg: str
    legs: List[LegResult]
    net_by_coin: Dict[str, float]
    sequence_net_by_coin: Dict[str, float]
    decision: str
    spot_actions: List[str] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        return (self.end_ts_ms - self.start_ts_ms) / 1000.0

    @property
    def net_total(self) -> float:
        return sum(self.net_by_coin.values())


@dataclass(frozen=True)
class AnalyticsSummary:
    completed_cycles: int
    completed_sequences: int
    max_step_reached: int
    avg_cycle_duration_sec: float
    total_fees: float
    total_funding: float
    cumulative_net_by_coin: Dict[str, float]
    cumulative_net_total: float
    max_drawdown: float


_EMPTY = AnalyticsSummary(0, 0, 0, 0.0, 0.0, 0.0, {}, 0.0, 0.0)


class AnalyticsEngine:
    """Owns the history file: append-only cycle log plus a recomputed summary."""

    def __init__(self, history_path: Union[str, Path]):
        self.history_path = Path(history_path)
        self.cycles: List[CycleRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.history_path.exists():
            return
        raw = json.loads(self.history_path.read_text(encoding="utf-8"))
        for c in raw.get("cycles", []):
            c = dict(c)
            legs = [LegResult(**l) for l in c.pop("legs", [])]
            c.pop("duration_sec", None)
            c.pop("net_total", None)
            self.cycles.append(CycleRecord(legs=legs, **c))

    def record_cycle(self, cycle: CycleRecord) -> None:
        self.cycles.append(cycle)
        self._persist()

    def summary(self) -> AnalyticsSummary:
        if not self.cycles:
            return _EMPTY
        by_coin: Dict[str, float] = {}
        curve: List[float] = []
        running = 0.0
        for c in self.cycles:
            for coin, v in c.net_by_coin.items():
                by_coin[coin] = by_coin.get(coin, 0.0) + v
            running += c.net_total
            curve.append(running)
        return AnalyticsSummary(
            completed_cycles=len(self.cycles),
            completed_sequences=sum(1 for c in self.cycles if c.decision == "new_sequence"),
            max_step_reached=max(c.step for c in self.cycles),
            avg_cycle_duration_sec=statistics.fmean(c.duration_sec for c in self.cycles),
            total_fees=sum(l.open_fee + l.close_fee for c in self.cycles for l in c.legs),
            total_funding=sum(l.funding for c in self.cycles for l in c.legs),
            cumulative_net_by_coin=by_coin,
            cumulative_net_total=running,
            max_drawdown=self._max_drawdown(curve),
        )

    @staticmethod
    def _max_drawdown(curve: List[float]) -> float:
        peak = 0.0
        max_dd = 0.0
        for v in curve:
            peak = max(peak, v)
            max_dd = max(max_dd, peak - v)
        return max_dd

    def _persist(self) -> None:
        payload = {
            "cycles": [asdict(c) for c in self.cycles],
            "summary": asdict(self.summary()),
        }
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.history_path)
