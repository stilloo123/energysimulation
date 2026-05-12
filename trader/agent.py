from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException

from shared.a2a import (
    get_agent_card,
    get_bid_result,
    get_energy_agents,
    get_markets,
    get_recommendation,
    register_agent,
    submit_bid,
)
from shared.models import DispatchBid, DispatchResult, EnergyAgentCard, StatsResponse
from shared.thought_log import ThoughtLog
from trader.battery import Battery
from trader.brain import DefaultTraderBrain
from trader.ledger import TraderLedger


def load_config(path: str = "trader/config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if "TRADER_PORT" in os.environ:
        cfg["port"] = int(os.environ["TRADER_PORT"])
    if "TRADER_NAME" in os.environ:
        cfg["name"] = os.environ["TRADER_NAME"]
    return cfg


def build_app(cfg: dict) -> FastAPI:
    port = cfg["port"]
    self_url = f"http://localhost:{port}"
    trader_id = str(uuid.uuid4())

    battery = Battery(cfg["battery"])
    ledger = TraderLedger(cfg["seed_balance"], battery)
    brain = DefaultTraderBrain(cfg)
    log = ThoughtLog("trader")

    interval_minutes: float = 5.0  # default; overridden from market card if available
    known_markets: list[dict] = []
    known_energy_agents: list[EnergyAgentCard] = []
    current_energy_agent_url: str | None = None

    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(60)
            await register_agent(cfg["registry_urls"], self_url, cfg["name"], "trader")

    async def _discovery_loop():
        nonlocal known_markets, known_energy_agents
        while True:
            try:
                known_markets = await get_markets(cfg["registry_urls"])
                known_energy_agents = await get_energy_agents(cfg["registry_urls"])
                # Prune already-bid set to currently open intervals
                open_ids: set[str] = set()
                for market in known_markets:
                    try:
                        card = await get_agent_card(market["url"])
                        for iv in card.get("open_intervals", []):
                            open_ids.add(iv["interval_id"])
                    except Exception:
                        pass
                ledger.prune_already_bid(open_ids)
            except Exception as exc:
                log.write("DISCOVERY_ERROR", str(exc), {})
            await asyncio.sleep(cfg["discovery_interval_seconds"])

    async def _bidding_loop():
        nonlocal current_energy_agent_url, interval_minutes
        while True:
            try:
                for market in known_markets:
                    market_url = market["url"]
                    try:
                        card = await get_agent_card(market_url)
                    except Exception:
                        continue

                    open_intervals = card.get("open_intervals", [])
                    for iv_data in open_intervals:
                        interval_id = iv_data["interval_id"]
                        if interval_id in ledger._already_bid:
                            continue

                        # Skip if bid window is closing — not enough time for recommendation
                        try:
                            bid_close = datetime.fromisoformat(iv_data["bid_close_at"])
                            if bid_close.tzinfo is None:
                                bid_close = bid_close.replace(tzinfo=timezone.utc)
                            time_left = (bid_close - datetime.now(timezone.utc)).total_seconds()
                            if time_left < cfg.get("min_recommendation_seconds", 20):
                                ledger._already_bid.add(interval_id)
                                continue
                        except Exception:
                            pass

                        # Select energy agent (log when we switch)
                        prev_agent_url = current_energy_agent_url
                        current_energy_agent_url = brain.select_energy_agent(
                            known_energy_agents,
                            ledger._energy_performance,
                            battery.state(),
                            current_energy_agent_url,
                        )
                        if prev_agent_url and current_energy_agent_url != prev_agent_url:
                            log.write(
                                "AGENT_SWITCHED",
                                f"switched energy advisor: {prev_agent_url} → {current_energy_agent_url}",
                                {"from": prev_agent_url, "to": current_energy_agent_url},
                            )

                        # Get recommendation
                        recommendation = None
                        if current_energy_agent_url:
                            try:
                                recommendation = await get_recommendation(
                                    current_energy_agent_url,
                                    interval_id,
                                    battery.state(),
                                    market_url,
                                )
                            except Exception as exc:
                                log.write(
                                    "RECOMMENDATION_ERROR",
                                    f"Failed to get recommendation: {exc}",
                                    {"agent_url": current_energy_agent_url},
                                )

                        # Decide bid — interval is 5 min = 5/60 hours
                        iv_minutes = interval_minutes
                        iv_hours = iv_minutes / 60.0
                        direction, volume, limit_price = brain.decide_bid(
                            recommendation, battery, iv_hours
                        )

                        if direction == "none":
                            ledger._already_bid.add(interval_id)
                            continue

                        soc_before = battery.soc_mwh
                        bid = DispatchBid(
                            bid_id=str(uuid.uuid4()),
                            interval_id=interval_id,
                            trader_id=trader_id,
                            trader_url=self_url,
                            direction=direction,
                            volume_mw=volume,
                            limit_price=limit_price,
                            submitted_at=datetime.now(timezone.utc),
                        )
                        try:
                            result = await submit_bid(market_url, bid)
                            bid_id = result.get("bid_id", bid.bid_id)
                            ledger.record_pending(
                                bid_id,
                                interval_id,
                                market_url,
                                current_energy_agent_url,
                                direction,
                                volume,
                                soc_before,
                                rec_confidence=getattr(recommendation, "confidence", None),
                            )
                            ledger._already_bid.add(interval_id)
                            log.write(
                                "BID_DECISION",
                                f"{direction} {volume:.2f}MW @ limit ${limit_price:.2f}",
                                {
                                    "bid_id": bid_id,
                                    "interval_id": interval_id,
                                    "soc_pct": round(battery.soc_pct * 100, 1),
                                    "energy_agent": current_energy_agent_url,
                                    "confidence": getattr(recommendation, "confidence", None),
                                },
                            )
                        except Exception as exc:
                            log.write("BID_SUBMIT_ERROR", str(exc), {"interval_id": interval_id})

            except Exception as exc:
                log.write("BIDDING_LOOP_ERROR", str(exc), {})

            await asyncio.sleep(cfg["bid_delay_seconds"])

    async def _settlement_loop():
        _IV_HOURS = 5.0 / 60.0
        _DEADLINE_SECONDS = 120
        while True:
            try:
                for bid_id, pending in list(ledger._pending.items()):
                    market_url = pending["market_url"]

                    # Deadline write-off: drop bids that are too old and still unresolved
                    submitted_at = pending.get("submitted_at")
                    if submitted_at:
                        age = (datetime.now(timezone.utc) - submitted_at).total_seconds()
                        if age > _DEADLINE_SECONDS:
                            try:
                                result_data = await get_bid_result(market_url, bid_id)
                                if result_data.get("status") == "pending":
                                    ledger._pending.pop(bid_id, None)
                                    continue
                            except Exception:
                                ledger._pending.pop(bid_id, None)
                                continue

                    try:
                        result_data = await get_bid_result(market_url, bid_id)
                    except Exception:
                        continue

                    if result_data.get("status") == "pending":
                        continue

                    try:
                        result = DispatchResult.model_validate(result_data)
                    except Exception:
                        continue

                    direction = pending["direction"]
                    volume_cleared = result.volume_cleared

                    if direction == "charge" and volume_cleared > 0:
                        battery.apply_charge(volume_cleared * _IV_HOURS)
                    elif direction == "discharge" and volume_cleared > 0:
                        battery.apply_discharge(volume_cleared * _IV_HOURS)

                    soc_after = battery.soc_mwh
                    ledger.record_settled(bid_id, result, soc_after)

                    # Update energy agent performance tracking
                    energy_agent_url = pending.get("energy_agent_url")
                    observed_regret = 0.0
                    if energy_agent_url:
                        # Observed regret: opportunity cost for discharge; 0 for charge
                        volume_mw = pending["volume_mw"]
                        if direction == "discharge":
                            perfect = volume_mw * _IV_HOURS * result.cleared_price
                            observed_regret = max(0.0, perfect - result.revenue)
                        else:
                            observed_regret = 0.0

                        # Confidence accuracy: high-confidence recs scored on whether bid executed
                        rec_confidence = pending.get("rec_confidence")
                        confidence_was_correct: bool | None = None
                        if rec_confidence == "high":
                            confidence_was_correct = volume_cleared > 0

                        ledger.update_agent_performance(
                            energy_agent_url, observed_regret, confidence_was_correct
                        )

                    regret_note = f" regret={observed_regret:.2f}" if energy_agent_url else ""
                    log.write(
                        "BID_RESULT",
                        f"settled {direction} revenue=${result.revenue:.2f}{regret_note}",
                        {
                            "bid_id": bid_id,
                            "revenue": result.revenue,
                            "volume_cleared": volume_cleared,
                            "soc_pct_after": round(battery.soc_pct * 100, 1),
                            "curtailed": result.curtailed,
                            "energy_agent": energy_agent_url,
                        },
                    )

            except Exception as exc:
                log.write("SETTLEMENT_LOOP_ERROR", str(exc), {})

            await asyncio.sleep(cfg["settlement_poll_seconds"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await register_agent(cfg["registry_urls"], self_url, cfg["name"], "trader")
        tasks = [
            asyncio.create_task(_heartbeat_loop()),
            asyncio.create_task(_discovery_loop()),
            asyncio.create_task(_bidding_loop()),
            asyncio.create_task(_settlement_loop()),
        ]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title=cfg["name"], lifespan=lifespan)

    @app.get("/.well-known/agent.json")
    def agent_card():
        return {
            "name": cfg["name"],
            "type": "trader",
            "url": self_url,
            "stats_url": f"{self_url}/stats",
            "battery_state": battery.state().model_dump(),
            "current_energy_agent": current_energy_agent_url,
            "profit": ledger.profit(),
            "roi": ledger.roi(),
        }

    @app.get("/stats", response_model=StatsResponse)
    def stats():
        return StatsResponse(
            name=cfg["name"],
            type="trader",
            url=self_url,
            profit=ledger.profit(),
            roi=ledger.roi(),
            total_volume=ledger.total_volume(),
            seed_balance=ledger.seed_balance,
            active_intervals=len(ledger._pending),
        )

    @app.get("/thoughts")
    def thoughts():
        return log.recent(50)

    @app.get("/battery")
    def battery_state():
        return battery.state().model_dump()

    @app.get("/performance")
    def performance():
        return {
            url: {
                "intervals_advised": rec.intervals_advised,
                "avg_rec_regret": rec.avg_rec_regret,
                "confidence_accuracy": rec.confidence_accuracy,
                "last_queried_at": rec.last_queried_at.isoformat(),
            }
            for url, rec in ledger._energy_performance.items()
        }

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    cfg = load_config()
    app = build_app(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg["port"])
