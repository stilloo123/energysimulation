# Energy Simulation — Implementation Plan

## Prerequisites

```bash
Python 3.12+
ANTHROPIC_API_KEY (for Energy agent and Market brain)
pip install -r requirements.txt
```

Dependencies:
```
fastapi>=0.111
uvicorn[standard]>=0.30
httpx>=0.27
pydantic>=2.8
pyyaml>=6.0
strands-agents>=1.0          # Energy agent investigation pipeline
anthropic>=0.30              # Anthropic SDK (Market brain, Energy brain)
openai>=1.40                 # OpenAI SDK (judge model, local model support)
pandas>=2.2                  # Energy analysis tools
numpy>=1.26                  # Slice computations
python-dotenv>=1.0
rich>=13.0                   # Terminal output formatting
```

---

## Phase Overview

| Phase | What gets built | End state |
|---|---|---|
| 1 | Foundation: shared layer + registry | Registry runs, shared models importable |
| 2 | Market agent | Generates intervals, accepts bids, dispatches results |
| 3 | Trader agent (rule-based brain) | Queries Energy agent, places bids, tracks SOC |
| 4 | Energy agent (basic recommendation) | Returns grounded recommendations, quality-gated |
| 5 | Observer | HTML dashboard, all rankings visible |
| 6 | Energy agent learning | LearningLedger, AdaptationLoop, retrieval |
| 7 | Multi-Energy-agent competition | Trader selects best advisor, Observer cross-validates |
| 8 | ToolSynthesisLoop | Energy agent generates its own analysis tools |

Each phase is independently runnable and testable before moving to the next.

---

## Phase 1 — Foundation

**Goal**: Every agent imports from `shared/` without issues. Registry accepts registrations and serves agent lists.

### Files to create

#### `requirements.txt`
Full dependency list as above.

#### `shared/__init__.py`
Empty.

#### `shared/models.py`
All Pydantic data models. Single source of truth for every inter-agent contract.

Key models (in order of dependency):
1. `BatteryState` — soc_mwh, capacity_mwh, max_charge_mw, max_discharge_mw, efficiency
2. `MarketInterval` — interval_id, market_type, scheduled_at, bid_open_at, bid_close_at, dispatch_at, status, cleared_price, reference_price
3. `DispatchBid` — bid_id, interval_id, trader_id, trader_url, direction, volume_mw, limit_price, submitted_at
4. `DispatchResult` — bid_id, interval_id, cleared_price, volume_cleared, revenue, dispatched_at, curtailed
5. `Recommendation` — recommendation_id, interval_id, direction, volume_mw, limit_price, confidence, reasoning, evidence_tool_calls, similar_past_interval_ids, generated_at
6. `RecommendationRecord` — recommendation, market_state_snapshot, cleared_price, perfect_foresight_revenue, rec_regret, ingested_at
7. `AgentCard` — name, type, url, stats_url
8. `EnergyAgentCard(AgentCard)` — specialization, intervals_advised, avg_rec_regret, confidence_accuracy, active_traders, learning_generation
9. `StatsResponse` — name, type, url, profit, roi, total_volume, seed_balance, active_intervals
10. `PerformanceRecord` — agent_url, intervals_advised, avg_rec_regret, confidence_accuracy, last_queried_at

All models use `model_config = ConfigDict(extra="forbid")`.

#### `shared/a2a.py`
Async HTTP helpers using `httpx.AsyncClient` with 10s timeout.

Functions:
- `async register_agent(registry_urls, url, name, agent_type)` — POST /register to all registries, fire-and-forget exceptions
- `async get_markets(registry_urls)` — GET /markets from all registries, dedup by URL
- `async get_traders(registry_urls)` — GET /traders
- `async get_energy_agents(registry_urls)` — GET /energy
- `async get_all_agents(registry_urls)` — GET /agents
- `async get_agent_card(url)` — GET /.well-known/agent.json
- `async get_stats(url)` — GET /stats
- `async get_interval(market_url, interval_id)` — GET /intervals/{id}
- `async get_bid_result(market_url, bid_id)` — GET /bids/{bid_id}
- `async submit_bid(market_url, bid)` — POST /bids
- `async get_recommendation(energy_url, interval_id, battery_state)` — GET /recommend

Pattern for multi-registry calls: `asyncio.gather(*[call(url) for url in registry_urls], return_exceptions=True)`, collect successes, deduplicate by agent URL.

#### `shared/llm.py`
Model-agnostic LLM router. Port directly from a2a-gambling with no changes needed.

```python
def complete(model: str, messages: list[dict], max_tokens=1000) -> str
    # "anthropic/claude-*" → Anthropic SDK
    # else → OpenAI SDK (covers local models via api_base)
```

Supports `api_base` and `api_key` overrides from config for local models (Ollama, LM Studio).

#### `shared/thought_log.py`
Port directly from a2a-gambling with no changes. Creates `logs/{agent_name}.jsonl`.

Event types used per agent:
- Market: `INTERVAL_CREATED`, `INTERVAL_DISPATCHED`, `PRICE_GENERATED`, `ADAPTATION`
- Trader: `BID_DECISION`, `BID_RESULT`, `SOC_UPDATE`, `AGENT_SWITCHED`
- Energy: `RECOMMENDATION_MADE`, `REGRET_COMPUTED`, `ADAPTATION_RUN`, `TOOL_SYNTHESISED`

