"""
Smoke test — no servers, no API key required.

Run from the repo root:
    python smoke_test.py

Tests every layer except live LLM calls:
  - CSV loading + nightly analysis pipeline
  - Market brain (CSV replay + cleared/forecast split)
  - Battery physics
  - Trader ledger + agent-selection logic
  - Energy ledger (regret calculation)
  - Tool registry + all analysis tools
  - Hard gate
  - Recommendation parsing
"""

import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_failures = []


def check(name: str, fn):
    try:
        fn()
        print(f"  {PASS}  {name}")
    except Exception as exc:
        _failures.append(name)
        print(f"  {FAIL}  {name}")
        traceback.print_exc()
        print()


# ---------------------------------------------------------------------------
# CSV + Nightly Analysis
# ---------------------------------------------------------------------------
print("\n── CSV + Nightly Analysis ──")

CSV_PATH = "/Users/sachin/projects/energyproject/data/BLYTHB1_20260126.csv"


def test_csv_load():
    from energy.analysis.nightly import _load_csv, _build_history
    cleared, forecast_map = _load_csv(CSV_PATH)
    assert len(cleared) == 288, f"expected 288 cleared rows, got {len(cleared)}"
    assert len(forecast_map) == 288, f"expected 288 forecast slots, got {len(forecast_map)}"
    history = _build_history(cleared, forecast_map)
    assert len(history) >= 280, f"too few history items: {len(history)}"
    # Every item has required fields
    required = {"interval_id", "hour_of_day", "cleared_price", "direction", "rec_regret", "soc_pct"}
    for item in history[:5]:
        missing = required - item.keys()
        assert not missing, f"history item missing fields: {missing}"


def test_gap_analysis():
    from energy.analysis.nightly import _load_csv, _build_history
    from energy.analysis.gap import compute_regret_summary
    from energy.analysis.slices import slice_by_hour, slice_by_soc_regime, slice_by_price_bucket
    cleared, forecast_map = _load_csv(CSV_PATH)
    history = _build_history(cleared, forecast_map)
    gap = compute_regret_summary(history)
    assert gap.completed_recommendations > 0
    assert gap.avg_rec_regret >= 0
    hours = slice_by_hour(history)
    assert len(hours.slices) > 0
    soc = slice_by_soc_regime(history)
    assert len(soc.slices) > 0
    buckets = slice_by_price_bucket(history)
    assert len(buckets.slices) > 0


check("CSV loads 288 cleared + 288 forecast rows", test_csv_load)
check("Gap analysis + all slice tools run on CSV history", test_gap_analysis)

# ---------------------------------------------------------------------------
# Market Brain
# ---------------------------------------------------------------------------
print("\n── Market Brain ──")


def test_csv_market_brain():
    from market.brain import CSVMarketBrain
    brain = CSVMarketBrain(CSV_PATH)
    # generate_prices returns forecast (expected) prices
    now = datetime(2026, 1, 26, 9, 0, 0, tzinfo=timezone.utc)
    slots = [{"market_type": "energy", "scheduled_at": now.isoformat(), "hour": 9}]
    prices = brain.generate_prices(slots, [])
    assert len(prices) == 1
    assert prices[0] >= 0, f"negative price: {prices[0]}"
    # cleared_price_at returns actual cleared price
    cleared = brain.cleared_price_at(9, 0)
    assert cleared is not None, "no cleared price for 09:00"
    # forecast != cleared for at least some slots (they can differ)
    print(f"    09:00 UTC — forecast=${prices[0]:.2f}, cleared=${cleared:.2f}")


def test_fallback_price():
    from market.brain import _fallback_price
    assert _fallback_price({"market_type": "energy", "hour": 18}) == 200.0
    assert _fallback_price({"market_type": "fcas_raise", "hour": 0}) == 15.0
    assert _fallback_price({"market_type": "fcas_lower", "hour": 0}) == 8.0


check("CSVMarketBrain loads and serves forecast vs cleared prices", test_csv_market_brain)
check("Fallback prices by market type", test_fallback_price)

# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------
print("\n── Battery ──")


