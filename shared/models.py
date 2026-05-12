from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class BatteryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    soc_mwh: float
    capacity_mwh: float
    max_charge_mw: float
    max_discharge_mw: float
    efficiency: float


class MarketInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_id: str
    market_type: Literal["energy", "fcas_raise", "fcas_lower"]
    scheduled_at: datetime
    bid_open_at: datetime
    bid_close_at: datetime
    dispatch_at: datetime
    status: Literal["scheduled", "open", "closed", "dispatched"]
    cleared_price: float | None = None
    reference_price: float | None = None


class DispatchBid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bid_id: str
    interval_id: str
    trader_id: str
    trader_url: str
    direction: Literal["charge", "discharge", "none"]
    volume_mw: float
    limit_price: float
    submitted_at: datetime


class DispatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bid_id: str
    interval_id: str
    cleared_price: float
    volume_cleared: float
    revenue: float
    dispatched_at: datetime
    curtailed: bool


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    interval_id: str
    direction: Literal["charge", "discharge", "none"]
    volume_mw: float
    limit_price: float
    confidence: Literal["high", "low"]
    reasoning: str
    evidence_tool_calls: list[str]
    similar_past_interval_ids: list[str]
    generated_at: datetime


class RecommendationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation: Recommendation
    market_state_snapshot: dict
    cleared_price: float | None = None
    perfect_foresight_revenue: float | None = None
    rec_regret: float | None = None
    ingested_at: datetime | None = None


class AgentCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["market", "trader", "energy", "observer"]
    url: str
    stats_url: str


class EnergyAgentCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["market", "trader", "energy", "observer"]
    url: str
    stats_url: str
    specialization: str
    intervals_advised: int
    avg_rec_regret: float | None = None
    confidence_accuracy: float | None = None
    active_traders: int
    learning_generation: int


class StatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    url: str
    profit: float
    roi: float
    total_volume: float
    seed_balance: float
    active_intervals: int


class PerformanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_url: str
    intervals_advised: int
    avg_rec_regret: float
    confidence_accuracy: float
    last_queried_at: datetime
