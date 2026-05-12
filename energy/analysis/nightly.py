from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from energy.analysis.gap import compute_regret_summary
from energy.analysis.slices import slice_by_hour, slice_by_soc_regime, slice_by_price_bucket

logger = logging.getLogger(__name__)

_INTERVAL_HOURS = 5.0 / 60.0
_STRATEGY_PATH = Path("energy/strategy_context.md")


class NightlyAnalysis:
    """Seeds strategy_context.md from CSV data before live trading starts.

    Converts historical/cleared + historical/expected rows into interval_history
    format, runs the standard slice analysis, and calls brain.distill_lessons to
    produce actionable rules — the same format the live AdaptationLoop produces.
    """

    def __init__(self, csv_path: str, brain) -> None:
        self._csv_path = csv_path
        self._brain = brain

    def run(self) -> list[str]:
        cleared_rows, forecast_map = _load_csv(self._csv_path)
        if not cleared_rows:
            logger.warning("NightlyAnalysis: no cleared rows in %s", self._csv_path)
            return []

        history = _build_history(cleared_rows, forecast_map)
        if not history:
            return []

        gap = compute_regret_summary(history)
        hour_slices = slice_by_hour(history)
        soc_slices = slice_by_soc_regime(history)

        logger.info(
            "NightlyAnalysis: %d intervals, avg_regret=%.2f, gap=$%.2f",
            gap.completed_recommendations,
            gap.avg_rec_regret,
            gap.gap_absolute,
        )

        try:
            lessons = self._brain.distill_lessons(gap, [hour_slices, soc_slices], [])
        except Exception as exc:
            logger.warning("NightlyAnalysis: distill_lessons failed: %s", exc)
            return []

        if lessons:
            _write_strategy_context(lessons, gap)
            logger.info("NightlyAnalysis: wrote %d strategy rules", len(lessons))

        return lessons


def _load_csv(path: str) -> tuple[list[dict], dict[tuple[int, int], float]]:
    """Load cleared rows and build forecast price map keyed by (hour_utc, min_utc)."""
    cleared: list[dict] = []
    forecast_map: dict[tuple[int, int], float] = {}

    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(
                    row["START_DATETIME"].strip(), "%Y-%m-%d %H:%M:%S.%f %z"
                ).astimezone(timezone.utc)
            except (KeyError, ValueError):
                continue

            scenario = row.get("SCENARIO_NAME", "")
            schedule_type = row.get("SCHEDULE_TYPE", "")
            if scenario == "historical" and schedule_type == "cleared":
                cleared.append({**row, "_dt": dt})
            elif scenario == "historical" and schedule_type == "expected":
                try:
                    forecast_map[(dt.hour, dt.minute)] = float(row["PRICE_ENERGY"])
                except (KeyError, ValueError):
                    pass

    return cleared, forecast_map


def _build_history(
    cleared: list[dict],
    forecast_map: dict[tuple[int, int], float],
) -> list[dict]:
    """Convert cleared CSV rows to the interval_history dict format."""
    history = []
    for row in cleared:
        try:
            dt: datetime = row["_dt"]
            charge = float(row["CHARGE_ENERGY"])
            discharge = float(row["DISCHARGE_ENERGY"])
            cleared_price = float(row["PRICE_ENERGY"])
            revenue = float(row["REVENUE"])
            soc_pct = float(row["SOC"]) / 100.0

            if discharge > charge:
                direction = "discharge"
                volume_mw = discharge / _INTERVAL_HOURS
            elif charge > 0:
                direction = "charge"
                volume_mw = charge / _INTERVAL_HOURS
            else:
                direction = "none"
                volume_mw = 0.0

            # Pre-dispatch forecast price: what the operator saw before committing
            forecast_price = forecast_map.get((dt.hour, dt.minute), cleared_price)

            # Perfect foresight: earn full cleared_price on whatever was dispatched
            perfect_foresight_revenue = max(0.0, volume_mw * _INTERVAL_HOURS * max(0.0, cleared_price))

            # Actual revenue (from CSV; may be negative for curtailed/mispriced intervals)
            actual_revenue = max(0.0, revenue)

            # Regret = gap between perfect and actual for dispatched intervals.
            # For idle intervals, flag missed opportunity when forecast undershot cleared.
            if volume_mw > 0:
                rec_regret = max(0.0, perfect_foresight_revenue - actual_revenue)
            elif cleared_price > 0 and forecast_price < cleared_price * 0.8:
                # Forecast was >20% below actual — likely why battery sat idle
                rec_regret = max(0.0, cleared_price - forecast_price) * _INTERVAL_HOURS
            else:
                rec_regret = 0.0

            # limit_price proxy: treat forecast price as what the operator bid against
            limit_price = max(0.0, forecast_price)

            history.append({
                "interval_id": f"csv_{dt.hour:02d}{dt.minute:02d}",
                "market_type": "energy",
                "hour_of_day": dt.hour,
                "cleared_price": cleared_price,
                "direction": direction,
                "volume_mw": volume_mw,
                "limit_price": limit_price,
                "rec_regret": rec_regret,
                "perfect_foresight_revenue": perfect_foresight_revenue,
                "soc_pct": soc_pct,
            })
        except (KeyError, ValueError, ZeroDivisionError):
            continue

    return history


def _write_strategy_context(lessons: list[str], gap) -> None:
    lines = [
        "# Strategy Context — Bootstrapped from Historical CSV",
        "",
        f"avg_regret_proxy: {gap.avg_rec_regret:.2f}  |  "
        f"intervals_analysed: {gap.completed_recommendations}",
        "",
        "## Learned Rules",
        "",
    ]
    for i, rule in enumerate(lessons, 1):
        lines.append(f"{i}. {rule}")
    lines.append("")
    _STRATEGY_PATH.write_text("\n".join(lines))
