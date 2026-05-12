from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from shared.a2a import get_agent_card, get_all_agents, get_stats, register_agent
from shared.models import EnergyAgentCard, StatsResponse
from observer.rankings import network_summary, rank_energy_agents, rank_markets, rank_traders


def load_config(path: str = "observer/config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if "OBSERVER_PORT" in os.environ:
        cfg["port"] = int(os.environ["OBSERVER_PORT"])
    return cfg


def build_app(cfg: dict) -> FastAPI:
    port = cfg["port"]
    self_url = f"http://localhost:{port}"

    # In-memory cache updated by poll_loop
    _cache: dict = {
        "stats": [],
        "energy_cards": [],
        "agent_cards": {},       # url → raw card dict
        "trader_observed": {},   # energy_agent_url → avg observed regret
        "market_intervals": [],  # upcoming intervals from all markets
        "thoughts": [],          # recent events from all agents
        "last_poll": None,
        "battery_state": {},     # trader battery state
    }

    async def _poll_loop():
        while True:
            await asyncio.sleep(cfg["poll_interval_seconds"])
            await _do_poll()

    async def _do_poll():
        try:
            agents = await get_all_agents(cfg["registry_urls"])
            stats_list: list[StatsResponse] = []
            energy_cards: list[EnergyAgentCard] = []
            agent_cards: dict = {}
            market_intervals: list[dict] = []
            thoughts: list[dict] = []

            for agent in agents:
                url = agent["url"]
                agent_type = agent.get("type", "")

                # Fetch stats
                try:
                    s = await get_stats(url)
                    stats_list.append(s)
                except Exception:
                    pass

                # Fetch agent card
                try:
                    card = await get_agent_card(url)
                    agent_cards[url] = card

                    if agent_type == "energy":
                        try:
                            ec = EnergyAgentCard.model_validate(card)
                            energy_cards.append(ec)
                        except Exception:
                            pass

                    if agent_type == "market":
                        for iv in card.get("upcoming_intervals", []):
                            iv["market_name"] = agent.get("name", url)
                            market_intervals.append(iv)
                except Exception:
                    pass

                # Fetch recent thoughts
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(f"{url}/thoughts")
                        if resp.status_code == 200:
                            recent = resp.json()[-5:]
                            for t in recent:
                                t["agent_name"] = agent.get("name", url)
                                t["agent_type"] = agent_type
                            thoughts.extend(recent)
                except Exception:
                    pass

                # Fetch battery state from traders
                if agent_type == "trader":
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            resp = await client.get(f"{url}/battery")
                            if resp.status_code == 200:
                                _cache["battery_state"] = resp.json()
                    except Exception:
                        pass

            # Compute trader-observed energy agent regret from trader performance endpoints
            trader_observed: dict[str, float] = {}
            for agent in agents:
                if agent.get("type") != "trader":
                    continue
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(f"{agent['url']}/performance")
                        if resp.status_code == 200:
                            perf = resp.json()
                            for ea_url, rec in perf.items():
                                regret = rec.get("avg_rec_regret", 0.0)
                                if ea_url not in trader_observed:
                                    trader_observed[ea_url] = regret
                                else:
                                    # average across traders
                                    trader_observed[ea_url] = (
                                        trader_observed[ea_url] + regret
                                    ) / 2
                except Exception:
                    pass

            # Sort thoughts by timestamp descending
            thoughts.sort(key=lambda t: t.get("ts", ""), reverse=True)

            _cache["stats"] = stats_list
            _cache["energy_cards"] = energy_cards
            _cache["agent_cards"] = agent_cards
            _cache["trader_observed"] = trader_observed
            _cache["market_intervals"] = sorted(
                market_intervals, key=lambda iv: iv.get("scheduled_at", "")
            )
            _cache["thoughts"] = thoughts[:50]
            _cache["last_poll"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await register_agent(cfg["registry_urls"], self_url, "Observer", "observer")
        # Run first poll immediately
        await _do_poll()
        task = asyncio.create_task(_poll_loop())
        yield
        task.cancel()

    app = FastAPI(title="Energy Simulation Observer", lifespan=lifespan)

    @app.get("/rankings/markets")
    def rankings_markets():
        return rank_markets(_cache["stats"])

    @app.get("/rankings/traders")
    def rankings_traders():
        return rank_traders(_cache["stats"])

    @app.get("/rankings/energy")
    def rankings_energy():
        return rank_energy_agents(_cache["energy_cards"], _cache["trader_observed"])

    @app.get("/network")
    def network():
        return network_summary(
            _cache["stats"], _cache["energy_cards"], _cache["trader_observed"]
        )

    @app.get("/schedule")
    def schedule():
        return _cache["market_intervals"]

    @app.get("/thoughts")
    def thoughts():
        return _cache["thoughts"]

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        stats: list[StatsResponse] = _cache["stats"]
        energy_cards: list[EnergyAgentCard] = _cache["energy_cards"]
        trader_observed: dict = _cache["trader_observed"]
        market_intervals: list = _cache["market_intervals"]
        all_thoughts: list = _cache["thoughts"]
        last_poll: str = _cache["last_poll"] or "never"
        battery: dict = _cache["battery_state"]

        net = network_summary(stats, energy_cards, trader_observed)
        markets_ranked = rank_markets(stats)
        traders_ranked = rank_traders(stats)
        energy_ranked = rank_energy_agents(energy_cards, trader_observed)

        now = datetime.now(timezone.utc)

        # Battery SOC
        soc_mwh = battery.get("soc_mwh", 0)
        capacity = battery.get("capacity_mwh", 100)
        soc_pct = round(soc_mwh / capacity * 100) if capacity else 0
        soc_bar_color = "#4ade80" if soc_pct > 60 else "#fbbf24" if soc_pct > 30 else "#f87171"
        soc_display = f"{soc_pct}% ({soc_mwh:.1f}/{capacity:.0f} MWh)"

        # Narrative: what is happening right now
        recent_events = [t.get("event", "") for t in all_thoughts[:10]]
        recent_reasoning = [t.get("reasoning", "") for t in all_thoughts[:5]]
        if "BID_DECISION" in recent_events:
            last_bid = next((t for t in all_thoughts if t.get("event") == "BID_DECISION"), None)
            if last_bid:
                narrative = f"Trader is actively bidding — last action: {last_bid.get('reasoning', '')}."
            else:
                narrative = "Trader is placing bids."
        elif "BID_RESULT" in recent_events:
            narrative = "Waiting for next bid window. Recent bids have settled."
        elif net["total_markets"] == 0:
            narrative = "No markets online yet. Start the market agent."
        elif net["total_energy_agents"] == 0:
            narrative = "No energy agents online. Start the energy agent."
        else:
            narrative = "Simulation running. Waiting for open bid intervals."

        # Plain-English event labels
        def _event_label(event: str, reasoning: str) -> str:
            labels = {
                "BID_DECISION": "🟢 Bid placed",
                "BID_RESULT": "💰 Bid settled",
                "INTERVAL_DISPATCHED": "📊 Price revealed",
                "INTERVAL_CREATED": "📅 Interval scheduled",
                "RECOMMENDATION_MADE": "🤖 Recommendation",
                "AGENT_SWITCHED": "🔄 Advisor switched",
                "DISCOVERY_ERROR": "⚠️ Discovery error",
                "NIGHTLY_ANALYSIS": "📚 Strategy updated",
            }
            return labels.get(event, event)

        def _countdown(iv: dict) -> str:
            try:
                t = datetime.fromisoformat(iv.get("dispatch_at", ""))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                secs = int((t - now).total_seconds())
                return "dispatching..." if secs < 0 else f"{secs}s"
            except Exception:
                return "?"

        def _regret_str(v):
            return f"{v:.2f}" if v is not None else "—"

        def _flag_str(flagged):
            return " ⚠️" if flagged else ""

        market_rows = "".join(
            f"<tr><td>{s.name}</td><td>{s.url}</td>"
            f"<td>{s.total_volume:.1f}</td>"
            f"<td>${s.profit:,.2f}</td>"
            f"<td>{s.active_intervals}</td></tr>"
            for s in markets_ranked
        ) or "<tr><td colspan='5'>No markets online</td></tr>"

        trader_rows = "".join(
            f"<tr><td>{s.name}</td>"
            f"<td class=\"{'pos' if s.profit >= 0 else 'neg'}\">${s.profit:,.2f}</td>"
            f"<td>{s.roi * 100:.2f}%</td>"
            f"<td>{s.total_volume:.1f}</td>"
            f"<td>{s.active_intervals}</td></tr>"
            for s in traders_ranked
        ) or "<tr><td colspan='5'>No traders online</td></tr>"

        energy_rows = "".join(
            f"<tr><td>{r['name']}{_flag_str(r['flagged'])}</td>"
            f"<td>{r['specialization']}</td>"
            f"<td>{r['intervals_advised']}</td>"
            f"<td>{_regret_str(r['self_reported_regret'])}</td>"
            f"<td>{_regret_str(r['trader_observed_regret'])}</td>"
            f"<td>{r['delta_pct'] if r['delta_pct'] is not None else '—'}%</td>"
            f"<td>{r['learning_generation']}</td>"
            f"<td>{r['active_traders']}</td></tr>"
            for r in energy_ranked
        ) or "<tr><td colspan='8'>No energy agents online</td></tr>"

        schedule_rows = "".join(
            f"<tr><td>{iv.get('market_name', '?')}</td>"
            f"<td>{iv.get('market_type', '?')}</td>"
            f"<td>{iv.get('status', '?')}</td>"
            f"<td>${iv.get('reference_price') or '?'}</td>"
            f"<td>{_countdown(iv)}</td></tr>"
            for iv in market_intervals[:12]
        ) or "<tr><td colspan='5'>No scheduled intervals</td></tr>"

        thought_rows = "".join(
            f"<tr><td>{t.get('ts', '')[:19].replace('T', ' ')}</td>"
            f"<td>{t.get('agent_name', '?')}</td>"
            f"<td>{_event_label(t.get('event', ''), t.get('reasoning', ''))}</td>"
            f"<td>{str(t.get('reasoning', ''))[:120]}</td></tr>"
            for t in all_thoughts[:30]
        ) or "<tr><td colspan='4'>No events yet</td></tr>"

        pnl_color = "green" if net["total_pnl"] >= 0 else "red"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>Energy Simulation Observer</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 24px; }}
  h1 {{ font-size: 1.5rem; font-weight: 700; color: #38bdf8; margin-bottom: 4px; }}
  .subtitle {{ font-size: 0.8rem; color: #64748b; margin-bottom: 20px; }}
  .narrative {{ background: #1e293b; border-left: 3px solid #38bdf8; padding: 12px 16px; border-radius: 0 8px 8px 0; margin-bottom: 24px; font-size: 0.9rem; color: #94a3b8; }}
  .kpis {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }}
  .kpi {{ background: #1e293b; border-radius: 10px; padding: 16px 22px; min-width: 150px; }}
  .kpi .label {{ font-size: 0.7rem; text-transform: uppercase; color: #64748b; letter-spacing: 0.08em; }}
  .kpi .value {{ font-size: 1.5rem; font-weight: 700; color: #f1f5f9; margin-top: 4px; }}
  .kpi .value.green {{ color: #4ade80; }}
  .kpi .value.red {{ color: #f87171; }}
  .kpi .value.blue {{ color: #38bdf8; }}
  .soc-bar-wrap {{ margin-top: 6px; background: #0f172a; border-radius: 4px; height: 6px; width: 100%; }}
  .soc-bar {{ height: 6px; border-radius: 4px; background: {soc_bar_color}; width: {soc_pct}%; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 0.85rem; font-weight: 600; color: #94a3b8; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.06em; }}
  table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; font-size: 0.85rem; }}
  th {{ background: #0f172a; color: #64748b; text-align: left; padding: 10px 14px; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; }}
  td {{ padding: 9px 14px; border-top: 1px solid #0f172a; color: #cbd5e1; }}
  td.pos {{ color: #4ade80; }}
  td.neg {{ color: #f87171; }}
  tr:hover td {{ background: #243347; }}
</style>
</head>
<body>
<h1>⚡ Energy Simulation Observer</h1>
<p class="subtitle">Last updated: {last_poll[:19].replace("T", " ")} UTC &nbsp;·&nbsp; Auto-refreshes every 30s</p>

<div class="narrative">💬 {narrative}</div>

<div class="kpis">
  <div class="kpi">
    <div class="label">Battery SOC</div>
    <div class="value blue" style="font-size:1.1rem">{soc_display}</div>
    <div class="soc-bar-wrap"><div class="soc-bar"></div></div>
  </div>
  <div class="kpi"><div class="label">Net P&amp;L</div><div class="value {pnl_color}">${net['total_pnl']:,.0f}</div></div>
  <div class="kpi"><div class="label">Volume Traded</div><div class="value">{net['total_volume_mwh']:.1f} MWh</div></div>
  <div class="kpi"><div class="label">Intervals Advised</div><div class="value blue">{energy_ranked[0]['intervals_advised'] if energy_ranked else 0}</div></div>
  <div class="kpi"><div class="label">Advisor Regret</div><div class="value">{_regret_str(energy_ranked[0]['self_reported_regret'] if energy_ranked else None)}</div></div>
  <div class="kpi"><div class="label">Markets</div><div class="value blue">{net['total_markets']}</div></div>
  <div class="kpi"><div class="label">Traders</div><div class="value blue">{net['total_traders']}</div></div>
  <div class="kpi"><div class="label">Energy Agents</div><div class="value blue">{net['total_energy_agents']}</div></div>
</div>

<section>
  <h2>Live Feed</h2>
  <table>
    <thead><tr><th>Time (UTC)</th><th>Agent</th><th>Event</th><th>Detail</th></tr></thead>
    <tbody>{thought_rows}</tbody>
  </table>
</section>

<section>
  <h2>Trader Performance</h2>
  <table>
    <thead><tr><th>Name</th><th>P&amp;L</th><th>ROI</th><th>Volume (MWh)</th><th>Pending Bids</th></tr></thead>
    <tbody>{trader_rows}</tbody>
  </table>
</section>

<section>
  <h2>Energy Advisors (lower regret = better)</h2>
  <table>
    <thead>
      <tr>
        <th>Name</th><th>Specialization</th><th>Intervals Advised</th>
        <th>Self-Reported Regret</th><th>Trader-Observed Regret</th>
        <th>Delta</th><th>Learning Gen</th><th>Active Traders</th>
      </tr>
    </thead>
    <tbody>{energy_rows}</tbody>
  </table>
</section>

<section>
  <h2>Market</h2>
  <table>
    <thead><tr><th>Name</th><th>URL</th><th>Volume Cleared (MWh)</th><th>Revenue</th><th>Active Intervals</th></tr></thead>
    <tbody>{market_rows}</tbody>
  </table>
</section>

<section>
  <h2>Upcoming Dispatch</h2>
  <table>
    <thead><tr><th>Market</th><th>Type</th><th>Status</th><th>Reference Price</th><th>Dispatch In</th></tr></thead>
    <tbody>{schedule_rows}</tbody>
  </table>
</section>

</body>
</html>"""
        return HTMLResponse(content=html)

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    cfg = load_config()
    app = build_app(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg["port"])