#### `registry/server.py`
Port directly from a2a-gambling. Change endpoint names:
- `/casinos` → `/markets`
- `/players` → `/traders`
- Add `/energy` for Energy agents
- Keep `/agents` (all types) and `/report`

Eviction: 180s without heartbeat.

#### `CLAUDE.md`
Project instructions (see separate file).

### Test after Phase 1
```bash
python -m registry.server
# In another terminal:
curl http://localhost:8000/agents  # → []
curl -X POST http://localhost:8000/register \
  -H "Content-Type: application/json" \
  -d '{"url":"http://localhost:8001","name":"test","type":"market"}'
curl http://localhost:8000/markets  # → [{...}]
```

---

## Phase 2 — Market Agent

**Goal**: Market generates realistic price intervals, opens bid windows on schedule, accepts bids, dispatches results.

### Files to create

#### `market/config.yaml`
```yaml
name: "NEM Spot Market"
model: anthropic/claude-sonnet-4-6
agents_md: market/AGENTS.md
seed_balance: 0.0              # market doesn't have a balance
port: 8001
registry_urls:
  - http://localhost:8000
interval_minutes: 5            # real interval length
time_multiplier: 10.0          # 10x speed: 5-min intervals every 30s
bid_window_pct: 0.6            # bid window = 60% of interval duration
lookahead_intervals: 6         # pre-generate this many ahead
grid_capacity_mw: 500.0        # max total volume cleared per interval
market_types:
  - energy
  - fcas_raise
  - fcas_lower
```

#### `market/AGENTS.md`
System prompt for the price generation brain. Instructs the LLM to:
- Generate realistic electricity prices correlated with time of day (peak: 07:00–09:00, 17:00–20:00)
- Add volatility spikes for FCAS (triggered by frequency events)
- Maintain price continuity (no sudden jumps without cause)
- Return JSON: `{"intervals": [{"market_type": "energy", "reference_price": 45.20, "notes": "morning peak"}, ...]}`

#### `market/brain.py`
```python
class Brain(ABC):
    @abstractmethod
    def generate_prices(
        self,
        upcoming_slots: list[dict],    # {market_type, scheduled_at, hour, day_of_week}
        recent_cleared: list[dict],    # last N cleared prices for context
    ) -> list[float]:                  # one reference_price per slot
        ...

class DefaultMarketBrain(Brain):
    # LLM call using shared/llm.py
    # Parses JSON response, validates price ranges (0–15000 $/MWh for energy, 0–500 for FCAS)
    # Falls back to time-of-day heuristic if LLM fails
```

#### `market/scheduler.py`
```python
class IntervalScheduler:
    def __init__(self, cfg: dict):
        self._intervals: dict[str, MarketInterval] = {}
        self._interval_minutes = cfg["interval_minutes"]
        self._multiplier = cfg["time_multiplier"]
        # real_seconds_per_interval = (interval_minutes * 60) / time_multiplier

    def create_interval(self, market_type, scheduled_at, reference_price) -> MarketInterval
    def open_interval(self, interval_id) -> None          # status = "open"
    def close_interval(self, interval_id) -> None         # status = "closed"
    def dispatch_interval(self, interval_id, cleared_price) -> None  # status = "dispatched"
    def get_interval(self, interval_id) -> MarketInterval | None
    def get_open_intervals(self) -> list[MarketInterval]
    def get_upcoming_intervals(self, n: int) -> list[MarketInterval]
    def get_dispatched_intervals(self, since: datetime) -> list[MarketInterval]
```

Interval timing calculation:
```
real_seconds = (interval_minutes * 60) / time_multiplier
bid_window_seconds = real_seconds * bid_window_pct
scheduled_at = now + real_seconds
bid_open_at = now
bid_close_at = now + bid_window_seconds
dispatch_at = scheduled_at
```

#### `market/ledger.py`
```python
@dataclass
class BidRecord:
    bid_id: str
    interval_id: str
    trader_id: str
    trader_url: str
    direction: str
    volume_mw: float
    limit_price: float
    submitted_at: datetime
    result: DispatchResult | None = None

class MarketLedger:
    def __init__(self, grid_capacity_mw: float):
        self._bids: dict[str, BidRecord] = {}          # bid_id → BidRecord
        self._interval_bids: dict[str, list[str]] = {} # interval_id → [bid_ids]
        self._grid_capacity_mw = grid_capacity_mw
        self._cleared_volume: float = 0.0
        self._cleared_revenue: float = 0.0

    def accept_bid(self, bid: DispatchBid) -> str        # returns bid_id
    def get_bid(self, bid_id: str) -> BidRecord | None
    def get_bids_for_interval(self, interval_id: str) -> list[BidRecord]

    def clear_interval(self, interval_id: str, cleared_price: float) -> list[DispatchResult]:
        # For each bid in interval:
        #   discharge: execute if cleared_price >= limit_price
        #   charge: execute if cleared_price <= limit_price
        # Sum all accepted volumes — if > grid_capacity_mw: pro-rata curtailment
        # Compute revenue per bid: volume_cleared * cleared_price * ±1
        # Store results, return list[DispatchResult]

    def stats(self) -> dict
```

