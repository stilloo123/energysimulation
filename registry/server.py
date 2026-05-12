from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Energy Simulation Registry")

_agents: dict[str, dict] = {}

STALE_AFTER_SECONDS = 180

AgentTypeLiteral = Literal["market", "trader", "energy", "observer"]


class RegisterRequest(BaseModel):
    url: str
    name: str
    type: AgentTypeLiteral


@app.post("/register")
async def register(req: RegisterRequest):
    _agents[req.url] = {
        "url": req.url,
        "name": req.name,
        "type": req.type,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
    }
    return {"status": "registered"}


@app.get("/markets")
async def list_markets():
    _evict_stale()
    return [e for e in _agents.values() if e["type"] == "market"]


@app.get("/traders")
async def list_traders():
    _evict_stale()
    return [e for e in _agents.values() if e["type"] == "trader"]


@app.get("/energy")
async def list_energy():
    _evict_stale()
    return [e for e in _agents.values() if e["type"] == "energy"]


@app.get("/observers")
async def list_observers():
    _evict_stale()
    return [e for e in _agents.values() if e["type"] == "observer"]


@app.get("/agents")
async def list_all():
    _evict_stale()
    return list(_agents.values())


@app.get("/health")
async def health():
    return {"status": "ok", "agents": len(_agents)}


def _evict_stale() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS)
    stale = [
        url
        for url, e in _agents.items()
        if datetime.fromisoformat(e["last_heartbeat"]) < cutoff
    ]
    for url in stale:
        del _agents[url]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run("registry.server:app", host="0.0.0.0", port=port, reload=False)
