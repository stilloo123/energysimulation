from __future__ import annotations

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from shared.a2a import register_agent
from shared.models import DispatchBid, MarketInterval, StatsResponse
from shared.thought_log import ThoughtLog
from market.brain import CSVMarketBrain, DefaultMarketBrain
from market.ledger import MarketLedger
from market.scheduler import IntervalScheduler


def load_config(path: str = "market/config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Allow env overrides
    if "TIME_MULTIPLIER" in os.environ:
        cfg["time_multiplier"] = float(os.environ["TIME_MULTIPLIER"])
    if "MARKET_PORT" in os.environ:
        cfg["port"] = int(os.environ["MARKET_PORT"])
    if "MARKET_NAME" in os.environ:
        cfg["name"] = os.environ["MARKET_NAME"]
    if "CSV_PATH" in os.environ:
        cfg["csv_path"] = os.environ["CSV_PATH"]
    return cfg


def build_app(cfg: dict) -> FastAPI:
    port = cfg["port"]
    self_url = f"http://localhost:{port}"

    scheduler = IntervalScheduler(cfg)
    ledger = MarketLedger(cfg["grid_capacity_mw"])
    if cfg.get("csv_path"):
        brain = CSVMarketBrain(cfg["csv_path"])
    else:
        brain = DefaultMarketBrain(cfg["agents_md"], cfg["model"])
    log = ThoughtLog("market")

    interval_minutes = cfg["interval_minutes"]
    multiplier = cfg["time_multiplier"]
    real_seconds = (interval_minutes * 60) / multiplier
    lookahead = cfg["lookahead_intervals"]
    market_types: list[str] = cfg["market_types"]

    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(60)
            await register_agent(cfg["registry_urls"], self_url, cfg["name"], "market")

    async def _price_generation_loop():
        while True:
            upcoming = scheduler.get_upcoming_intervals(lookahead)
            need = lookahead - len(upcoming)
            if need > 0:
                from datetime import timedelta
                # Compute all scheduled times once so slots and intervals use identical values
                next_at = scheduler.next_scheduled_at()
                scheduled_times = [
                    next_at + timedelta(seconds=real_seconds * i)
                    for i in range(need)
                ]

                slots = []
                for i, scheduled in enumerate(scheduled_times):
                    for mtype in market_types:
                        slots.append({
                            "market_type": mtype,
                            "scheduled_at": scheduled.isoformat(),
                            "hour": scheduled.hour,
                            "day_of_week": scheduled.weekday(),
                        })

                recent = [
                    {"market_type": iv.market_type, "cleared_price": iv.cleared_price}
                    for iv in scheduler.get_dispatched_intervals(
                        datetime.now(timezone.utc) - timedelta(hours=2)
                    )
                    if iv.cleared_price is not None
                ][-20:]

                try:
                    prices = brain.generate_prices(slots, recent)
                except Exception as exc:
                    log.write("PRICE_GENERATION_ERROR", str(exc), {})
                    prices = [70.0] * len(slots)

                for i, scheduled in enumerate(scheduled_times):
                    for j, mtype in enumerate(market_types):
                        idx = i * len(market_types) + j
                        price = prices[idx] if idx < len(prices) else 70.0
                        iv = scheduler.create_interval(mtype, scheduled, price)
                        log.write(
                            "INTERVAL_CREATED",
                            f"created {mtype} interval for {scheduled.isoformat()}",
                            {"interval_id": iv.interval_id, "reference_price": price},
                        )

            await asyncio.sleep(real_seconds * 0.5)

    async def _scheduler_loop():
        while True:
            now = datetime.now(timezone.utc)
            for iv in scheduler.all_intervals():
                if iv.status == "scheduled" and now >= iv.bid_open_at:
                    scheduler.open_interval(iv.interval_id)

                elif iv.status == "open" and now >= iv.bid_close_at:
                    scheduler.close_interval(iv.interval_id)

                elif iv.status == "closed" and now >= iv.dispatch_at:
                    # Use actual cleared price from CSV if available, else reference price
                    if hasattr(brain, "cleared_price_for"):
                        actual = brain.cleared_price_for(iv.scheduled_at.isoformat())
                        cleared_price = actual if actual is not None else (iv.reference_price or 70.0)
                    else:
                        cleared_price = iv.reference_price or 70.0
                    scheduler.dispatch_interval(iv.interval_id, cleared_price)
                    results = ledger.clear_interval(iv.interval_id, cleared_price)
                    log.write(
                        "INTERVAL_DISPATCHED",
                        f"dispatched {iv.market_type} at ${cleared_price:.2f}/MWh",
                        {
                            "interval_id": iv.interval_id,
                            "cleared_price": cleared_price,
                            "bids_cleared": len(results),
                        },
                    )

            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await register_agent(cfg["registry_urls"], self_url, cfg["name"], "market")
        tasks = [
            asyncio.create_task(_heartbeat_loop()),
            asyncio.create_task(_price_generation_loop()),
            asyncio.create_task(_scheduler_loop()),
        ]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title=cfg["name"], lifespan=lifespan)

    @app.get("/.well-known/agent.json")
    def agent_card():
        open_ivs = scheduler.get_open_intervals()
        upcoming = scheduler.get_upcoming_intervals(lookahead)
        return {
            "name": cfg["name"],
            "type": "market",
            "url": self_url,
            "stats_url": f"{self_url}/stats",
            "open_intervals": [iv.model_dump(mode="json") for iv in open_ivs],
            "upcoming_intervals": [iv.model_dump(mode="json") for iv in upcoming],
            **ledger.stats(),
        }

    @app.get("/intervals/{interval_id}", response_model=MarketInterval)
    def get_interval(interval_id: str):
        iv = scheduler.get_interval(interval_id)
        if iv is None:
            raise HTTPException(status_code=404, detail="Interval not found")
        return iv

    @app.get("/intervals")
    def list_intervals(status: str | None = None, market_type: str | None = None):
        ivs = scheduler.all_intervals()
        if status:
            ivs = [iv for iv in ivs if iv.status == status]
        if market_type:
            ivs = [iv for iv in ivs if iv.market_type == market_type]
        return [iv.model_dump(mode="json") for iv in ivs]

    @app.post("/bids")
    def submit_bid(bid: DispatchBid):
        iv = scheduler.get_interval(bid.interval_id)
        if iv is None:
            raise HTTPException(status_code=404, detail="Interval not found")
        if iv.status != "open":
            raise HTTPException(
                status_code=400, detail=f"Interval not open (status={iv.status})"
            )
        if bid.direction not in ("charge", "discharge"):
            raise HTTPException(status_code=400, detail="Invalid direction")
        if bid.volume_mw <= 0:
            raise HTTPException(status_code=400, detail="volume_mw must be > 0")
        bid_id = ledger.accept_bid(bid)
        return {"bid_id": bid_id, "interval_id": bid.interval_id, "status": "accepted"}

    @app.get("/bids/{bid_id}")
    def get_bid_result(bid_id: str):
        record = ledger.get_bid(bid_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Bid not found")
        if record.result is None:
            return {"status": "pending", "bid_id": bid_id}
        return record.result.model_dump(mode="json")

    @app.get("/stats", response_model=StatsResponse)
    def stats():
        s = ledger.stats()
        return StatsResponse(
            name=cfg["name"],
            type="market",
            url=self_url,
            profit=s["cleared_revenue"],
            roi=0.0,
            total_volume=s["cleared_volume_mwh"],
            seed_balance=cfg["seed_balance"],
            active_intervals=len(scheduler.get_open_intervals()),
        )

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