#### `market/agent.py`
```python
def load_config(path: str) -> dict:
    # Load YAML, support TIME_MULTIPLIER env override

def build_app(cfg: dict) -> FastAPI:
    scheduler = IntervalScheduler(cfg)
    ledger = MarketLedger(cfg["grid_capacity_mw"])
    brain = DefaultMarketBrain(cfg["agents_md"], cfg["model"], ...)
    log = ThoughtLog("market")

    @asynccontextmanager
    async def lifespan(app):
        await register_agent(cfg["registry_urls"], self_url, cfg["name"], "market")
        tasks = [
            asyncio.create_task(_heartbeat_loop()),
            asyncio.create_task(_price_generation_loop()),
            asyncio.create_task(_scheduler_loop()),
        ]
        yield
        for t in tasks: t.cancel()

    # _heartbeat_loop: every 60s re-register
    # _price_generation_loop: keep lookahead_intervals ahead, call brain.generate_prices()
    # _scheduler_loop: every 5s check all intervals, open/close/dispatch on schedule
    #   dispatch: ledger.clear_interval(interval_id, cleared_price)
    #             log INTERVAL_DISPATCHED

    @app.get("/.well-known/agent.json")
    def agent_card() -> dict:
        # name, type, url, stats_url
        # upcoming_intervals: scheduler.get_upcoming_intervals(6)
        # open_intervals: scheduler.get_open_intervals()

    @app.get("/intervals/{interval_id}")
    def get_interval(interval_id: str) -> MarketInterval

    @app.get("/intervals")
    def list_intervals(status: str | None = None, market_type: str | None = None)

    @app.post("/bids")
    def submit_bid(bid: DispatchBid) -> dict:
        # Validate: interval exists and is open
        # Validate: direction in ["charge", "discharge"]
        # Validate: volume_mw > 0
        # ledger.accept_bid(bid)
        # return {bid_id, interval_id, status: "accepted"}

    @app.get("/bids/{bid_id}")
    def get_bid_result(bid_id: str) -> dict:
        # if result exists: return DispatchResult
        # else: return {status: "pending"}

    @app.get("/stats") → StatsResponse
    @app.get("/thoughts") → recent ThoughtLog entries
```

### Test after Phase 2
```bash
python -m registry.server &
python -m market.agent &
# Watch intervals being created and dispatched
curl http://localhost:8001/.well-known/agent.json
curl http://localhost:8001/intervals?status=open
```

---

## Phase 3 — Trader Agent

**Goal**: Trader discovers market, manages battery SOC, places bids, settles results. Brain is rule-based (always follows Energy agent recommendation, scales down if confidence=low).

### Files to create

#### `trader/config.yaml`
```yaml
name: "Battery Trader Alpha"
model: anthropic/claude-sonnet-4-6
agents_md: trader/AGENTS.md
port: 8002
registry_urls:
  - http://localhost:8000
seed_balance: 100000.0          # starting cash ($)
battery:
  capacity_mwh: 100.0
  max_charge_mw: 50.0
  max_discharge_mw: 50.0
  initial_soc_pct: 0.5          # start at 50%
  efficiency: 0.85
bet_delay_seconds: 30           # bidding loop interval
discovery_interval_seconds: 60
settlement_poll_seconds: 10
min_energy_agent_intervals: 5   # min intervals before switching advisors
regret_switch_threshold: 500.0  # switch if avg_rec_regret exceeds this for 10 intervals
```

#### `trader/AGENTS.md`
System prompt for bid decision brain. Instructs:
- Follow Energy agent recommendation unless confidence="low" (reduce volume by 50%)
- Never exceed SOC physical limits
- Skip interval if no recommendation available (direction="none")

#### `trader/battery.py`
```python
class Battery:
    def __init__(self, cfg: dict):
        self.capacity_mwh = cfg["capacity_mwh"]
        self.max_charge_mw = cfg["max_charge_mw"]
        self.max_discharge_mw = cfg["max_discharge_mw"]
        self.efficiency = cfg["efficiency"]
        self._soc_mwh = cfg["capacity_mwh"] * cfg["initial_soc_pct"]

    @property
    def soc_mwh(self) -> float
    @property
    def soc_pct(self) -> float     # soc_mwh / capacity_mwh

    def can_charge(self) -> bool:  # soc_pct < 0.90
    def can_discharge(self) -> bool: # soc_pct > 0.10

    def max_charge_volume(self, interval_hours: float) -> float:
        # min(max_charge_mw * interval_hours, (capacity - soc) / efficiency)
        # Returns 0 if soc_pct >= 0.90

    def max_discharge_volume(self, interval_hours: float) -> float:
        # min(max_discharge_mw * interval_hours, soc * efficiency)
        # Returns 0 if soc_pct <= 0.10

    def apply_charge(self, volume_mwh: float) -> None:
        # soc += volume_mwh * efficiency (energy in)
        # clamp to capacity

    def apply_discharge(self, volume_mwh: float) -> None:
        # soc -= volume_mwh / efficiency (energy out)
        # clamp to 0
```

All methods enforce hard physical constraints — never raises or ignores, just returns 0 or clamps.

