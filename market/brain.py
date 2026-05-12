from __future__ import annotations

import csv
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from shared.llm import complete

logger = logging.getLogger(__name__)

# Price validity ranges per market type
_PRICE_RANGES = {
    "energy": (0.0, 15000.0),
    "fcas_raise": (0.0, 500.0),
    "fcas_lower": (0.0, 500.0),
}

# Time-of-day fallback prices by hour (energy market)
_ENERGY_TOD = {
    0: 40, 1: 35, 2: 32, 3: 30, 4: 30, 5: 35,
    6: 65, 7: 110, 8: 130, 9: 95, 10: 75, 11: 70,
    12: 65, 13: 60, 14: 65, 15: 75, 16: 90, 17: 140,
    18: 200, 19: 180, 20: 130, 21: 90, 22: 65, 23: 50,
}


class Brain(ABC):
    @abstractmethod
    def generate_prices(
        self,
        upcoming_slots: list[dict],
        recent_cleared: list[dict],
    ) -> list[float]:
        ...


class DefaultMarketBrain(Brain):
    def __init__(self, agents_md_path: str, model: str):
        self._model = model
        self._system = Path(agents_md_path).read_text()

    def generate_prices(
        self,
        upcoming_slots: list[dict],
        recent_cleared: list[dict],
    ) -> list[float]:
        prompt = (
            f"Recent cleared prices (last {len(recent_cleared)} intervals):\n"
            + json.dumps(recent_cleared, default=str)
            + f"\n\nGenerate reference prices for these upcoming slots:\n"
            + json.dumps(upcoming_slots, default=str)
        )
        try:
            raw = complete(
                self._model,
                [
                    {"role": "system", "content": self._system},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1000,
            )
            data = json.loads(raw)
            prices = []
            for i, entry in enumerate(data["intervals"]):
                price = float(entry["reference_price"])
                market_type = upcoming_slots[i]["market_type"]
                lo, hi = _PRICE_RANGES.get(market_type, (0.0, 15000.0))
                prices.append(max(lo, min(hi, price)))
            return prices
        except Exception as exc:
            logger.warning("LLM price generation failed (%s), using fallback", exc)
            return [_fallback_price(s) for s in upcoming_slots]


class CSVMarketBrain(Brain):
    """Replays real NEM prices from a BLYTHB1-format CSV sequentially.

    Advances through the CSV row-by-row so price patterns are independent
    of wall-clock time. Start hour is configurable (default 16:00 to hit
    the evening price spike quickly).
    """

    def __init__(self, csv_path: str, start_hour: int = 0):
        self._forecast: list[float] = []  # sequential reference prices
        self._cleared: list[float] = []   # sequential cleared prices
        self._idx = 0
        # scheduled_at ISO string → cleared price (filled during generate_prices)
        self._scheduled_cleared: dict[str, float] = {}
        self._load(csv_path, start_hour)
        logger.info(
            "CSVMarketBrain loaded %d intervals from %s (starting hour=%d)",
            len(self._forecast), csv_path, start_hour,
        )

    def _load(self, path: str, start_hour: int) -> None:
        rows: list[tuple[datetime, float, float]] = []
        cleared_by_key: dict[tuple[int, int], float] = {}
        forecast_by_key: dict[tuple[int, int], float] = {}

        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    dt = datetime.strptime(
                        row["START_DATETIME"].strip(), "%Y-%m-%d %H:%M:%S.%f %z"
                    ).astimezone(timezone.utc)
                    price = float(row["PRICE_ENERGY"])
                    key = (dt.hour, dt.minute)
                    schedule_type = row.get("SCHEDULE_TYPE", "")
                    if schedule_type == "cleared":
                        cleared_by_key[key] = price
                    elif schedule_type == "expected":
                        forecast_by_key[key] = price
                except (KeyError, ValueError):
                    continue

        # Build a sorted list of (minute_of_day, forecast, cleared) pairs
        all_keys = sorted(set(cleared_by_key) | set(forecast_by_key))
        pairs = [(h * 60 + m, forecast_by_key.get((h, m), cleared_by_key.get((h, m), 70.0)),
                  cleared_by_key.get((h, m), 70.0)) for h, m in all_keys]
        pairs.sort()

        # Rotate so we start at start_hour
        start_minutes = start_hour * 60
        start_idx = next((i for i, (mins, _, _) in enumerate(pairs) if mins >= start_minutes), 0)
        pairs = pairs[start_idx:] + pairs[:start_idx]

        self._forecast = [f for _, f, _ in pairs]
        self._cleared = [c for _, _, c in pairs]

    def generate_prices(
        self,
        upcoming_slots: list[dict],
        recent_cleared: list[dict],
    ) -> list[float]:
        prices = []
        for slot in upcoming_slots:
            if self._idx < len(self._forecast):
                forecast = self._forecast[self._idx]
                cleared = self._cleared[self._idx]
                scheduled_at = slot.get("scheduled_at", "")
                if scheduled_at:
                    self._scheduled_cleared[scheduled_at] = cleared
                prices.append(max(0.0, forecast))
                self._idx = (self._idx + 1) % len(self._forecast)
            else:
                prices.append(_fallback_price(slot))
        return prices

    def cleared_price_for(self, scheduled_at: str) -> float | None:
        """Return cleared price for an interval by its scheduled_at ISO string."""
        return self._scheduled_cleared.get(scheduled_at)


def _fallback_price(slot: dict) -> float:
    market_type = slot.get("market_type", "energy")
    hour = slot.get("hour", 12)
    if market_type == "energy":
        return float(_ENERGY_TOD.get(hour, 70))
    elif market_type == "fcas_raise":
        return 15.0
    else:
        return 8.0
