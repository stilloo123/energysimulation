from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.models import DispatchBid, DispatchResult


@dataclass
class BidRecord:
    bid_id: str
    interval_id: str
    trader_id: str
    trader_url: str
    direction: str
    volume_mw: float
    limit_price: float
    submitted_at: datetime
    result: DispatchResult | None = None


class MarketLedger:
    def __init__(self, grid_capacity_mw: float):
        self._bids: dict[str, BidRecord] = {}
        self._interval_bids: dict[str, list[str]] = {}
        self._grid_capacity_mw = grid_capacity_mw
        self._cleared_volume: float = 0.0
        self._cleared_revenue: float = 0.0

    def accept_bid(self, bid: DispatchBid) -> str:
        bid_id = bid.bid_id
        record = BidRecord(
            bid_id=bid_id,
            interval_id=bid.interval_id,
            trader_id=bid.trader_id,
            trader_url=bid.trader_url,
            direction=bid.direction,
            volume_mw=bid.volume_mw,
            limit_price=bid.limit_price,
            submitted_at=bid.submitted_at,
        )
        self._bids[bid_id] = record
        self._interval_bids.setdefault(bid.interval_id, []).append(bid_id)
        return bid_id

    def get_bid(self, bid_id: str) -> BidRecord | None:
        return self._bids.get(bid_id)

    def get_bids_for_interval(self, interval_id: str) -> list[BidRecord]:
        bid_ids = self._interval_bids.get(interval_id, [])
        return [self._bids[b] for b in bid_ids if b in self._bids]

    def clear_interval(
        self, interval_id: str, cleared_price: float
    ) -> list[DispatchResult]:
        bids = self.get_bids_for_interval(interval_id)
        now = datetime.now(timezone.utc)

        # Determine which bids execute at this cleared_price
        executable: list[BidRecord] = []
        for bid in bids:
            if bid.direction == "discharge" and cleared_price >= bid.limit_price:
                executable.append(bid)
            elif bid.direction == "charge" and cleared_price <= bid.limit_price:
                executable.append(bid)

        # Pro-rata curtailment if total exceeds grid capacity
        total_volume = sum(b.volume_mw for b in executable)
        curtailment_ratio = (
            min(1.0, self._grid_capacity_mw / total_volume)
            if total_volume > 0
            else 1.0
        )
        curtailed = total_volume > self._grid_capacity_mw

        results: list[DispatchResult] = []
        for bid in executable:
            volume_cleared = bid.volume_mw * curtailment_ratio
            # discharge earns money, charge costs money
            if bid.direction == "discharge":
                revenue = volume_cleared * cleared_price
            else:
                revenue = -(volume_cleared * cleared_price)

            result = DispatchResult(
                bid_id=bid.bid_id,
                interval_id=interval_id,
                cleared_price=cleared_price,
                volume_cleared=volume_cleared,
                revenue=revenue,
                dispatched_at=now,
                curtailed=curtailed,
            )
            bid.result = result
            results.append(result)
            self._cleared_volume += volume_cleared
            self._cleared_revenue += revenue

        # Bids that didn't execute get a zero result
        non_executable = [b for b in bids if b not in executable]
        for bid in non_executable:
            result = DispatchResult(
                bid_id=bid.bid_id,
                interval_id=interval_id,
                cleared_price=cleared_price,
                volume_cleared=0.0,
                revenue=0.0,
                dispatched_at=now,
                curtailed=False,
            )
            bid.result = result

        return results

    def stats(self) -> dict:
        return {
            "total_bids": len(self._bids),
            "cleared_volume_mwh": self._cleared_volume,
            "cleared_revenue": self._cleared_revenue,
        }
