from __future__ import annotations

from energy.analysis.schemas import InspectEntry, InspectOutput, Provenance


def inspect_intervals(
    interval_history: list[dict],
    hour_min: int | None = None,
    hour_max: int | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    soc_min: float | None = None,
    soc_max: float | None = None,
    direction: str | None = None,
    limit: int = 50,
) -> InspectOutput:
    """Filter interval history by multiple criteria and return matching records."""
    results = []
    for r in interval_history:
        hour = r.get("hour_of_day")
        price = r.get("cleared_price")
        soc = r.get("soc_pct")
        d = r.get("direction", "none")

        if hour_min is not None and (hour is None or hour < hour_min):
            continue
        if hour_max is not None and (hour is None or hour > hour_max):
            continue
        if price_min is not None and (price is None or price < price_min):
            continue
        if price_max is not None and (price is None or price > price_max):
            continue
        if soc_min is not None and (soc is None or float(soc) < soc_min):
            continue
        if soc_max is not None and (soc is None or float(soc) > soc_max):
            continue
        if direction is not None and d != direction:
            continue

        results.append(InspectEntry(
            interval_id=r.get("interval_id", ""),
            hour=hour or 0,
            market_type=r.get("market_type", "energy"),
            direction=d,
            cleared_price=float(price or 0),
            limit_price=float(r.get("limit_price", 0) or 0),
            volume_mw=float(r.get("volume_mw", 0) or 0),
            soc_pct=float(soc or 0),
            rec_regret=r.get("rec_regret"),
        ))

    results = results[-limit:]  # most recent N

    return InspectOutput(
        entries=results,
        provenance=Provenance(method="inspect_intervals", n_rows=len(results)),
    )
