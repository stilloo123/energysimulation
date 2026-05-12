from __future__ import annotations

from collections import defaultdict

from energy.analysis.schemas import (
    Provenance,
    SliceByHourOutput,
    SliceByPriceBucketOutput,
    SliceBySOCRegimeOutput,
    SliceEntry,
)


def slice_by_hour(interval_history: list[dict]) -> SliceByHourOutput:
    buckets: dict[int, list[dict]] = defaultdict(list)
    for r in interval_history:
        hour = r.get("hour_of_day")
        if hour is not None:
            buckets[int(hour)].append(r)

    slices = []
    for hour in sorted(buckets):
        rows = buckets[hour]
        avg_regret = _avg_regret(rows)
        avg_price = _avg(rows, "cleared_price")
        dominant = _dominant_direction(rows)
        slices.append(SliceEntry(
            label=f"hour_{hour:02d}",
            n=len(rows),
            avg_regret=avg_regret,
            avg_cleared_price=avg_price,
            dominant_direction=dominant,
        ))

    return SliceByHourOutput(
        slices=slices,
        provenance=Provenance(method="slice_by_hour", n_rows=len(interval_history)),
    )


def slice_by_price_bucket(interval_history: list[dict]) -> SliceByPriceBucketOutput:
    prices = [r.get("cleared_price", 0.0) for r in interval_history if r.get("cleared_price") is not None]
    if not prices:
        return SliceByPriceBucketOutput(
            slices=[],
            provenance=Provenance(method="slice_by_price_bucket", n_rows=0),
        )

    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    q1 = prices_sorted[n // 4]
    q2 = prices_sorted[n // 2]
    q3 = prices_sorted[3 * n // 4]

    def bucket_label(p: float) -> str:
        if p <= q1:
            return "Q1_low"
        elif p <= q2:
            return "Q2_mid_low"
        elif p <= q3:
            return "Q3_mid_high"
        else:
            return "Q4_high"

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in interval_history:
        p = r.get("cleared_price")
        if p is not None:
            buckets[bucket_label(p)].append(r)

    slices = []
    for label in ["Q1_low", "Q2_mid_low", "Q3_mid_high", "Q4_high"]:
        rows = buckets.get(label, [])
        slices.append(SliceEntry(
            label=label,
            n=len(rows),
            avg_regret=_avg_regret(rows),
            avg_cleared_price=_avg(rows, "cleared_price"),
            dominant_direction=_dominant_direction(rows),
        ))

    return SliceByPriceBucketOutput(
        slices=slices,
        provenance=Provenance(method="slice_by_price_bucket", n_rows=len(interval_history)),
    )


def slice_by_soc_regime(interval_history: list[dict]) -> SliceBySOCRegimeOutput:
    def regime(soc: float) -> str:
        if soc < 0.30:
            return "low_soc"
        elif soc < 0.70:
            return "mid_soc"
        else:
            return "high_soc"

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in interval_history:
        soc = r.get("soc_pct")
        if soc is not None:
            buckets[regime(float(soc))].append(r)

    slices = []
    for label in ["low_soc", "mid_soc", "high_soc"]:
        rows = buckets.get(label, [])
        slices.append(SliceEntry(
            label=label,
            n=len(rows),
            avg_regret=_avg_regret(rows),
            avg_cleared_price=_avg(rows, "cleared_price"),
            dominant_direction=_dominant_direction(rows),
        ))

    return SliceBySOCRegimeOutput(
        slices=slices,
        provenance=Provenance(method="slice_by_soc_regime", n_rows=len(interval_history)),
    )


def _avg_regret(rows: list[dict]) -> float:
    vals = [r["rec_regret"] for r in rows if r.get("rec_regret") is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _avg(rows: list[dict], key: str) -> float:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _dominant_direction(rows: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        d = r.get("direction", "none")
        counts[d] += 1
    return max(counts, key=counts.__getitem__) if counts else "none"
