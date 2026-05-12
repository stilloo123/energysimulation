# Energy Simulation — Design Document

## Vision

A distributed multi-agent simulation of an electricity market where autonomous agents interact via an A2A (Agent-to-Agent) HTTP protocol. The simulation models three real-world roles: a **Market** that generates price signals and clears dispatch bids, a **Trader** that manages a battery asset and places bids to maximise revenue, and an **Energy agent** that acts as an intelligent advisor — recommending optimal charge/discharge decisions in real time and improving its recommendations over time through a learning loop grounded in market outcomes.

Multiple Energy agents can be hosted by different people and compete on recommendation quality, measured against a public ground truth (the market's cleared price). Traders discover all available Energy agents and migrate toward the best-performing one, creating natural competitive pressure that drives improvement.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        Registry :8000                        │
│         Stateless agent directory — dumb URL store           │
└───────────┬──────────────────────────────────────┬──────────┘
            │ register / heartbeat                  │ discover
            ▼                                       ▼
┌───────────────────┐                  ┌─────────────────────┐
│   Market :8001    │◄─── POST /bids ──│   Trader :8002      │
│                   │                  │                       │
│  Generates price  │──── result ─────►│  Manages battery     │
│  intervals, opens │                  │  SOC, places bids,   │
│  bid windows,     │                  │  tracks P&L          │
│  clears dispatch  │                  └──────────┬──────────┘
└───────────────────┘                             │ GET /recommend
                                                  ▼
                                    ┌─────────────────────────┐
                                    │  Energy Agent :8003+    │
                                    │                         │
                                    │  Real-time advisor.     │
                                    │  Investigates market    │
                                    │  history, recommends    │
                                    │  direction + volume +   │
                                    │  limit price. Learns    │
                                    │  from every outcome.    │
                                    │                         │
                                    │  Multiple instances     │
                                    │  compete on quality.    │
                                    └─────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      Observer :8080                          │
│   Polls all agents · HTML dashboard · Rankings per role      │
└─────────────────────────────────────────────────────────────┘
```

---

## Agent Roles

### Registry (port 8000)
Stateless FastAPI directory. Agents register on startup and heartbeat every 60s. Registry evicts stale entries after 180s (3 missed beats). Exposes `/markets`, `/traders`, `/energy`, `/observers`, `/agents`. No market data — just URLs and agent metadata.

### Market (port 8001)
Simulates an electricity spot market and FCAS (Frequency Control Ancillary Services) market. Responsibilities:
- LLM brain generates realistic price scenarios: time-of-day patterns, volatility regimes, correlated FCAS spikes
- Scheduler opens bid windows for upcoming intervals, closes them at deadline, dispatches at interval time
- Accepts `POST /bids` from Traders, stores bids, clears them at dispatch
- Posts `DispatchResult` (cleared price, volume cleared, revenue) publicly via `GET /bids/{bid_id}`
- Maintains `GET /intervals/{id}` so Energy agents can poll cleared prices for learning
- AgentCard exposes next N upcoming intervals so Traders and Energy agents can plan ahead
- Volume cap: total cleared volume per interval ≤ grid capacity (config), pro-rata curtailment if oversubscribed

### Trader (port 8002)
Manages a battery asset with physical constraints. Responsibilities:
- Battery state machine: SOC (state of charge), capacity, max charge/discharge rate, round-trip efficiency
- Discovery loop finds all Markets and Energy agents from registry
- For each open interval: selects an Energy agent, requests a recommendation, decides whether and how to bid
- Submits `POST /bids` to Market, polls result via `GET /bids/{bid_id}`
- Tracks P&L per market, per Energy agent — knows which advisor performs best
- Switches Energy agents if current one's rolling recommendation regret exceeds threshold
- Hard SOC constraints enforced in `battery.py` — not suggestions:
  - SOC < 10%: only charge bids allowed
  - SOC > 90%: only discharge bids allowed
  - Volume capped at physically available capacity

### Energy Agent (port 8003+, multiple instances)
Real-time intelligent advisor. Responsibilities:
- Exposes `GET /recommend` — Trader queries before each bid decision
- Runs a tool-use investigation pipeline against live market history
- Hard gate: recommendation must be grounded in tool evidence — ungrounded recommendations return `direction="none"`
- Soft gate (judge): scores recommendation quality, downgrades confidence if low
- Bid-window deadline guard: if `time_to_bid_close < min_recommendation_seconds`, return `direction="none"` immediately
- `result_ingestion_loop`: polls Market for cleared prices on intervals where it made recommendations, computes `rec_regret`
- `adaptation_loop`: every N intervals, distills LearningLedger into updated `strategy_context.md`
- `tool_synthesis_loop`: every M intervals, finds unexplained regret clusters, generates and backtests new analysis tools
- AgentCard exposes self-reported performance metrics (cross-validated by Observer against actual trader outcomes)

### Observer (port 8080)
Polling aggregator and dashboard. Responsibilities:
- Polls `/stats` and AgentCards from all registered agents every 30s
- HTML dashboard: market prices, trader P&L, battery SOC, Energy agent accuracy over time
- `/rankings/markets` — sorted by volume cleared
- `/rankings/traders` — sorted by ROI
- `/rankings/energy` — sorted by avg rec_regret (lower is better)
- Cross-validation: compares Energy agent self-reported rec_regret vs trader-observed actual outcomes, flags discrepancies

---

## Directory Structure

```
energysimulation/
├── shared/
│   ├── __init__.py
│   ├── models.py          # all Pydantic data models
│   ├── a2a.py             # async HTTP helpers, registry calls
│   ├── llm.py             # model-agnostic LLM router (Anthropic / OpenAI / local)
│   └── thought_log.py     # JSONL event journaling for all agents
│
├── registry/
│   ├── __init__.py
│   └── server.py          # stateless FastAPI directory
│
├── market/
│   ├── __init__.py
│   ├── agent.py           # FastAPI app, lifespan, endpoints
│   ├── brain.py           # LLM price generation (Brain ABC + DefaultMarketBrain)
│   ├── scheduler.py       # interval lifecycle state machine
│   ├── ledger.py          # bid storage, result storage, volume cap
│   ├── AGENTS.md          # system prompt for price generation brain
│   └── config.yaml
│
├── trader/
│   ├── __init__.py
│   ├── agent.py           # FastAPI app, lifespan, endpoints
│   ├── brain.py           # bid decision + energy agent selection (Brain ABC)
│   ├── battery.py         # SOC state machine with hard physical constraints
│   ├── ledger.py          # P&L tracking, per-energy-agent performance records
│   ├── AGENTS.md          # system prompt for bid decision brain
│   └── config.yaml
│
├── energy/
│   ├── __init__.py
│   ├── agent.py           # FastAPI app, lifespan, /recommend, /insights
│   ├── brain.py           # Strands-based recommendation engine
│   ├── ledger.py          # LearningLedger (RecommendationRecords + regret)
│   ├── adaptation.py      # AdaptationLoop + ToolSynthesisLoop
│   ├── retrieval.py       # similar-situation lookup from LearningLedger
│   ├── tool_registry.py   # static tool registry + generated tool loader
│   ├── tools/
│   │   └── generated/     # dynamically generated tools land here
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── gap.py         # revenue gap computation (adapted from energyproject)
│   │   ├── slices.py      # hour / price-bucket / SOC-regime slicing
│   │   ├── diagnostic.py  # expected vs cleared disparity analysis
│   │   ├── inspector.py   # interval drill-down with rich filtering
│   │   └── schemas.py     # Pydantic schemas for all analysis outputs
│   ├── prompts/
│   │   ├── investigator.md
│   │   ├── narrator.md
│   │   └── judge.md
│   ├── AGENTS.md          # system prompt for recommendation brain
│   └── config.yaml
│
├── observer/
│   ├── __init__.py
│   ├── agent.py           # polling loop, HTML dashboard, REST rankings
│   ├── rankings.py        # ranking logic for all agent types
│   └── config.yaml
│
├── requirements.txt
├── start.sh
├── stop.sh
├── smoke_test.py
├── .env.sample
├── assets/
│   └── dashboard.png
└── CLAUDE.md
```

---

## Data Models (`shared/models.py`)

### Market side

```python
class MarketInterval:
    interval_id: str
    market_type: Literal["energy", "fcas_raise", "fcas_lower"]
    scheduled_at: datetime
    bid_open_at: datetime       # bid window opens
    bid_close_at: datetime      # bid window closes
    dispatch_at: datetime       # price revealed, result posted
    status: Literal["scheduled", "open", "closed", "dispatched"]
    cleared_price: float | None # None until dispatched — posted directly, no hash
    reference_price: float | None  # indicative pre-dispatch price hint (optional)

class DispatchBid:
    bid_id: str
    interval_id: str
    trader_id: str
    trader_url: str
    direction: Literal["charge", "discharge", "none"]
    volume_mw: float
    limit_price: float          # charge: only if cleared_price <= limit; discharge: only if >= limit
    submitted_at: datetime

class DispatchResult:
    bid_id: str
    interval_id: str
    cleared_price: float
    volume_cleared: float       # may be less than bid volume (curtailment)
    revenue: float              # positive = earned (discharge), negative = cost (charge)
    dispatched_at: datetime
    curtailed: bool
```

### Trader side

```python
class BatteryState:
    soc_mwh: float
    capacity_mwh: float
    max_charge_mw: float
    max_discharge_mw: float
    efficiency: float           # round-trip 0–1 (e.g. 0.85)

class BetRecord:               # renamed: DispatchRecord in trader ledger
    bid_id: str
    interval_id: str
    market_url: str
    energy_agent_url: str | None  # which advisor was queried
    direction: str
    volume_mw: float
    revenue: float
    soc_before: float
    soc_after: float
    timestamp: datetime
```

### Energy agent side

```python
class Recommendation:
    recommendation_id: str
    interval_id: str
    direction: Literal["charge", "discharge", "none"]
    volume_mw: float
    limit_price: float
    confidence: Literal["high", "low"]
    reasoning: str
    evidence_tool_calls: list[str]  # which tools were called (hard gate grounding)
    similar_past_interval_ids: list[str]
    generated_at: datetime

class RecommendationRecord:    # stored in LearningLedger
    recommendation: Recommendation
    market_state_snapshot: dict  # SOC%, hour_of_day, recent_price_slope, market_type
    cleared_price: float | None  # filled by result_ingestion_loop
    perfect_foresight_revenue: float | None
    rec_regret: float | None    # perfect - revenue_if_followed_rec
    ingested_at: datetime | None
```

### Shared / discovery

```python
class AgentCard:
    name: str
    type: Literal["market", "trader", "energy", "observer"]
    url: str
    stats_url: str

class EnergyAgentCard(AgentCard):  # extended for energy type
    specialization: str             # "balanced", "spot_focus", "fcas_focus"
    intervals_advised: int
    avg_rec_regret: float | None
    confidence_accuracy: float | None  # when said "high", % correct direction
    active_traders: int
    learning_generation: int        # how many adaptation loops completed

class StatsResponse:
    name: str
    type: str
    url: str
    profit: float
    roi: float
    total_volume: float             # MWh dispatched (trader) or cleared (market)
    seed_balance: float
    active_intervals: int
```

---

## Communication Protocol

### No cryptography
This is a cooperative simulation. No commit-reveal, no seed hashing, no fairness verification. The Market posts `cleared_price` directly at dispatch time. All agents trust the simulation.

### Agent registration
```
POST /register  {url, name, type}
→ stored in registry with last_heartbeat timestamp
→ evicted after 180s without heartbeat
```

### Interval lifecycle (Market-driven)
```
scheduler creates interval → status: "scheduled"
bid_open_at reached        → status: "open"   (appears in Market AgentCard)
bid_close_at reached       → status: "closed" (no new bids accepted)
dispatch_at reached        → Market generates cleared_price
                           → status: "dispatched"
                           → GET /intervals/{id} returns cleared_price
                           → GET /bids/{bid_id} returns DispatchResult
```

### Bid flow (Trader → Market)
```
Trader: POST /bids  {interval_id, direction, volume_mw, limit_price, trader_id}
Market: returns {bid_id, interval_id, status: "accepted"}

(async — Trader polls)
Trader: GET /bids/{bid_id}
Market: returns DispatchResult once dispatched, or {status: "pending"}
```

### Recommendation flow (Trader → Energy agent)
```
Trader: GET /recommend?interval_id=X&battery_state=<json>
Energy: runs investigation pipeline (max 4 tool calls, ~20-30s)
      → narrator: structured output → Recommendation
      → hard gate: all evidence_tool_calls grounded?
        fail → return direction="none"
      → judge: scores quality, sets confidence
Energy: returns Recommendation

Trader: decides to follow, scale down, or ignore
Trader: POST /bids to Market
```

### Learning feedback (Energy agent → Market, self-directed)
```
Energy agent result_ingestion_loop (every 30s):
  for each pending recommendation interval_id:
    GET /intervals/{id} from Market
    if dispatched:
      compute rec_regret
      update LearningLedger
      remove from pending set
```

---

## Background Loops

### Market
| Loop | Interval | Responsibility |
|---|---|---|
| `heartbeat_loop` | 60s | Re-register with registry |
| `price_generation_loop` | runs ahead | Pre-generate next N intervals with indicative prices via LLM brain |
| `scheduler_loop` | 5s | Open/close/dispatch intervals on schedule |

### Trader
| Loop | Interval | Responsibility |
|---|---|---|
| `heartbeat_loop` | 60s | Re-register with registry |
| `discovery_loop` | 60s | Fetch all markets + energy agents from registry |
| `bidding_loop` | 30s | For each open interval not yet bid: get recommendation → decide → bid |
| `settlement_loop` | 10s | Poll pending bid results, update battery SOC + ledger |

### Energy Agent
| Loop | Interval | Responsibility |
|---|---|---|
| `heartbeat_loop` | 60s | Re-register with registry |
| `result_ingestion_loop` | 30s | Fetch cleared prices from Market for pending recommendations, compute rec_regret |
| `adaptation_loop` | every 100 intervals | Distill LearningLedger into strategy_context.md |
| `tool_synthesis_loop` | every 200 intervals | Find unexplained regret clusters, generate + backtest new tools |

### Observer
| Loop | Interval | Responsibility |
|---|---|---|
| `poll_loop` | 30s | Fetch stats + AgentCards from all agents, update cache |

---

## Energy Agent Learning Architecture

### Layer 1 — LearningLedger (ground truth accumulation)
Every recommendation is stored as a `RecommendationRecord`. When the Market dispatches the interval, `result_ingestion_loop` fills in `cleared_price`, computes `rec_regret`, and marks the record complete.

`rec_regret = perfect_foresight_revenue − revenue_if_recommendation_followed`

This separates recommendation quality from trader compliance — the Energy agent is scored on its own advice, not on whether the Trader listened.

### Layer 2 — Strategy context (distilled lessons)
A background `adaptation_loop` runs every 100 completed recommendations:
1. Query LearningLedger for high-regret clusters
2. Run existing analysis tools (gap.py, slices.py, diagnostic.py) against the ledger records
3. Identify systematic patterns: "mornings with SOC > 80% — I consistently over-recommend discharge volume"
4. Rewrite `strategy_context.md` with compact learned rules
5. Increment `learning_generation` counter

The Brain reads `strategy_context.md` into its system context on every recommendation call.

### Layer 3 — Retrieval (few-shot from history)
A `find_similar_intervals` tool is always available to the Brain:
```
Input: current_soc_pct, hour_of_day, price_trend_slope, market_type
Output: top-10 past RecommendationRecords by feature similarity
        each record shows: what was recommended, cleared_price, rec_regret
```
Before recommending, the Brain calls this tool and gets real examples of what worked and what didn't in similar conditions.

### Layer 4 — Tool synthesis (expanding analytical capability)
`tool_synthesis_loop` runs every 200 intervals:
1. Find the largest unexplained regret cluster (high regret, no existing tool covers the pattern)
2. Ask the Brain to write a new Python analysis function targeting that pattern
3. Validate the new tool executes without error against the cluster data
4. If it passes: write to `energy/tools/generated/{name}.py`, register in tool registry
5. Log the synthesis event with before/after regret estimate

The judge and hard gate prevent synthesised tools from inflating scores — every recommendation still requires grounded evidence.

### Quality gates on recommendations
| Gate | Type | Action on failure |
|---|---|---|
| Hard gate | Mechanical: evidence_tool_calls grounded? | Return `direction="none"` — don't risk ungrounded bid |
| Soft gate (judge) | LLM judge on different model | Downgrade `confidence` to "low" — Trader brain scales volume down |
| Deadline guard | Time check: bid window closing? | Return `direction="none"` if < `min_recommendation_seconds` remaining |

---

## Multi-Energy-Agent Competition

Multiple Energy agent instances register as `type="energy"` in the Registry. Each exposes an `EnergyAgentCard` with self-reported performance metrics.

### Trader selection
Trader's discovery loop fetches all Energy agents. Trader ledger tracks a `PerformanceRecord` per agent URL:
```python
PerformanceRecord:
    agent_url: str
    intervals_advised: int
    avg_rec_regret: float     # rolling, Trader-computed from own outcomes
    confidence_accuracy: float
    last_queried_at: datetime
```
Trader brain method `select_energy_agent(available, performance, battery_state)` chooses which to query. Default: stick with current unless rolling regret exceeds threshold for 10 consecutive intervals, then switch to best available.

### Observer cross-validation
Energy agents self-report `avg_rec_regret` in their AgentCard. The Observer tracks actual trader outcomes per advisor and flags discrepancies:
```
/rankings/energy
→ sorted by avg_rec_regret (lower = better)
→ columns: name, intervals_advised, self_reported_regret, trader_observed_regret, delta, learning_generation, active_traders
→ delta > threshold → flagged
```

### Competitive dynamics
- Lower rec_regret → more Traders follow → more LearningLedger data → faster improvement (network effect)
- Specialisation emerges: an Energy agent that focuses on FCAS develops a niche
- Different people can host instances with different Brain implementations (different models, strategies, tool inventories)
- Gaming resistance: rec_regret is computed from Market's public cleared prices, not self-reported

---

## Key Invariants

- `shared/` has no imports from market/trader/energy/observer — it is the foundation
- Battery SOC constraints in `battery.py` are hard limits, not LLM suggestions
- Hard gate failure always returns `direction="none"` — never send an ungrounded bid recommendation
- `result_ingestion_loop` on the Energy agent pulls from Market directly — does not require Trader feedback
- `Brain` is the only thing that should be subclassed per agent — all other components are plumbing
- Simulation time is configurable via `time_multiplier` in market config (1.0 = real-time 5-min intervals, 5.0 = 60s intervals)
- Observer cross-validates Energy agent self-reported metrics — trust but verify
- No cryptographic primitives anywhere — this is a cooperative simulation
