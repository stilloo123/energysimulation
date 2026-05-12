from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from shared.models import DispatchResult
from trader.battery import Battery


@dataclass
class DispatchRecord:
    bid_id: str
    interval_id: str
    market_url: str
    energy_agent_url: str | None
    direction: str
    volume_mw: float
    revenue: float
    soc_before: float
    soc_after: float
    timestamp: datetime


@dataclass
class PerformanceRecord:
    agent_url: str
    intervals_advised: int = 0
    total_rec_regret: float = 0.0
    recent_regrets: list[float] = field(default_factory=list)
    confidence_correct: int = 0
    confidence_total: int = 0
    last_queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def avg_rec_regret(self) -> float:
        if self.intervals_advised == 0:
            return 0.0
        return self.total_rec_regret / self.intervals_advised

    @property
    def confidence_accuracy(self) -> float:
        if self.confidence_total == 0:
            return 0.0
        return self.confidence_correct / self.confidence_total


class TraderLedger:
    def __init__(self, seed_balance: float, battery: Battery):
        self.balance: float = seed_balance
        self.seed_balance: float = seed_balance
        self._battery = battery
        self._records: list[DispatchRecord] = []
        self._pending: dict[str, dict] = {}  # bid_id → metadata
        self._energy_performance: dict[str, PerformanceRecord] = {}
        self._already_bid: set[str] = set()  # interval_ids bid this cycle

    def record_pending(
        self,
        bid_id: str,
        interval_id: str,
        market_url: str,
        energy_agent_url: str | None,
        direction: str,
        volume_mw: float,
        soc_before: float,
        rec_confidence: str | None = None,
    ) -> None:
        self._pending[bid_id] = {
            "interval_id": interval_id,
            "market_url": market_url,
            "energy_agent_url": energy_agent_url,
            "direction": direction,
            "volume_mw": volume_mw,
            "soc_before": soc_before,
            "rec_confidence": rec_confidence,
            "submitted_at": datetime.now(timezone.utc),
        }

    def record_settled(
        self, bid_id: str, result: DispatchResult, soc_after: float
    ) -> None:
        pending = self._pending.pop(bid_id, None)
        if pending is None:
            return
        self.balance += result.revenue
        record = DispatchRecord(
            bid_id=bid_id,
            interval_id=pending["interval_id"],
            market_url=pending["market_url"],
            energy_agent_url=pending["energy_agent_url"],
            direction=pending["direction"],
            volume_mw=pending["volume_mw"],
            revenue=result.revenue,
            soc_before=pending["soc_before"],
            soc_after=soc_after,
            timestamp=datetime.now(timezone.utc),
        )
        self._records.append(record)

    def update_agent_performance(
        self,
        agent_url: str,
        rec_regret: float,
        confidence_was_correct: bool | None = None,
    ) -> None:
        if agent_url not in self._energy_performance:
            self._energy_performance[agent_url] = PerformanceRecord(agent_url=agent_url)
        rec = self._energy_performance[agent_url]
        rec.intervals_advised += 1
        rec.total_rec_regret += rec_regret
        rec.recent_regrets.append(rec_regret)
        if len(rec.recent_regrets) > 10:
            rec.recent_regrets.pop(0)
        if confidence_was_correct is not None:
            rec.confidence_total += 1
            if confidence_was_correct:
                rec.confidence_correct += 1
        rec.last_queried_at = datetime.now(timezone.utc)

    def should_switch_agent(
        self, current_url: str, min_intervals: int, threshold: float
    ) -> bool:
        rec = self._energy_performance.get(current_url)
        if rec is None or rec.intervals_advised < min_intervals:
            return False
        recent = rec.recent_regrets[-10:]
        return len(recent) >= 10 and (sum(recent) / len(recent)) > threshold

    def best_energy_agent(self) -> str | None:
        eligible = [
            rec for rec in self._energy_performance.values()
            if rec.intervals_advised >= 5
        ]
        if not eligible:
            return None
        return min(eligible, key=lambda r: r.avg_rec_regret).agent_url

    def prune_already_bid(self, current_interval_ids: set[str]) -> None:
        self._already_bid &= current_interval_ids

    def profit(self) -> float:
        return self.balance - self.seed_balance

    def roi(self) -> float:
        if self.seed_balance == 0:
            return 0.0
        return self.profit() / self.seed_balance

    def total_volume(self) -> float:
        return sum(r.volume_mw for r in self._records)

    def stats(self) -> dict:
        return {
            "balance": self.balance,
            "profit": self.profit(),
            "roi": self.roi(),
            "total_volume_mwh": self.total_volume(),
            "total_bids": len(self._records),
            "pending_bids": len(self._pending),
            "energy_agents_tracked": len(self._energy_performance),
        }
