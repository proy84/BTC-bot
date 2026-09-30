"""
data_exporter.py

Writes a live snapshot of the bot (active direction, net position,
break-even, reversal thresholds) plus the performance summary to the live
state file for a dashboard to poll. Every write is atomic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Union

from analytics import AnalyticsEngine

logger = logging.getLogger("bot.data_exporter")


class DataExporter:
    def __init__(self, state_path: Union[str, Path], analytics: AnalyticsEngine, recent_orders: int = 30):
        self.state_path = Path(state_path)
        self.analytics = analytics
        self.recent_orders = recent_orders

    def export(self, live: dict) -> None:
        payload = {
            "live": live,
            "performance_summary": asdict(self.analytics.summary()),
            "recent_reversals": [asdict(r) for r in self.analytics.h.reversals[-10:]],
            "recent_orders": [asdict(o) for o in self.analytics.h.orders[-self.recent_orders:]],
        }
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            logger.exception("Scrittura di %s fallita", self.state_path)
