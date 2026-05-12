from __future__ import annotations

from energy.analysis.schemas import ForecastErrorEntry, ForecastErrorOutput, Provenance


def forecast_error_diagnostic(
    interval_history: list[dict], top_n: int = 20
) -> ForecastErrorOutput:
    """Compare recommended limit_price vs actual cleared_price."""
    entries = []
    for r in interval_history:
        limit = r.get("limit_price")
        cleared = r.get("cleared_price")
        if limit is None or cleared is None:
            continue
        entries.append(ForecastErrorEntry(
            interval_id=r.get("interval_id", ""),
            hour=r.get("hour_of_day", 0),
            direction=r.get("direction", "none"),
            limit_price=float(limit),
            cleared_price=float(cleared),
            error=float(limit) - float(cleared),
        ))

    # Sort by absolute error descending — worst predictions first
    entries.sort(key=lambda e: abs(e.error), reverse=True)
    top = entries[:top_n]

    mae = sum(abs(e.error) for e in entries) / len(entries) if entries else 0.0

    return ForecastErrorOutput(
        entries=top,
        mean_absolute_error=mae,
        provenance=Provenance(method="forecast_error_diagnostic", n_rows=len(entries)),
    )