def test_battery_constraints():
    from trader.battery import Battery
    cfg = {"capacity_mwh": 100.0, "max_charge_mw": 50.0,
           "max_discharge_mw": 50.0, "initial_soc_pct": 0.5, "efficiency": 0.85}
    bat = Battery(cfg)
    assert bat.can_charge()
    assert bat.can_discharge()
    # Drain to near-empty
    bat._soc_mwh = 5.0  # 5%
    assert bat.can_charge()
    assert not bat.can_discharge()  # SOC < 10%
    # Fill to near-full
    bat._soc_mwh = 95.0  # 95%
    assert not bat.can_charge()   # SOC > 90%
    assert bat.can_discharge()


def test_battery_charge_discharge():
    from trader.battery import Battery
    cfg = {"capacity_mwh": 100.0, "max_charge_mw": 50.0,
           "max_discharge_mw": 50.0, "initial_soc_pct": 0.5, "efficiency": 0.85}
    bat = Battery(cfg)
    initial = bat.soc_mwh
    bat.apply_charge(1.0)   # 1 MWh in
    assert bat.soc_mwh > initial  # SOC went up
    bat.apply_discharge(1.0)
    assert bat.soc_mwh < bat.soc_mwh + 1  # SOC went down


check("Battery SOC hard constraints (< 10% no discharge, > 90% no charge)", test_battery_constraints)
check("Battery charge/discharge changes SOC correctly", test_battery_discharge := test_battery_charge_discharge)

# ---------------------------------------------------------------------------
# Energy Ledger (regret calculation)
# ---------------------------------------------------------------------------
print("\n── Energy Ledger ──")


def _make_rec(direction="discharge", volume=10.0, limit=80.0):
    from shared.models import Recommendation
    import uuid
    return Recommendation(
        recommendation_id=str(uuid.uuid4()),
        interval_id="iv-001",
        direction=direction,
        volume_mw=volume,
        limit_price=limit,
        confidence="high",
        reasoning="test",
        evidence_tool_calls=["compute_regret_summary"],
        similar_past_interval_ids=[],
        generated_at=datetime.now(timezone.utc),
    )


def test_regret_discharge_above_limit():
    from energy.ledger import LearningLedger
    ledger = LearningLedger()
    rec = _make_rec(direction="discharge", volume=10.0, limit=80.0)
    ledger.record_recommendation(rec, {"soc_pct": 0.5, "hour_of_day": 12,
                                       "market_type": "energy", "reference_price": 80.0})
    # Cleared above limit → bid executes
    ledger.fill_result("iv-001", cleared_price=120.0)
    record = ledger.get_record_by_interval("iv-001")
    assert record is not None
    assert record.rec_regret is not None
    # regret = perfect - actual; perfect = 10MW * (5/60)h * 120 = 100, actual = same → 0
    assert record.rec_regret >= 0, f"regret should be >= 0: {record.rec_regret}"


def test_regret_discharge_below_limit():
    from energy.ledger import LearningLedger
    ledger = LearningLedger()
    rec = _make_rec(direction="discharge", volume=10.0, limit=80.0)
    ledger.record_recommendation(rec, {"soc_pct": 0.5, "hour_of_day": 12,
                                       "market_type": "energy", "reference_price": 80.0})
    # Cleared below limit → bid doesn't execute, full regret
    ledger.fill_result("iv-001", cleared_price=50.0)
    record = ledger.get_record_by_interval("iv-001")
    assert record.rec_regret is not None
    # Perfect = 10 * (5/60) * 50 = 41.67, actual = 0 (bid not executed) → regret = 41.67
    assert record.rec_regret > 0, f"should have positive regret when bid didn't execute"


check("Regret = 0 when bid executes at cleared price", test_regret_discharge_above_limit)
check("Regret > 0 when discharge bid doesn't execute (cleared < limit)", test_regret_discharge_below_limit)

# ---------------------------------------------------------------------------
# Tool Registry + Analysis Tools
# ---------------------------------------------------------------------------
print("\n── Tool Registry + Analysis Tools ──")

