"""
data_exporter.py

Writes a live snapshot of BTC bot (both legs, two-leg net per leg, sequence
state) plus the performance summary to `btc_bot_live.json` for a dashboard to
poll. Every write is atomic (temp file + replace).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Union

from analytics import AnalyticsEngine

logger = logging.getLogger("btc_bot.data_exporter")


class DataExporter:
    def __init__(self, state_path: Union[str, Path], analytics: AnalyticsEngine, history_limit: int = 50):
        self.state_path = Path(state_path)
        self.analytics = analytics
        self.history_limit = history_limit

    def export(self, live: dict) -> None:
        payload = {
            "live": live,
            "performance_summary": asdict(self.analytics.summary()),
            "recent_cycles": [
                {
                    "cycle_id": c.cycle_id,
                    "sequence_id": c.sequence_id,
                    "step": c.step,
                    "tp_leg": c.tp_leg,
                    "duration_sec": c.duration_sec,
                    "net_by_coin": c.net_by_coin,
                    "decision": c.decision,
                }
                for c in self.analytics.cycles[-self.history_limit:]
            ],
        }
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            logger.exception("Scrittura di %s fallita", self.state_path)
