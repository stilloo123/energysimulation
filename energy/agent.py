from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException

from shared.a2a import get_interval, get_markets, register_agent
from shared.models import BatteryState, EnergyAgentCard, Recommendation, StatsResponse
from shared.thought_log import ThoughtLog
from energy.adaptation import AdaptationLoop, ToolSynthesisLoop
from energy.analysis.nightly import NightlyAnalysis
from energy.brain import DefaultEnergyBrain
from energy.ledger import LearningLedger
from energy.tool_registry import ToolRegistry


def load_config(path: str = "energy/config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if "ENERGY_PORT" in os.environ:
        cfg["port"] = int(os.environ["ENERGY_PORT"])
    if "ENERGY_NAME" in os.environ:
        cfg["name"] = os.environ["ENERGY_NAME"]
    if "ENERGY_SPECIALIZATION" in os.environ:
        cfg["specialization"] = os.environ["ENERGY_SPECIALIZATION"]
    if "ENERGY_MODEL" in os.environ:
        cfg["model"] = os.environ["ENERGY_MODEL"]
    return cfg


def _market_state_snapshot(interval, battery: BatteryState) -> dict:
    soc_pct = battery.soc_mwh / battery.capacity_mwh
    return {
        "soc_pct": round(soc_pct, 4),
        "hour_of_day": interval.scheduled_at.hour,
        "market_type": interval.market_type,
        "reference_price": interval.reference_price,
    }


def build_app(cfg: dict) -> FastAPI:
    port = cfg["port"]
    self_url = f"http://localhost:{port}"

    ledger = LearningLedger()
    tool_registry = ToolRegistry(ledger)
    brain = DefaultEnergyBrain(cfg, ledger, tool_registry)
    log = ThoughtLog(cfg["name"])
    adaptation_loop = AdaptationLoop(ledger, brain, log, cfg)
    tool_synthesis_loop = ToolSynthesisLoop(ledger, brain, tool_registry, log, cfg)

    known_markets: list[dict] = []
    _recent_query_times: list[datetime] = []

    def _count_recent_queries(window_seconds: int = 300) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
        return sum(1 for t in _recent_query_times if t.timestamp() > cutoff)

    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(60)
            await register_agent(cfg["registry_urls"], self_url, cfg["name"], "energy")

    async def _discovery_loop():
        nonlocal known_markets
        while True:
            try:
                known_markets = await get_markets(cfg["registry_urls"])
            except Exception as exc:
                log.write("DISCOVERY_ERROR", str(exc), {})
            await asyncio.sleep(60)

    async def _result_ingestion_loop():
        while True:
            await asyncio.sleep(cfg["result_ingestion_interval"])
            pending = ledger.get_pending_interval_ids()
            if not pending:
                continue
            for interval_id in pending:
                for market in known_markets:
                    try:
                        iv = await get_interval(market["url"], interval_id)
                        if iv.status == "dispatched" and iv.cleared_price is not None:
                            ledger.fill_result(interval_id, iv.cleared_price)
                            record = ledger.get_record_by_interval(interval_id)
                            regret = record.rec_regret if record else None
                            regret_str = f"{regret:.2f}" if regret is not None else "n/a"
                            log.write(
                                "REGRET_COMPUTED",
                                f"cleared=${iv.cleared_price:.2f} regret={regret_str} for {interval_id}",
                                {
                                    "interval_id": interval_id,
                                    "cleared_price": iv.cleared_price,
                                    "rec_regret": regret,
                                },
                            )
                            adaptation_loop.tick(1)
                            tool_synthesis_loop.tick(1)
                            break
                    except Exception:
                        pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Seed strategy_context.md from CSV before accepting any live intervals
        if cfg.get("csv_path"):
            try:
                nightly = NightlyAnalysis(cfg["csv_path"], brain)
                lessons = await asyncio.to_thread(nightly.run)
                log.write(
                    "NIGHTLY_ANALYSIS",
                    f"bootstrapped {len(lessons)} strategy rules from CSV",
                    {"csv_path": cfg["csv_path"], "n_rules": len(lessons)},
                )
            except Exception as exc:
                log.write("NIGHTLY_ANALYSIS_ERROR", str(exc), {})

        await register_agent(cfg["registry_urls"], self_url, cfg["name"], "energy")
        tasks = [
            asyncio.create_task(_heartbeat_loop()),
            asyncio.create_task(_discovery_loop()),
            asyncio.create_task(_result_ingestion_loop()),
        ]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title=cfg["name"], lifespan=lifespan)

    @app.get("/recommend", response_model=Recommendation)
    async def recommend(
        interval_id: str,
        battery_state: str,
        market_url: str,
    ) -> Recommendation:
        try:
            battery = BatteryState.model_validate_json(battery_state)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid battery_state: {exc}")

        try:
            interval = await get_interval(market_url, interval_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not fetch interval: {exc}")

        now = datetime.now(timezone.utc)
        time_left = (interval.bid_close_at - now).total_seconds()
        history = ledger.recent_interval_history(200)

        rec = await brain.recommend(interval, battery, history, time_left)

        # Record in ledger
        snapshot = _market_state_snapshot(interval, battery)
        ledger.record_recommendation(rec, snapshot)

        _recent_query_times.append(now)
        if len(_recent_query_times) > 1000:
            _recent_query_times.pop(0)

        log.write(
            "RECOMMENDATION_MADE",
            f"{rec.direction} {rec.volume_mw:.1f}MW @ ${rec.limit_price:.2f} "
            f"[conf={rec.confidence}] — {rec.reasoning}",
            {
                "recommendation_id": rec.recommendation_id,
                "interval_id": interval_id,
                "direction": rec.direction,
                "volume_mw": rec.volume_mw,
                "limit_price": rec.limit_price,
                "confidence": rec.confidence,
                "evidence_tool_calls": rec.evidence_tool_calls,
            },
        )
        return rec

    @app.get("/.well-known/agent.json")
    def agent_card():
        completed = ledger.all_completed()
        avg_regret = (
            sum(r.rec_regret for r in completed) / len(completed)
            if completed else None
        )
        return EnergyAgentCard(
            name=cfg["name"],
            type="energy",
            url=self_url,
            stats_url=f"{self_url}/stats",
            specialization=cfg["specialization"],
            intervals_advised=len(ledger._records),
            avg_rec_regret=avg_regret,
            confidence_accuracy=None,
            active_traders=_count_recent_queries(),
            learning_generation=adaptation_loop._generation,
        ).model_dump()

    @app.get("/stats", response_model=StatsResponse)
    def stats():
        return StatsResponse(
            name=cfg["name"],
            type="energy",
            url=self_url,
            profit=0.0,
            roi=0.0,
            total_volume=0.0,
            seed_balance=cfg["seed_balance"],
            active_intervals=len(ledger.get_pending_interval_ids()),
        )

    @app.get("/insights")
    def insights():
        return ledger.summary()

    @app.get("/thoughts")
    def thoughts():
        return log.recent(50)

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    cfg = load_config()
    app = build_app(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg["port"])