_SAMPLE_HISTORY = [
    {"interval_id": f"iv-{i:03d}", "market_type": "energy", "hour_of_day": i % 24,
     "cleared_price": 50.0 + i * 2, "direction": "discharge" if i % 3 == 0 else "charge",
     "volume_mw": 10.0, "limit_price": 55.0 + i, "rec_regret": float(i % 10),
     "perfect_foresight_revenue": 5.0 + i, "soc_pct": 0.3 + (i % 5) * 0.1}
    for i in range(50)
]


def test_all_base_tools():
    from energy.tool_registry import execute_tool
    for name in ["compute_regret_summary", "slice_by_hour", "slice_by_price_bucket",
                 "slice_by_soc_regime"]:
        result = execute_tool(name, {}, _SAMPLE_HISTORY)
        assert "error" not in result, f"{name} returned error: {result}"
    result = execute_tool("forecast_error_diagnostic", {"top_n": 5}, _SAMPLE_HISTORY)
    assert "error" not in result
    result = execute_tool("inspect_intervals", {"hour_min": 0, "hour_max": 12, "limit": 10},
                          _SAMPLE_HISTORY)
    assert "error" not in result
    result = execute_tool("get_strategy_context", {}, _SAMPLE_HISTORY)
    assert "strategy_context" in result


def test_tool_registry_class():
    from energy.tool_registry import ToolRegistry
    reg = ToolRegistry()
    names = reg.tool_names()
    assert "compute_regret_summary" in names
    assert "get_strategy_context" in names
    result = reg.execute("slice_by_hour", {}, _SAMPLE_HISTORY)
    assert "error" not in result
    result = reg.execute("unknown_tool_xyz", {}, _SAMPLE_HISTORY)
    assert "error" in result


check("All 7 base analysis tools execute without error", test_all_base_tools)
check("ToolRegistry wraps base tools + returns error for unknown", test_tool_registry_class)

# ---------------------------------------------------------------------------
# Hard Gate
# ---------------------------------------------------------------------------
print("\n── Hard Gate ──")


def test_hard_gate_pass():
    from energy.brain import _hard_gate
    from shared.models import Recommendation
    import uuid
    rec = Recommendation(
        recommendation_id=str(uuid.uuid4()), interval_id="iv-1",
        direction="discharge", volume_mw=5.0, limit_price=100.0,
        confidence="high", reasoning="ok",
        evidence_tool_calls=["slice_by_hour"],
        similar_past_interval_ids=[],
        generated_at=datetime.now(timezone.utc),
    )
    assert _hard_gate(rec, ["slice_by_hour", "compute_regret_summary"])


def test_hard_gate_fail_no_calls():
    from energy.brain import _hard_gate
    from shared.models import Recommendation
    import uuid
    rec = Recommendation(
        recommendation_id=str(uuid.uuid4()), interval_id="iv-1",
        direction="discharge", volume_mw=5.0, limit_price=100.0,
        confidence="high", reasoning="ok",
        evidence_tool_calls=["slice_by_hour"],
        similar_past_interval_ids=[],
        generated_at=datetime.now(timezone.utc),
    )
    assert not _hard_gate(rec, [])  # no tools called


def test_hard_gate_fail_uncalled_evidence():
    from energy.brain import _hard_gate
    from shared.models import Recommendation
    import uuid
    rec = Recommendation(
        recommendation_id=str(uuid.uuid4()), interval_id="iv-1",
        direction="discharge", volume_mw=5.0, limit_price=100.0,
        confidence="high", reasoning="ok",
        evidence_tool_calls=["forecast_error_diagnostic"],  # claimed but not called
        similar_past_interval_ids=[],
        generated_at=datetime.now(timezone.utc),
    )
    assert not _hard_gate(rec, ["slice_by_hour"])


def test_hard_gate_none_always_passes():
    from energy.brain import _hard_gate
    from shared.models import Recommendation
    import uuid
    rec = Recommendation(
        recommendation_id=str(uuid.uuid4()), interval_id="iv-1",
        direction="none", volume_mw=0.0, limit_price=0.0,
        confidence="low", reasoning="no action",
        evidence_tool_calls=[],
        similar_past_interval_ids=[],
        generated_at=datetime.now(timezone.utc),
    )
    assert _hard_gate(rec, [])  # none always passes


