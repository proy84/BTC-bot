"""
analytics.py

Persists TRX bot activity to `trx_bot_history.json`: every filled order,
every direction reversal and every closed position episode, plus a running
summary (orders, realized net, fees, funding, largest position reached).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Union


@dataclass(frozen=True)
class OrderRecord:
    timestamp_ms: int
    mode: str          # active direction when fired
    side: str          # "buy" | "sell"
    qty: float
    price: float
    fee: float
    position_side: str  # net position after the fill ("long" | "short" | "flat")
    position_qty: float


@dataclass(frozen=True)
class ReversalRecord:
    timestamp_ms: int
    from_mode: str
    to_mode: str
    price: float
    breakeven_net: float
    position_side: str
    position_qty: float


@dataclass(frozen=True)
class EpisodeRecord:
    timestamp_ms: int
    episode_id: int
    side: str
    realized_gross: float
    fees: float
    funding: float
    net: float


@dataclass(frozen=True)
class AnalyticsSummary:
    orders: int
    reversals: int
    closed_episodes: int
    realized_net_total: float
    fees_total: float
    funding_total: float
    max_position_qty: float


@dataclass
class _History:
    orders: List[OrderRecord] = field(default_factory=list)
    reversals: List[ReversalRecord] = field(default_factory=list)
    episodes: List[EpisodeRecord] = field(default_factory=list)


class AnalyticsEngine:
    """Owns the history file: append-only logs plus a recomputed summary."""

    def __init__(self, history_path: Union[str, Path]):
        self.history_path = Path(history_path)
        self.h = _History()
        self._load()

    def _load(self) -> None:
        if not self.history_path.exists():
            return
        raw = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.h.orders = [OrderRecord(**o) for o in raw.get("orders", [])]
        self.h.reversals = [ReversalRecord(**r) for r in raw.get("reversals", [])]
        self.h.episodes = [EpisodeRecord(**e) for e in raw.get("episodes", [])]

    def record_order(self, rec: OrderRecord) -> None:
        self.h.orders.append(rec)
        self._persist()

    def record_reversal(self, rec: ReversalRecord) -> None:
        self.h.reversals.append(rec)
        self._persist()

    def record_episode(self, rec: EpisodeRecord) -> None:
        self.h.episodes.append(rec)
        self._persist()

    def summary(self) -> AnalyticsSummary:
        return AnalyticsSummary(
            orders=len(self.h.orders),
            reversals=len(self.h.reversals),
            closed_episodes=len(self.h.episodes),
            realized_net_total=sum(e.net for e in self.h.episodes),
            fees_total=sum(o.fee for o in self.h.orders),
            funding_total=sum(e.funding for e in self.h.episodes),
            max_position_qty=max((o.position_qty for o in self.h.orders), default=0.0),
        )

    def _persist(self) -> None:
        payload = {
            "summary": asdict(self.summary()),
            "episodes": [asdict(e) for e in self.h.episodes],
            "reversals": [asdict(r) for r in self.h.reversals],
            "orders": [asdict(o) for o in self.h.orders],
        }
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(self.history_path)