#### `trader/ledger.py`
```python
@dataclass
class DispatchRecord:
    bid_id: str
    interval_id: str
    market_url: str
    energy_agent_url: str | None
    direction: str
    volume_mw: float
    revenue: float
    soc_before: float
    soc_after: float
    timestamp: datetime

@dataclass
class PerformanceRecord:
    agent_url: str
    intervals_advised: int
    total_rec_regret: float
    recent_regrets: list[float]   # last 10
    confidence_correct: int
    confidence_total: int
    last_queried_at: datetime

    @property
    def avg_rec_regret(self) -> float
    @property
    def confidence_accuracy(self) -> float

class TraderLedger:
    def __init__(self, seed_balance: float, battery: Battery):
        self.balance = seed_balance
        self.seed_balance = seed_balance
        self._records: list[DispatchRecord] = []
        self._pending: dict[str, dict] = {}           # bid_id → {interval_id, energy_agent_url, ...}
        self._energy_performance: dict[str, PerformanceRecord] = {}
        self._already_bid: set[str] = set()           # interval_ids this cycle

    def record_pending(self, bid_id, interval_id, energy_agent_url, direction, volume_mw, soc_before)
    def record_settled(self, bid_id, result: DispatchResult, soc_after: float)
    def update_agent_performance(self, agent_url, rec_regret, confidence_was_correct)
    def best_energy_agent(self) -> str | None         # by avg_rec_regret, min 5 intervals
    def prune_already_bid(self, current_interval_ids: set[str])  # remove resolved intervals
    def profit(self) -> float
    def roi(self) -> float
    def stats(self) -> dict
```

#### `trader/brain.py`
```python
class Brain(ABC):
    @abstractmethod
    def select_energy_agent(
        self,
        available: list[EnergyAgentCard],
        performance: dict[str, PerformanceRecord],
        battery_state: BatteryState,
    ) -> str | None:              # agent URL or None (bid without advisor)
        ...

    @abstractmethod
    def decide_bid(
        self,
        recommendation: Recommendation | None,
        battery: Battery,
        interval: MarketInterval,
        interval_hours: float,
    ) -> tuple[str, float, float]:  # (direction, volume_mw, limit_price)
        ...

class DefaultTraderBrain(Brain):
    def select_energy_agent(self, available, performance, battery_state) -> str | None:
        # If current agent avg_rec_regret > threshold for 10 intervals: switch
        # Switch to: lowest avg_rec_regret among those with >= min_intervals advised
        # If no data yet: pick first available

    def decide_bid(self, recommendation, battery, interval, interval_hours):
        # If recommendation is None or direction="none": return ("none", 0, 0)
        # direction = recommendation.direction
        # volume = recommendation.volume_mw
        # If confidence="low": volume *= 0.5
        # Clamp to battery physical limits
        # limit_price = recommendation.limit_price
        # return (direction, volume, limit_price)
```

#### `trader/agent.py`
```python
def build_app(cfg: dict) -> FastAPI:
    battery = Battery(cfg["battery"])
    ledger = TraderLedger(cfg["seed_balance"], battery)
    brain = DefaultTraderBrain(cfg)
    log = ThoughtLog("trader")

    known_markets: list[dict] = []
    known_energy_agents: list[EnergyAgentCard] = []

    @asynccontextmanager
    async def lifespan(app):
        await register_agent(...)
        tasks = [
            asyncio.create_task(_heartbeat_loop()),
            asyncio.create_task(_discovery_loop()),
            asyncio.create_task(_bidding_loop()),
            asyncio.create_task(_settlement_loop()),
        ]
        yield
        for t in tasks: t.cancel()

    # _discovery_loop (60s):
    #   known_markets = await get_markets(registry_urls)
    #   known_energy_agents = await get_energy_agents(registry_urls)
    #   ledger.prune_already_bid(current_open_interval_ids)

    # _bidding_loop (30s):
    #   for each market in known_markets:
    #     card = await get_agent_card(market.url)
    #     for interval in card.open_intervals:
    #       if interval.interval_id in ledger._already_bid: continue
    #       agent_url = brain.select_energy_agent(known_energy_agents, ledger._energy_performance, battery.state)
    #       rec = await get_recommendation(agent_url, interval.interval_id, battery.state) if agent_url else None
    #       direction, volume, limit_price = brain.decide_bid(rec, battery, interval, interval_hours)
    #       if direction == "none": continue
    #       result = await submit_bid(market.url, DispatchBid(...))
    #       ledger.record_pending(result.bid_id, interval.interval_id, agent_url, direction, volume, battery.soc_mwh)
    #       ledger._already_bid.add(interval.interval_id)
    #       log.write("BID_DECISION", reasoning, {...})

    # _settlement_loop (10s):
    #   for bid_id in list(ledger._pending.keys()):
    #     result = await get_bid_result(market_url, bid_id)
    #     if result.status == "pending": continue
    #     if direction == "charge": battery.apply_charge(result.volume_cleared * interval_hours)
    #     if direction == "discharge": battery.apply_discharge(result.volume_cleared * interval_hours)
    #     ledger.record_settled(bid_id, result, battery.soc_mwh)
    #     log.write("BID_RESULT", ..., {revenue, soc_after})
    #     # deadline write-off: if interval dispatched > 10min ago, remove from pending

    @app.get("/.well-known/agent.json") → AgentCard + battery_state + current_pnl
    @app.get("/stats") → StatsResponse
    @app.get("/thoughts") → ThoughtLog
```

### Test after Phase 3
```bash
python -m registry.server &
python -m market.agent &
python -m trader.agent &
# Energy agent not running yet — trader should handle gracefully (no recommendation, no bid)
curl http://localhost:8002/stats
curl http://localhost:8002/.well-known/agent.json  # shows battery state
```

---

## Phase 4 — Energy Agent (Basic Recommendation)

**Goal**: Energy agent investigates market history, returns a grounded recommendation. Quality gates (hard gate + judge) enforce that ungrounded or low-quality recommendations don't reach the Trader.

### Files to create