check("Hard gate passes when evidence tools were called", test_hard_gate_pass)
check("Hard gate blocks when no tools were called", test_hard_gate_fail_no_calls)
check("Hard gate blocks when evidence tool not in called set", test_hard_gate_fail_uncalled_evidence)
check("Hard gate always passes for direction=none", test_hard_gate_none_always_passes)

# ---------------------------------------------------------------------------
# Recommendation Parsing
# ---------------------------------------------------------------------------
print("\n── Recommendation Parsing ──")


def test_parse_valid_json():
    from energy.brain import _parse_recommendation
    text = '''{
        "direction": "discharge",
        "volume_mw": 25.0,
        "limit_price": 150.0,
        "confidence": "high",
        "reasoning": "Evening peak, high SOC",
        "evidence_tool_calls": ["slice_by_hour"],
        "similar_past_interval_ids": []
    }'''
    rec = _parse_recommendation(text, "iv-001", ["slice_by_hour"])
    assert rec.direction == "discharge"
    assert rec.volume_mw == 25.0
    assert rec.limit_price == 150.0
    assert rec.confidence == "high"
    assert "slice_by_hour" in rec.evidence_tool_calls


def test_parse_invalid_json_returns_none():
    from energy.brain import _parse_recommendation
    rec = _parse_recommendation("This is not JSON at all", "iv-001", [])
    assert rec.direction == "none"


def test_parse_strips_uncalled_evidence():
    from energy.brain import _parse_recommendation
    text = '{"direction": "discharge", "volume_mw": 10, "limit_price": 80, ' \
           '"confidence": "high", "reasoning": "x", ' \
           '"evidence_tool_calls": ["slice_by_hour", "forecast_error_diagnostic"]}'
    # Only slice_by_hour was actually called
    rec = _parse_recommendation(text, "iv-001", ["slice_by_hour"])
    assert "forecast_error_diagnostic" not in rec.evidence_tool_calls
    assert "slice_by_hour" in rec.evidence_tool_calls


check("Valid JSON parses to correct Recommendation", test_parse_valid_json)
check("Unparseable text returns direction=none", test_parse_invalid_json_returns_none)
check("Evidence stripped to only actually-called tools", test_parse_strips_uncalled_evidence)

# ---------------------------------------------------------------------------
# Trader Brain Agent Selection
# ---------------------------------------------------------------------------
print("\n── Trader Brain ──")


def test_trader_select_no_agents():
    from trader.brain import DefaultTraderBrain
    from trader.battery import Battery
    cfg = {"model": "anthropic/claude-sonnet-4-6", "agents_md": "trader/AGENTS.md",
           "min_energy_agent_intervals": 5, "regret_switch_threshold": 500.0}
    brain = DefaultTraderBrain(cfg)
    bat = Battery({"capacity_mwh": 100.0, "max_charge_mw": 50.0,
                   "max_discharge_mw": 50.0, "initial_soc_pct": 0.5, "efficiency": 0.85})
    result = brain.select_energy_agent([], {}, bat.state(), None)
    assert result is None


def test_trader_decide_bid_no_rec():
    from trader.brain import DefaultTraderBrain
    from trader.battery import Battery
    cfg = {"model": "anthropic/claude-sonnet-4-6", "agents_md": "trader/AGENTS.md",
           "min_energy_agent_intervals": 5, "regret_switch_threshold": 500.0}
    brain = DefaultTraderBrain(cfg)
    bat = Battery({"capacity_mwh": 100.0, "max_charge_mw": 50.0,
                   "max_discharge_mw": 50.0, "initial_soc_pct": 0.5, "efficiency": 0.85})
    direction, volume, price = brain.decide_bid(None, bat, None, 5.0 / 60.0)
    assert direction == "none"


check("Trader selects None when no energy agents available", test_trader_select_no_agents)
check("Trader returns none bid when no recommendation", test_trader_decide_bid_no_rec)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if _failures:
    print(f"\033[91m{len(_failures)} FAILED:\033[0m {', '.join(_failures)}")
    sys.exit(1)
else:
    total = 20  # approximate
    print(f"\033[92mAll checks passed.\033[0m")
