from __future__ import annotations

from energy.ledger import LearningLedger


def execute_retrieval(ledger: LearningLedger, args: dict) -> dict:
    """Execute find_similar_intervals against the live LearningLedger."""
    soc_pct = float(args.get("soc_pct", 0.5))
    hour = int(args.get("hour_of_day", 12))
    slope = float(args.get("price_trend_slope", 0.0))
    mtype = str(args.get("market_type", "energy"))

    records = ledger.similar_intervals(soc_pct, hour, slope, mtype, top_k=10)

    examples = []
    for r in records:
        snap = r.market_state_snapshot
        examples.append({
            "interval_id": r.recommendation.interval_id,
            "hour": snap.get("hour_of_day"),
            "soc_pct": snap.get("soc_pct"),
            "market_type": snap.get("market_type"),
            "recommended_direction": r.recommendation.direction,
            "volume_mw": r.recommendation.volume_mw,
            "limit_price": r.recommendation.limit_price,
            "cleared_price": r.cleared_price,
            "rec_regret": r.rec_regret,
            "confidence": r.recommendation.confidence,
        })

    return {
        "n_found": len(examples),
        "examples": examples,
        "provenance": {
            "method": "find_similar_intervals",
            "n_rows": len(examples),
            "version": "0.2.0",
            "query": {"soc_pct": soc_pct, "hour": hour, "market_type": mtype},
        },
    }
