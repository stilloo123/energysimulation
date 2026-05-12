from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from energy.analysis.diagnostic import forecast_error_diagnostic
from energy.analysis.gap import compute_regret_summary
from energy.analysis.inspector import inspect_intervals
from energy.analysis.slices import slice_by_hour, slice_by_price_bucket, slice_by_soc_regime

# Anthropic tool definitions (input_schema follows JSON Schema)
TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "compute_regret_summary",
        "description": (
            "Compute an overall summary of recommendation quality across all past intervals. "
            "Returns total recommendations, avg regret, total revenue gap vs perfect foresight."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "slice_by_hour",
        "description": (
            "Break down recommendation regret and cleared prices by hour of day (0–23). "
            "Use this to identify which hours are most/least profitable for discharge or charge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "slice_by_price_bucket",
        "description": (
            "Break down regret by price quartile (Q1=low, Q4=high). "
            "Shows whether the agent over- or under-bids at different price levels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "slice_by_soc_regime",
        "description": (
            "Break down regret by battery SOC regime: low (<30%), mid (30–70%), high (>70%). "
            "Identifies if SOC level affects recommendation quality."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "forecast_error_diagnostic",
        "description": (
            "Find the intervals where the recommended limit_price diverged most from the actual "
            "cleared price. High error = poor price forecasting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "Number of worst-error intervals to return (default 20)",
                    "default": 20,
                }
            },
            "required": [],
        },
    },
    {
        "name": "inspect_intervals",
        "description": (
            "Drill into raw interval history with optional filters. "
            "Use to examine specific conditions (e.g. evening hours with high SOC)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hour_min": {"type": "integer", "description": "Filter: hour >= this value"},
                "hour_max": {"type": "integer", "description": "Filter: hour <= this value"},
                "price_min": {"type": "number", "description": "Filter: cleared_price >= this"},
                "price_max": {"type": "number", "description": "Filter: cleared_price <= this"},
                "soc_min": {"type": "number", "description": "Filter: soc_pct >= this (0–1)"},
                "soc_max": {"type": "number", "description": "Filter: soc_pct <= this (0–1)"},
                "direction": {
                    "type": "string",
                    "enum": ["charge", "discharge", "none"],
                    "description": "Filter by recommended direction",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50)",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
    {
        "name": "find_similar_intervals",
        "description": (
            "Find past intervals with similar conditions (SOC, hour, price trend, market type) "
            "and show their outcomes. Useful for few-shot context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "soc_pct": {"type": "number", "description": "Current SOC percentage (0–1)"},
                "hour_of_day": {"type": "integer", "description": "Current hour (0–23)"},
                "price_trend_slope": {
                    "type": "number",
                    "description": "Recent price trend (positive = rising)",
                },
                "market_type": {
                    "type": "string",
                    "enum": ["energy", "fcas_raise", "fcas_lower"],
                },
            },
            "required": ["soc_pct", "hour_of_day", "price_trend_slope", "market_type"],
        },
    },
    {
        "name": "get_strategy_context",
        "description": (
            "Return the current learned strategy rules accumulated from past adaptation loops. "
            "Empty if no adaptation has run yet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def execute_tool(name: str, args: dict, interval_history: list[dict], ledger=None) -> dict:
    """Execute a named tool against the current interval history."""
    if name == "compute_regret_summary":
        return compute_regret_summary(interval_history).model_dump()

    elif name == "slice_by_hour":
        return slice_by_hour(interval_history).model_dump()

    elif name == "slice_by_price_bucket":
        return slice_by_price_bucket(interval_history).model_dump()

    elif name == "slice_by_soc_regime":
        return slice_by_soc_regime(interval_history).model_dump()

    elif name == "forecast_error_diagnostic":
        top_n = int(args.get("top_n", 20))
        return forecast_error_diagnostic(interval_history, top_n=top_n).model_dump()

    elif name == "inspect_intervals":
        return inspect_intervals(
            interval_history,
            hour_min=args.get("hour_min"),
            hour_max=args.get("hour_max"),
            price_min=args.get("price_min"),
            price_max=args.get("price_max"),
            soc_min=args.get("soc_min"),
            soc_max=args.get("soc_max"),
            direction=args.get("direction"),
            limit=int(args.get("limit", 50)),
        ).model_dump()

    elif name == "find_similar_intervals":
        if ledger is not None:
            from energy.retrieval import execute_retrieval
            return execute_retrieval(ledger, args)
        return {
            "n_found": 0,
            "examples": [],
            "provenance": {"method": "find_similar_intervals", "n_rows": 0, "version": "0.1.0"},
        }

    elif name == "get_strategy_context":
        path = Path("energy/strategy_context.md")
        content = path.read_text() if path.exists() else ""
        return {
            "strategy_context": content,
            "has_content": bool(content),
            "provenance": {"method": "get_strategy_context", "n_rows": 1, "version": "0.1.0"},
        }

    else:
        return {"error": f"unknown tool: {name}"}


class ToolRegistry:
    """Stateful registry that manages base tools + dynamically synthesised tools."""

    def __init__(self, ledger=None):
        self._definitions: list[dict] = list(TOOL_DEFINITIONS)
        self._generated: dict[str, object] = {}  # name → execute callable
        self._ledger = ledger
        self._load_generated()

    @property
    def definitions(self) -> list[dict]:
        return self._definitions

    def tool_names(self) -> list[str]:
        return [d["name"] for d in self._definitions]

    def execute(self, name: str, args: dict, interval_history: list[dict]) -> dict:
        if name in self._generated:
            try:
                return self._generated[name](args, interval_history)
            except Exception as exc:
                return {"error": str(exc), "tool": name}
        return execute_tool(name, args, interval_history, self._ledger)

    def register_from_file(self, path: str) -> bool:
        try:
            import uuid as _uuid
            spec = importlib.util.spec_from_file_location(
                f"gen_tool_{_uuid.uuid4().hex}", path
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            definition: dict = getattr(mod, "TOOL_DEFINITION")
            executor = getattr(mod, "execute")
            name = definition["name"]
            self._definitions = [d for d in self._definitions if d["name"] != name]
            self._definitions.append(definition)
            self._generated[name] = executor
            logger.info("Registered synthesised tool: %s", name)
            return True
        except Exception as exc:
            logger.warning("Failed to register tool from %s: %s", path, exc)
            return False

    def _load_generated(self) -> None:
        gen_dir = Path("energy/tools/generated")
        if not gen_dir.exists():
            return
        for py_file in sorted(gen_dir.glob("*.py")):
            if py_file.stem.startswith("_"):
                continue
            self.register_from_file(str(py_file))
