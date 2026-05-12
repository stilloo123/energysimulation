# Energy Simulation — Dev Notes

## Project structure

```
shared/     protocol layer — models, HTTP helpers, LLM router, thought log (no side effects)
registry/   stateless agent directory — FastAPI, no market data
market/     market agent — interval scheduler, bid clearing, price generation brain
trader/     trader agent — battery SOC, bidding loop, energy agent selection
energy/     energy agent — real-time advisor, recommendation pipeline, learning loop
observer/   observer agent — polling aggregator, HTML dashboard, rankings
```

## Running locally

```bash
pip install -r requirements.txt
# set ANTHROPIC_API_KEY

python -m registry.server          # port 8000
python -m market.agent             # port 8001
python -m energy.agent             # port 8003  (start before trader)
python -m trader.agent             # port 8002
python -m observer.agent           # port 8080
```

Dashboard: http://localhost:8080

## Running multiple Energy agents (competition mode)

```bash
# Each person hosts their own Energy agent instance
ENERGY_PORT=8003 ENERGY_NAME="Advisor Alpha" python -m energy.agent
ENERGY_PORT=8004 ENERGY_NAME="FCAS Specialist" python -m energy.agent
# Both register with the shared registry — Observer leaderboard shows both
```

## Key invariants

- `shared/` has no imports from market/trader/energy/observer — it is the foundation
- Battery SOC hard constraints live in `trader/battery.py` — never bypass them
- Hard gate failure in Energy agent always returns `direction="none"` — no ungrounded bids
- `result_ingestion_loop` in Energy agent pulls cleared prices from Market directly — no Trader feedback needed
- `Brain` is the only class users should subclass — everything else is plumbing
- No cryptographic primitives — this is a cooperative simulation, no trust enforcement needed
- Simulation speed is controlled by `time_multiplier` in market/config.yaml (1.0 = real-time 5-min intervals)
- Observer cross-validates Energy agent self-reported metrics against actual trader outcomes

## Adding a new agent type

1. Create directory with `agent.py`, `brain.py`, `ledger.py`, `config.yaml`, `AGENTS.md`
2. Use `build_app(cfg)` pattern with FastAPI lifespan for background loops
3. Expose `GET /.well-known/agent.json` and `GET /stats` for Observer compatibility
4. Expose `GET /thoughts` backed by `shared/thought_log.py`
5. Register with registry at startup (type must be one of: market, trader, energy, observer)
6. Heartbeat every 60s — registry evicts after 180s without heartbeat

## Adding a competing Energy agent strategy

1. Subclass `energy.brain.Brain`, override `recommend()` and `distill_lessons()`
2. Set a unique `name` and `specialization` in your `config.yaml`
3. Start with a different port: `ENERGY_PORT=8004 python -m energy.agent`
4. Observer `/rankings/energy` will show your agent vs others

## Phase build order

See `implementation.md` for full details. Short version:
1. shared + registry
2. market
3. trader (rule-based brain — always follows Energy agent)
4. energy (basic recommendation + quality gates)
5. observer (dashboard + rankings)
6. energy learning (LearningLedger, AdaptationLoop, retrieval)
7. multi-energy competition (Trader selects best advisor, Observer cross-validates)
8. ToolSynthesisLoop (Energy agent generates its own tools)
