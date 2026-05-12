from __future__ import annotations

from pydantic import BaseModel


class Provenance(BaseModel):
    method: str
    n_rows: int
    version: str = "0.1.0"


class GapFacts(BaseModel):
    total_recommendations: int
    completed_recommendations: int
    avg_rec_regret: float
    total_perfect_revenue: float
    total_actual_revenue: float
    gap_absolute: float
    gap_pct: float
    provenance: Provenance


class SliceEntry(BaseModel):
    label: str
    n: int
    avg_regret: float
    avg_cleared_price: float
    dominant_direction: str


class SliceByHourOutput(BaseModel):
    slices: list[SliceEntry]
    provenance: Provenance


class SliceByPriceBucketOutput(BaseModel):
    slices: list[SliceEntry]
    provenance: Provenance


class SliceBySOCRegimeOutput(BaseModel):
    slices: list[SliceEntry]
    provenance: Provenance


class ForecastErrorEntry(BaseModel):
    interval_id: str
    hour: int
    direction: str
    limit_price: float
    cleared_price: float
    error: float  # limit_price - cleared_price


class ForecastErrorOutput(BaseModel):
    entries: list[ForecastErrorEntry]
    mean_absolute_error: float
    provenance: Provenance


class InspectEntry(BaseModel):
    interval_id: str
    hour: int
    market_type: str
    direction: str
    cleared_price: float
    limit_price: float
    volume_mw: float
    soc_pct: float
    rec_regret: float | None


class InspectOutput(BaseModel):
    entries: list[InspectEntry]
    provenance: Provenance
