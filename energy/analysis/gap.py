from __future__ import annotations

from energy.analysis.schemas import GapFacts, Provenance


def compute_regret_summary(interval_history: list[dict]) -> GapFacts:
    """Compute overall recommendation regret summary from interval history dicts."""
    completed = [r for r in interval_history if r.get("rec_regret") is not None]

    if not completed:
        return GapFacts(
            total_recommendations=len(interval_history),
            completed_recommendations=0,
            avg_rec_regret=0.0,
            total_perfect_revenue=0.0,
            total_actual_revenue=0.0,
            gap_absolute=0.0,
            gap_pct=0.0,
            provenance=Provenance(method="compute_regret_summary", n_rows=0),
        )

    avg_regret = sum(r["rec_regret"] for r in completed) / len(completed)
    total_perfect = sum(r.get("perfect_foresight_revenue", 0.0) or 0.0 for r in completed)
    total_actual = sum(
        (r.get("perfect_foresight_revenue", 0.0) or 0.0) - (r["rec_regret"] or 0.0)
        for r in completed
    )
    gap = total_perfect - total_actual
    gap_pct = (gap / total_perfect * 100.0) if total_perfect > 0 else 0.0

    return GapFacts(
        total_recommendations=len(interval_history),
        completed_recommendations=len(completed),
        avg_rec_regret=avg_regret,
        total_perfect_revenue=total_perfect,
        total_actual_revenue=total_actual,
        gap_absolute=gap,
        gap_pct=gap_pct,
        provenance=Provenance(method="compute_regret_summary", n_rows=len(completed)),
    )
