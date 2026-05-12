from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx

from shared.models import (
    AgentCard,
    BatteryState,
    DispatchBid,
    DispatchResult,
    EnergyAgentCard,
    MarketInterval,
    Recommendation,
    StatsResponse,
)

_TIMEOUT = 10.0


async def register_agent(
    registry_urls: list[str], url: str, name: str, agent_type: str
) -> None:
    payload = {"url": url, "name": name, "type": agent_type}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [
            _post_ignore(client, f"{reg}/register", payload)
            for reg in registry_urls
        ]
        await asyncio.gather(*tasks, return_exceptions=True)


async def get_markets(registry_urls: list[str]) -> list[dict]:
    return await _gather_agents(registry_urls, "markets")


async def get_traders(registry_urls: list[str]) -> list[dict]:
    return await _gather_agents(registry_urls, "traders")


async def get_energy_agents(registry_urls: list[str]) -> list[EnergyAgentCard]:
    raw = await _gather_agents(registry_urls, "energy")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            *[_get_json(client, f"{e['url']}/.well-known/agent.json") for e in raw if e.get("url")],
            return_exceptions=True,
        )
    cards = []
    for result in results:
        if isinstance(result, Exception):
            continue
        try:
            cards.append(EnergyAgentCard.model_validate(result))
        except Exception:
            pass
    return cards


async def get_all_agents(registry_urls: list[str]) -> list[dict]:
    return await _gather_agents(registry_urls, "agents")


async def get_agent_card(url: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{url}/.well-known/agent.json")
        resp.raise_for_status()
        return resp.json()


async def get_stats(url: str) -> StatsResponse:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{url}/stats")
        resp.raise_for_status()
        return StatsResponse.model_validate(resp.json())


async def get_interval(market_url: str, interval_id: str) -> MarketInterval:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{market_url}/intervals/{interval_id}")
        resp.raise_for_status()
        return MarketInterval.model_validate(resp.json())


async def get_bid_result(market_url: str, bid_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{market_url}/bids/{bid_id}")
        resp.raise_for_status()
        return resp.json()


async def submit_bid(market_url: str, bid: DispatchBid) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{market_url}/bids",
            content=bid.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_recommendation(
    energy_url: str,
    interval_id: str,
    battery_state: BatteryState,
    market_url: str,
) -> Recommendation:
    params = {
        "interval_id": interval_id,
        "battery_state": battery_state.model_dump_json(),
        "market_url": market_url,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(f"{energy_url}/recommend", params=params)
        resp.raise_for_status()
        return Recommendation.model_validate(resp.json())


async def _gather_agents(registry_urls: list[str], path: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            *[_get_json(client, f"{reg}/{path}") for reg in registry_urls],
            return_exceptions=True,
        )
    seen_urls: set[str] = set()
    agents: list[dict] = []
    for result in results:
        if isinstance(result, Exception):
            continue
        for entry in result:
            url = entry.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                agents.append(entry)
    return agents


async def _get_json(client: httpx.AsyncClient, url: str) -> list[dict]:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def _post_ignore(client: httpx.AsyncClient, url: str, payload: dict) -> None:
    try:
        await client.post(url, json=payload)
    except Exception:
        pass
