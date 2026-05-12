from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from shared.models import Recommendation, RecommendationRecord

# 5-min interval expressed in hours
_INTERVAL_HOURS = 5.0 / 60.0


class LearningLedger:
    def __init__(self):
        self._records: dict[str, RecommendationRecord] = {}
        self._by_interval: dict[str, str] = {}
        self._pending: set[str] = set()

    def record_recommendation(self, rec: Recommendation, market_state: dict) -> None:
        record = RecommendationRecord(
            recommendation=rec,
            market_state_snapshot=market_state,
            cleared_price=None,
            perfect_foresight_revenue=None,
            rec_regret=None,
            ingested_at=None,
        )
        self._records[rec.recommendation_id] = record
        self._by_interval[rec.interval_id] = rec.recommendation_id
        if rec.direction != "none":
            self._pending.add(rec.interval_id)

    def get_pending_interval_ids(self) -> list[str]:
        return list(self._pending)

    def get_record_by_interval(self, interval_id: str) -> RecommendationRecord | None:
        rec_id = self._by_interval.get(interval_id)
        return self._records.get(rec_id) if rec_id else None

    def fill_result(self, interval_id: str, cleared_price: float) -> None:
        """Store cleared price and compute rec_regret."""
        rec_id = self._by_interval.get(interval_id)
        if rec_id is None or rec_id not in self._records:
            self._pending.discard(interval_id)
            return

        record = self._records[rec_id]
        rec = record.recommendation

        # Perfect foresight: always discharge at cleared price for this interval
        perfect = rec.volume_mw * _INTERVAL_HOURS * cleared_price

        # Revenue if the recommendation had been followed exactly
        if rec.direction == "discharge":
            revenue_if_followed = (
                rec.volume_mw * _INTERVAL_HOURS * cleared_price
                if cleared_price >= rec.limit_price
                else 0.0
            )
        elif rec.direction == "charge":
            revenue_if_followed = (
                -(rec.volume_mw * _INTERVAL_HOURS * cleared_price)
                if cleared_price <= rec.limit_price
                else 0.0
            )
        else:
            revenue_if_followed = 0.0

        rec_regret = perfect - revenue_if_followed

        self._records[rec_id] = record.model_copy(
            update={
                "cleared_price": cleared_price,
                "perfect_foresight_revenue": perfect,
                "rec_regret": rec_regret,
                "ingested_at": datetime.now(timezone.utc),
            }
        )
        self._pending.discard(interval_id)

    def all_completed(self) -> list[RecommendationRecord]:
        return [r for r in self._records.values() if r.rec_regret is not None]

    def similar_intervals(
        self,
        soc_pct: float,
        hour: int,
        price_slope: float,
        market_type: str,
        top_k: int = 10,
    ) -> list[RecommendationRecord]:
        candidates = [r for r in self._records.values() if r.cleared_price is not None]

        def score(r: RecommendationRecord) -> float:
            snap = r.market_state_snapshot
            r_hour = snap.get("hour_of_day", 0)
            r_soc = snap.get("soc_pct", 0.5)
            r_type = snap.get("market_type", "energy")
            hour_sim = max(0.0, 1.0 - abs(r_hour - hour) / 12.0)
            soc_sim = max(0.0, 1.0 - abs(r_soc - soc_pct) / 0.5)
            type_sim = 1.0 if r_type == market_type else 0.0
            return (hour_sim * 2 + soc_sim + type_sim) / 4.0

        scored = sorted(candidates, key=score, reverse=True)
        return scored[:top_k]

    def regret_clusters(self, n: int = 200) -> list[dict]:
        completed = self.all_completed()[-n:]
        if not completed:
            return []

        buckets: dict[tuple, list] = defaultdict(list)
        for r in completed:
            snap = r.market_state_snapshot
            hour = snap.get("hour_of_day", 0)
            soc = snap.get("soc_pct", 0.5)
            mtype = snap.get("market_type", "energy")
            hour_bucket = (hour // 4) * 4
            soc_bucket = "low" if soc < 0.3 else "high" if soc > 0.7 else "mid"
            buckets[(hour_bucket, soc_bucket, mtype)].append(r)

        clusters = []
        for (hour_bucket, soc_bucket, mtype), records in buckets.items():
            if len(records) < 3:
                continue
            avg_regret = sum(r.rec_regret for r in records) / len(records)
            clusters.append({
                "pattern": f"h{hour_bucket}-{hour_bucket+3}_{soc_bucket}_soc_{mtype}",
                "hour_bucket": hour_bucket,
                "soc_bucket": soc_bucket,
                "market_type": mtype,
                "avg_regret": avg_regret,
                "n_records": len(records),
                "interval_ids": [r.recommendation.interval_id for r in records],
            })

        return sorted(clusters, key=lambda c: c["avg_regret"] * c["n_records"], reverse=True)

    def recent_interval_history(self, n: int = 200) -> list[dict]:
        ingested = [
            r for r in self._records.values()
            if r.cleared_price is not None and r.ingested_at is not None
        ]
        ingested.sort(key=lambda r: r.ingested_at)
        recent = ingested[-n:]

        result = []
        for r in recent:
            rec = r.recommendation
            snap = r.market_state_snapshot
            result.append({
                "interval_id": rec.interval_id,
                "market_type": snap.get("market_type", "energy"),
                "hour_of_day": snap.get("hour_of_day", 0),
                "cleared_price": r.cleared_price,
                "direction": rec.direction,
                "volume_mw": rec.volume_mw,
                "limit_price": rec.limit_price,
                "rec_regret": r.rec_regret,
                "perfect_foresight_revenue": r.perfect_foresight_revenue,
                "soc_pct": snap.get("soc_pct", 0.5),
                "timestamp": r.ingested_at.isoformat() if r.ingested_at else None,
            })
        return result

    def summary(self) -> dict:
        completed = self.all_completed()
        ingested = [r for r in self._records.values() if r.cleared_price is not None]
        avg_regret = (
            sum(r.rec_regret for r in completed) / len(completed)
            if completed else None
        )
        return {
            "total_recommendations": len(self._records),
            "ingested": len(ingested),
            "completed_with_regret": len(completed),
            "pending": len(self._pending),
            "avg_rec_regret": avg_regret,
        }