#### `energy/config.yaml`
```yaml
name: "Energy Advisor Alpha"
specialization: "balanced"
model: anthropic/claude-sonnet-4-6
judge_model: openai/gpt-4o-mini   # or another provider for independence
agents_md: energy/AGENTS.md
port: 8003
registry_urls:
  - http://localhost:8000
min_recommendation_seconds: 15    # abort if less time left in bid window
max_investigator_iterations: 4    # cap tool calls for latency
seed_balance: 0.0                 # energy agent has no balance
result_ingestion_interval: 30     # seconds between polling market for results
adaptation_interval_intervals: 100  # run adaptation every N completed recs
tool_synthesis_interval_intervals: 200
```

#### `energy/AGENTS.md`
System prompt for the recommendation brain. Instructs:
- Investigate market history using available tools before recommending
- Recommend based on current SOC, recent price pattern, time of day
- Direction: "charge" when prices are low and SOC has room; "discharge" when prices are high and SOC allows
- Cite which tool call supports each element of the recommendation (grounding requirement)
- If insufficient evidence, recommend direction="none"

#### `energy/analysis/schemas.py`
Port from energyproject `src/schemas.py`. Remove CSV-specific fields, add energy-simulation-specific schemas:
- Keep: `Provenance`, `GapFacts`, `SliceEntry`, `SliceByHourOutput`, `SliceByPriceBucketOutput`, `SliceBySOCRegimeOutput`, `ForecastErrorOutput`
- Add: `MarketStateSnapshot` (soc_pct, hour_of_day, price_trend_slope, market_type, recent_prices)
- Replace: `Report` → `Recommendation` (from shared/models.py, already defined)

#### `energy/analysis/gap.py`
Adapted from energyproject. Instead of CSV DataFrame, operates on list of `RecommendationRecord`.
```python
def compute_regret_summary(records: list[RecommendationRecord]) -> GapFacts:
    # hist_cleared_revenue = sum(revenue_if_followed for records with result)
    # perf_cleared_revenue = sum(perfect_foresight_revenue for same)
    # gap = perf - hist
```

#### `energy/analysis/slices.py`
Adapted from energyproject. Operates on interval history records (dicts with hour, price, soc, regret).

#### `energy/analysis/diagnostic.py`
Adapted: compares recommended price vs cleared price, finds top mispredicted intervals.

#### `energy/analysis/inspector.py`
Adapted: filters interval history by hour, price range, SOC range, direction.

#### `energy/tools.py`
Thin Strands `@tool` wrappers around analysis functions. `make_tools(interval_history)` returns list of tools:

1. `compute_regret_summary()` — overall rec_regret summary
2. `slice_by_hour()` — regret across 24 hours of day
3. `slice_by_price_bucket()` — regret across Q1-Q4 price quartiles
4. `slice_by_soc_regime()` — regret across low/mid/high SOC
5. `forecast_error_diagnostic()` — recommended price vs cleared price disparity
6. `inspect_intervals(hour_min, hour_max, price_min, price_max, soc_min, soc_max, direction, limit)` — raw drill-down
7. `find_similar_intervals(soc_pct, hour_of_day, price_trend_slope, market_type)` — retrieval from LearningLedger (Phase 6)
8. `get_strategy_context()` — returns current learned rules from strategy_context.md

Tools 7 and 8 return empty results in Phase 4 (LearningLedger not yet populated). They're registered now so the hard gate can reference them.

#### `energy/brain.py`
```python
class Brain(ABC):
    @abstractmethod
    async def recommend(
        self,
        interval: MarketInterval,
        battery_state: BatteryState,
        interval_history: list[dict],  # recent intervals with prices + outcomes
        time_to_bid_close_seconds: float,
    ) -> Recommendation:
        ...

class DefaultEnergyBrain(Brain):
    async def recommend(self, interval, battery_state, interval_history, time_to_bid_close):
        # 1. Deadline guard
        if time_to_bid_close < cfg["min_recommendation_seconds"]:
            return Recommendation(direction="none", reasoning="bid window closing")

        # 2. Strands investigator (port from energyproject src/agent/investigator.py)
        #    max_iterations = cfg["max_investigator_iterations"] (4, not 8)
        #    tools = make_tools(interval_history)
        #    findings = await investigate(interval, battery_state, tools, trace)

        # 3. Narrator: structured output → Recommendation
        #    recommendation = await narrate(findings, interval, battery_state)

        # 4. Hard gate
        if not _hard_gate_passes(recommendation, trace):
            return Recommendation(direction="none", reasoning="grounding check failed")

        # 5. Judge (soft gate — parallel, non-blocking)
        asyncio.create_task(_run_judge_async(recommendation, trace))
        # Judge result updates confidence after the fact if needed

        return recommendation

def _hard_gate_passes(rec: Recommendation, trace: dict) -> bool:
    # Every tool in rec.evidence_tool_calls must appear in trace
    # direction must be one of "charge" / "discharge" / "none"
    # volume_mw >= 0
    # limit_price >= 0
```

#### `energy/prompts/investigator.md`
Adapted from energyproject `prompts/investigator.md`. Key changes:
- Context is current market state (SOC, hour, price trend), not a static CSV
- Tools operate on live interval history
- Goal: find PRIMARY pattern explaining optimal action right now
- Max 4 tool calls (not 8) — time-sensitive

#### `energy/prompts/narrator.md`
Adapted from energyproject. Output is a `Recommendation` struct, not a `Report`.

