from __future__ import annotations

import uuid
from datetime import datetime, timezone

from shared.models import MarketInterval


class IntervalScheduler:
    def __init__(self, cfg: dict):
        self._intervals: dict[str, MarketInterval] = {}
        self._interval_minutes: float = cfg["interval_minutes"]
        self._multiplier: float = cfg["time_multiplier"]
        # How long each interval lasts in real (wall-clock) seconds
        self.real_seconds: float = (self._interval_minutes * 60) / self._multiplier
        self._bid_window_pct: float = cfg["bid_window_pct"]

    def create_interval(
        self, market_type: str, scheduled_at: datetime, reference_price: float | None
    ) -> MarketInterval:
        bid_window_seconds = self.real_seconds * self._bid_window_pct
        bid_open_at = _add_seconds(scheduled_at, -bid_window_seconds)
        bid_close_at = scheduled_at
        interval = MarketInterval(
            interval_id=str(uuid.uuid4()),
            market_type=market_type,
            scheduled_at=scheduled_at,
            bid_open_at=bid_open_at,
            bid_close_at=bid_close_at,
            dispatch_at=scheduled_at,
            status="scheduled",
            cleared_price=None,
            reference_price=reference_price,
        )
        self._intervals[interval.interval_id] = interval
        return interval

    def open_interval(self, interval_id: str) -> None:
        iv = self._intervals[interval_id]
        self._intervals[interval_id] = iv.model_copy(update={"status": "open"})

    def close_interval(self, interval_id: str) -> None:
        iv = self._intervals[interval_id]
        self._intervals[interval_id] = iv.model_copy(update={"status": "closed"})

    def dispatch_interval(self, interval_id: str, cleared_price: float) -> None:
        iv = self._intervals[interval_id]
        self._intervals[interval_id] = iv.model_copy(
            update={"status": "dispatched", "cleared_price": cleared_price}
        )

    def get_interval(self, interval_id: str) -> MarketInterval | None:
        return self._intervals.get(interval_id)

    def get_open_intervals(self) -> list[MarketInterval]:
        return [iv for iv in self._intervals.values() if iv.status == "open"]

    def get_upcoming_intervals(self, n: int) -> list[MarketInterval]:
        upcoming = [
            iv for iv in self._intervals.values()
            if iv.status in ("scheduled", "open")
        ]
        upcoming.sort(key=lambda iv: iv.scheduled_at)
        return upcoming[:n]

    def get_dispatched_intervals(self, since: datetime) -> list[MarketInterval]:
        return [
            iv for iv in self._intervals.values()
            if iv.status == "dispatched" and iv.dispatch_at >= since
        ]

    def all_intervals(self) -> list[MarketInterval]:
        return list(self._intervals.values())

    def next_scheduled_at(self) -> datetime:
        upcoming = self.get_upcoming_intervals(999)
        if upcoming:
            return _add_seconds(upcoming[-1].scheduled_at, self.real_seconds)
        return _add_seconds(datetime.now(timezone.utc), self.real_seconds)


def _add_seconds(dt: datetime, seconds: float) -> datetime:
    from datetime import timedelta
    return dt + timedelta(seconds=seconds)
