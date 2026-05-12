from __future__ import annotations

from abc import ABC, abstractmethod

from shared.models import BatteryState, EnergyAgentCard, MarketInterval, Recommendation
from trader.battery import Battery
from trader.ledger import PerformanceRecord


class Brain(ABC):
    @abstractmethod
    def select_energy_agent(
        self,
        available: list[EnergyAgentCard],
        performance: dict[str, PerformanceRecord],
        battery_state: BatteryState,
        current_agent_url: str | None,
    ) -> str | None:
        ...

    @abstractmethod
    def decide_bid(
        self,
        recommendation: Recommendation | None,
        battery: Battery,
        interval_hours: float,
    ) -> tuple[str, float, float]:  # (direction, volume_mw, limit_price)
        ...


class DefaultTraderBrain(Brain):
    def __init__(self, cfg: dict):
        self._min_intervals = cfg["min_energy_agent_intervals"]
        self._switch_threshold = cfg["regret_switch_threshold"]

    def select_energy_agent(
        self,
        available: list[EnergyAgentCard],
        performance: dict[str, PerformanceRecord],
        battery_state: BatteryState,
        current_agent_url: str | None,
    ) -> str | None:
        if not available:
            return None

        available_urls = {card.url for card in available}

        # If current agent has gone offline, reset
        if current_agent_url and current_agent_url not in available_urls:
            current_agent_url = None

        # Check if current agent should be dropped
        if current_agent_url:
            perf = performance.get(current_agent_url)
            if perf and perf.intervals_advised >= self._min_intervals:
                recent = perf.recent_regrets[-10:]
                if len(recent) >= 10 and (sum(recent) / len(recent)) > self._switch_threshold:
                    current_agent_url = None  # trigger switch

        # If we have a valid current agent, stick with it
        if current_agent_url and current_agent_url in available_urls:
            return current_agent_url

        # Switch: pick the best agent with enough data, else first available
        eligible = [
            rec for url, rec in performance.items()
            if url in available_urls and rec.intervals_advised >= self._min_intervals
        ]
        if eligible:
            best_url = min(eligible, key=lambda r: r.avg_rec_regret).agent_url
            return best_url

        # No performance data yet — pick first available
        return available[0].url

    def decide_bid(
        self,
        recommendation: Recommendation | None,
        battery: Battery,
        interval_hours: float,
    ) -> tuple[str, float, float]:
        if recommendation is None or recommendation.direction == "none":
            return ("none", 0.0, 0.0)

        direction = recommendation.direction
        volume = recommendation.volume_mw
        limit_price = recommendation.limit_price

        # Scale down on low confidence
        if recommendation.confidence == "low":
            volume *= 0.5

        # Enforce physical battery limits
        if direction == "charge":
            if not battery.can_charge():
                return ("none", 0.0, 0.0)
            max_vol = battery.max_charge_volume(interval_hours)
            volume = min(volume, max_vol)
        elif direction == "discharge":
            if not battery.can_discharge():
                return ("none", 0.0, 0.0)
            max_vol = battery.max_discharge_volume(interval_hours)
            volume = min(volume, max_vol)

        if volume <= 0:
            return ("none", 0.0, 0.0)

        return (direction, volume, limit_price)