#### `energy/prompts/judge.md`
Adapted from energyproject. Rubric:
- `grounding [0,1]`: direction/volume/limit_price all trace to tool evidence
- `specificity [0,1]`: limit_price is a real number, volume is physically plausible
- `soc_validity [0,1]`: recommended direction is possible given stated SOC
- `timeliness [0,1]`: reasoning references current market conditions, not stale history

#### `energy/ledger.py` (basic version for Phase 4)
```python
class LearningLedger:
    def __init__(self):
        self._records: dict[str, RecommendationRecord] = {}  # recommendation_id → record
        self._pending: set[str] = set()  # interval_ids awaiting cleared_price

    def record_recommendation(self, rec: Recommendation, market_state: dict) -> None
    def get_pending_interval_ids(self) -> list[str]
    def get_record_by_interval(self, interval_id: str) -> RecommendationRecord | None
    def all_completed(self) -> list[RecommendationRecord]  # where rec_regret is not None
    def recent_interval_history(self, n: int = 200) -> list[dict]
        # Returns last N dispatched intervals as dicts for tool consumption
    # Phase 6 will add: fill_result(), compute_regret(), regret_clusters()
```

#### `energy/agent.py`
```python
def build_app(cfg: dict) -> FastAPI:
    ledger = LearningLedger()
    brain = DefaultEnergyBrain(cfg, ledger)
    log = ThoughtLog("energy")

    known_markets: list[dict] = []

    @asynccontextmanager
    async def lifespan(app):
        await register_agent(..., type="energy")
        tasks = [
            asyncio.create_task(_heartbeat_loop()),
            asyncio.create_task(_discovery_loop()),
            asyncio.create_task(_result_ingestion_loop()),
        ]
        yield

    # _discovery_loop (60s): known_markets = await get_markets(registry_urls)

    # _result_ingestion_loop (30s):
    #   for interval_id in ledger.get_pending_interval_ids():
    #     for market in known_markets:
    #       interval = await get_interval(market.url, interval_id)
    #       if interval.status == "dispatched" and interval.cleared_price is not None:
    #         # Phase 6 will compute rec_regret here
    #         ledger.mark_ingested(interval_id, interval.cleared_price)
    #         log.write("REGRET_COMPUTED", ...)

    @app.get("/recommend")
    async def recommend(
        interval_id: str,
        battery_state: str,  # JSON-encoded BatteryState
        market_url: str,
    ) -> Recommendation:
        interval = await get_interval(market_url, interval_id)
        battery = BatteryState.model_validate_json(battery_state)
        time_left = (interval.bid_close_at - datetime.utcnow()).total_seconds()
        history = ledger.recent_interval_history(200)
        rec = await brain.recommend(interval, battery, history, time_left)
        ledger.record_recommendation(rec, market_state_snapshot(interval, battery))
        log.write("RECOMMENDATION_MADE", rec.reasoning, {rec.dict()})
        return rec

    @app.get("/.well-known/agent.json") → EnergyAgentCard
    @app.get("/stats") → StatsResponse
    @app.get("/insights") → recent LearningLedger summary
    @app.get("/thoughts") → ThoughtLog
```

### Test after Phase 4
```bash
python -m registry.server &
python -m market.agent &
python -m energy.agent &
python -m trader.agent &
# Watch recommendations and bids flow
curl "http://localhost:8003/recommend?interval_id=X&battery_state={...}&market_url=http://localhost:8001"
```

---

## Phase 5 — Observer

**Goal**: HTML dashboard showing all agents, market prices, trader P&L, Energy agent performance.

### Files to create

#### `observer/config.yaml`
```yaml
registry_urls:
  - http://localhost:8000
poll_interval_seconds: 30
port: 8080
```

#### `observer/rankings.py`
```python
def rank_markets(stats: list[StatsResponse]) -> list[StatsResponse]:
    return sorted([s for s in stats if s.type == "market"], key=lambda s: s.total_volume, reverse=True)

def rank_traders(stats: list[StatsResponse]) -> list[StatsResponse]:
    return sorted([s for s in stats if s.type == "trader"], key=lambda s: s.roi, reverse=True)

def rank_energy_agents(cards: list[EnergyAgentCard]) -> list[EnergyAgentCard]:
    # Sort by avg_rec_regret ascending (lower = better)
    # Flag where self-reported vs observer-tracked regret diverges > threshold
    return sorted([c for c in cards if c.avg_rec_regret is not None], key=lambda c: c.avg_rec_regret)

def network_summary(stats: list[StatsResponse]) -> dict:
    return {
        "total_markets": count,
        "total_traders": count,
        "total_energy_agents": count,
        "total_volume_mwh": sum trader total_volume,
        "total_pnl": sum trader profit,
        "top_energy_agent": rank_energy_agents(cards)[0].name if any,
    }
```

#### `observer/agent.py`
Port structure from a2a-gambling observer with these additions:

New endpoints:
- `GET /rankings/markets`
- `GET /rankings/traders`
- `GET /rankings/energy` (new — Energy agent leaderboard)
- `GET /network`
- `GET /schedule` — upcoming market intervals from all markets
- `GET /` — HTML dashboard

Dashboard sections:
- **Overview**: KPI cards (agents online, total volume, net P&L, top advisor)
- **Markets**: table of market agents, price history chart (last 24 intervals)
- **Traders**: table sorted by ROI, battery SOC indicator per trader
- **Energy Agents**: leaderboard table (name, specialization, intervals_advised, avg_rec_regret, learning_generation, active_traders, cross-validation flag)
- **Schedule**: upcoming intervals with open/close/dispatch countdowns
- **Thoughts**: recent events from all agents

Cross-validation logic in poll_loop:
```python
# For each energy agent in known_energy_agents:
#   self_reported = card.avg_rec_regret
#   trader_observed = compute_trader_observed_regret(agent_url, trader_stats)
#   if abs(self_reported - trader_observed) / trader_observed > 0.20:
#       flag agent in rankings
```

### Test after Phase 5
```bash
# Start all agents, then:
open http://localhost:8080
```

---

## Phase 6 — Energy Agent Learning

**Goal**: Energy agent computes rec_regret for every completed recommendation, distills lessons into strategy_context, and uses retrieval to inform future recommendations.

### Changes to existing files

#### `energy/ledger.py` (full implementation)
Add to `LearningLedger`:
```python
def fill_result(self, interval_id: str, cleared_price: float) -> None:
    record = self._records_by_interval[interval_id]
    record.cleared_price = cleared_price
    record.perfect_foresight_revenue = _compute_perfect(cleared_price, record.recommendation)
    rec_revenue = _compute_if_followed(cleared_price, record.recommendation)
    record.rec_regret = record.perfect_foresight_revenue - rec_revenue
    record.ingested_at = datetime.utcnow()
    self._pending.discard(interval_id)

def regret_clusters(self, n: int = 200) -> list[dict]:
    # Group last N completed records by market_state features
    # Find clusters with high avg_rec_regret not explained by existing tools
    # Return: [{pattern, avg_regret, n_records, feature_values}]

def similar_intervals(
    self, soc_pct: float, hour: int, price_slope: float, market_type: str, top_k: int = 10
) -> list[RecommendationRecord]:
    # Feature similarity: SOC within ±10%, same hour ±1, similar slope
    # Return top_k by similarity score
```

#### `energy/retrieval.py`
```python
def make_retrieval_tool(ledger: LearningLedger):
    @tool
    def find_similar_intervals(
        soc_pct: float, hour_of_day: int, price_trend_slope: float, market_type: str
    ) -> dict:
        """Find past intervals with similar conditions and show outcomes."""
        records = ledger.similar_intervals(soc_pct, hour_of_day, price_trend_slope, market_type)
        return {
            "n_found": len(records),
            "examples": [
                {
                    "hour": r.market_state_snapshot["hour_of_day"],
                    "soc_pct": r.market_state_snapshot["soc_pct"],
                    "recommended": r.recommendation.direction,
                    "volume_mw": r.recommendation.volume_mw,
                    "cleared_price": r.cleared_price,
                    "rec_regret": r.rec_regret,
                }
                for r in records
            ],
            "provenance": {"method": "similarity_retrieval", "n_rows": len(records), "version": "0.1.0"},
        }
    return find_similar_intervals
```

#### `energy/adaptation.py`
```python
class AdaptationLoop:
    def __init__(self, ledger: LearningLedger, brain: Brain, log: ThoughtLog, cfg: dict):
        self._intervals_since_adapt = 0
        self._adapt_every = cfg["adaptation_interval_intervals"]

    async def tick(self):
        self._intervals_since_adapt += 1
        if self._intervals_since_adapt < self._adapt_every:
            return
        self._intervals_since_adapt = 0

        records = self.ledger.all_completed()[-200:]
        if len(records) < 20:
            return

        # Run analysis tools against records
        history = [r.to_dict() for r in records]
        gap = compute_regret_summary(records)
        slices = [slice_by_hour(history), slice_by_soc_regime(history)]

        # Ask Brain to distill lessons
        lessons = await self.brain.distill_lessons(gap, slices, records)

        # Rewrite strategy_context.md
        _write_strategy_context(lessons)
        self.log.write("ADAPTATION_RUN", f"generation {self._generation}", {
            "n_records": len(records),
            "avg_rec_regret": gap.gap_absolute / max(len(records), 1),
            "lessons_count": len(lessons),
        })
        self._generation += 1
```

Add `brain.distill_lessons()` method — LLM call that takes gap facts + slice results → returns list of compact rule strings → written to `energy/strategy_context.md`.

`get_strategy_context()` tool reads `strategy_context.md` and returns its contents. Empty string if file doesn't exist yet.

Update `result_ingestion_loop` in `energy/agent.py`:
```python
# After ingesting result:
ledger.fill_result(interval_id, cleared_price)
adaptation_loop.tick()
```

---

## Phase 7 — Multi-Energy-Agent Competition

**Goal**: Multiple Energy agents can run simultaneously. Traders discover all, track performance, and switch advisors. Observer shows leaderboard with cross-validation.

### Changes to existing files

#### `shared/a2a.py`
Add `get_energy_agents(registry_urls) -> list[EnergyAgentCard]`.

#### `trader/ledger.py`
`PerformanceRecord` already designed. Add:
```python
def should_switch_agent(self, current_url: str, min_intervals: int, threshold: float) -> bool:
    rec = self._energy_performance.get(current_url)
    if rec is None or rec.intervals_advised < min_intervals:
        return False
    recent = rec.recent_regrets[-10:]
    return len(recent) >= 10 and sum(recent) / len(recent) > threshold
```

#### `trader/brain.py`
`select_energy_agent()` uses `ledger.should_switch_agent()` + `ledger.best_energy_agent()` to switch when threshold exceeded.

After settlement, update performance:
```python
ledger.update_agent_performance(
    agent_url=bid_record.energy_agent_url,
    rec_regret=observed_regret,         # perfect_foresight - actual (simplified)
    confidence_was_correct=...,
)
```

#### `energy/agent.py`
`EnergyAgentCard` now includes populated performance fields from `LearningLedger`:
```python
@app.get("/.well-known/agent.json")
def agent_card() -> EnergyAgentCard:
    completed = ledger.all_completed()
    avg_regret = mean([r.rec_regret for r in completed]) if completed else None
    return EnergyAgentCard(
        ...,
        intervals_advised=len(completed),
        avg_rec_regret=avg_regret,
        learning_generation=adaptation_loop._generation,
        active_traders=_count_recent_queries(),
    )
```

#### `observer/rankings.py`
`rank_energy_agents()` cross-validates self-reported vs tracker:
```python
def rank_energy_agents(cards: list[EnergyAgentCard], trader_records: dict) -> list[dict]:
    ranked = []
    for card in cards:
        trader_observed = _compute_trader_observed(card.url, trader_records)
        delta = abs(card.avg_rec_regret - trader_observed) / max(trader_observed, 1)
        ranked.append({
            "card": card,
            "trader_observed_regret": trader_observed,
            "delta_pct": delta * 100,
            "flagged": delta > 0.20,
        })
    return sorted(ranked, key=lambda x: x["card"].avg_rec_regret or 999)
```

### Running multiple Energy agents
```bash
# Person A:
ENERGY_PORT=8003 ENERGY_NAME="Advisor Alpha" python -m energy.agent

# Person B (different config, model, strategy):
ENERGY_PORT=8004 ENERGY_NAME="FCAS Specialist" python -m energy.agent

# Both register with shared registry
# Both appear in Observer leaderboard
# Traders discover both and route to the better one
```

Each agent has its own `config.yaml` or uses env var overrides:
```yaml
# energy/config.yaml
name: "FCAS Specialist"
specialization: "fcas_focus"
port: 8004
```

---

## Phase 8 — ToolSynthesisLoop (Advanced)

**Goal**: Energy agent identifies recurring patterns its existing tools can't explain and generates new analysis tools to address them.

### `energy/adaptation.py` (extended)
```python
class ToolSynthesisLoop:
    def __init__(self, ledger, brain, tool_registry, log, cfg):
        self._intervals_since_synthesis = 0
        self._synthesise_every = cfg["tool_synthesis_interval_intervals"]

    async def tick(self):
        self._intervals_since_synthesis += 1
        if self._intervals_since_synthesis < self._synthesise_every:
            return
        self._intervals_since_synthesis = 0

        clusters = self.ledger.regret_clusters(200)
        if not clusters:
            return

        largest = max(clusters, key=lambda c: c["avg_regret"] * c["n_records"])
        if largest["avg_regret"] < SYNTHESIS_REGRET_THRESHOLD:
            return

        # Ask Brain to write a new tool
        tool_code = await self.brain.synthesise_tool(largest, self.tool_registry.tool_names())

        # Backtest: would this tool have helped on the cluster intervals?
        improvement = _backtest_tool(tool_code, largest["interval_ids"], self.ledger)

        if improvement > SYNTHESIS_IMPROVEMENT_THRESHOLD:
            tool_name = _extract_tool_name(tool_code)
            path = f"energy/tools/generated/{tool_name}.py"
            _write_tool(path, tool_code)
            self.tool_registry.register_from_file(path)
            self.log.write("TOOL_SYNTHESISED", f"new tool: {tool_name}", {
                "cluster_pattern": largest["pattern"],
                "estimated_improvement": improvement,
            })
```

Add `brain.synthesise_tool(cluster, existing_tool_names)` — LLM call that generates a Python function with `@tool` decorator targeting the described regret cluster. Backtest harness reruns the investigator on cluster intervals with the new tool available, compares avg rec_regret before/after.

---

## Startup Order

```bash
# 1. Registry first
python -m registry.server          # port 8000

# 2. Market (generates intervals immediately)
python -m market.agent             # port 8001

# 3. Energy agent(s) (before Trader so first recommendation is available)
python -m energy.agent             # port 8003
# optional: ENERGY_PORT=8004 python -m energy.agent   # second competitor

# 4. Trader
python -m trader.agent             # port 8002

# 5. Observer (anytime)
python -m observer.agent           # port 8080
open http://localhost:8080
```

---

## Key Files Per Phase Summary

| Phase | New files | Modified files |
|---|---|---|
| 1 | shared/*, registry/server.py, requirements.txt, CLAUDE.md | — |
| 2 | market/* | shared/models.py (MarketInterval, DispatchBid, DispatchResult) |
| 3 | trader/* | shared/a2a.py (submit_bid, get_bid_result) |
| 4 | energy/*, energy/analysis/*, energy/prompts/* | shared/a2a.py (get_recommendation), shared/models.py (Recommendation) |
| 5 | observer/* | shared/a2a.py (get_energy_agents) |
| 6 | energy/retrieval.py, energy/adaptation.py (AdaptationLoop) | energy/ledger.py, energy/agent.py, energy/tools.py |
| 7 | — | trader/brain.py, trader/ledger.py, observer/rankings.py, energy/agent.py |
| 8 | energy/tools/generated/ | energy/adaptation.py (ToolSynthesisLoop), energy/brain.py |
