from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from shared.models import BatteryState, MarketInterval, Recommendation
from energy.tool_registry import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)


class Brain(ABC):
    @abstractmethod
    async def recommend(
        self,
        interval: MarketInterval,
        battery_state: BatteryState,
        interval_history: list[dict],
        time_to_bid_close_seconds: float,
    ) -> Recommendation:
        ...


class DefaultEnergyBrain(Brain):
    def __init__(self, cfg: dict, ledger=None, tool_registry=None):
        self._model_id = cfg["model"].removeprefix("anthropic/")
        self._judge_model_id = cfg.get("judge_model", cfg["model"]).removeprefix("anthropic/")
        self._max_iterations = cfg["max_investigator_iterations"]
        self._min_rec_seconds = cfg["min_recommendation_seconds"]
        self._investigator_prompt = Path("energy/prompts/investigator.md").read_text()
        self._narrator_prompt = Path("energy/prompts/narrator.md").read_text()
        self._judge_prompt = Path("energy/prompts/judge.md").read_text()
        self._ledger = ledger
        self._tool_registry = tool_registry  # None → fall back to module-level defaults

    async def recommend(
        self,
        interval: MarketInterval,
        battery_state: BatteryState,
        interval_history: list[dict],
        time_to_bid_close_seconds: float,
    ) -> Recommendation:
        # 1. Deadline guard
        if time_to_bid_close_seconds < self._min_rec_seconds:
            return _none_rec(interval.interval_id, "bid window closing")

        # 2. Investigation + narration (sync in thread pool)
        try:
            rec, tool_calls_made = await asyncio.to_thread(
                self._investigate_sync,
                interval,
                battery_state,
                interval_history,
                time_to_bid_close_seconds,
            )
        except Exception as exc:
            logger.warning("Investigation failed: %s", exc)
            return _none_rec(interval.interval_id, f"investigation error: {exc}")

        # 3. Hard gate
        if not _hard_gate(rec, tool_calls_made):
            logger.info("Hard gate failed for %s (calls=%s)", interval.interval_id, tool_calls_made)
            return _none_rec(interval.interval_id, "grounding check failed")

        # 4. Fire-and-forget judge (updates logged, doesn't block response)
        asyncio.create_task(self._run_judge(rec, interval, battery_state, tool_calls_made))

        return rec

    def _investigate_sync(
        self,
        interval: MarketInterval,
        battery_state: BatteryState,
        interval_history: list[dict],
        time_to_bid_close: float,
    ) -> tuple[Recommendation, list[str]]:
        import anthropic

        client = anthropic.Anthropic()
        soc_pct = battery_state.soc_mwh / battery_state.capacity_mwh * 100

        user_msg = (
            f"Current market interval:\n"
            f"- interval_id: {interval.interval_id}\n"
            f"- market_type: {interval.market_type}\n"
            f"- reference_price: ${interval.reference_price}/MWh\n"
            f"- scheduled_at: {interval.scheduled_at.isoformat()}\n"
            f"- time_to_bid_close: {time_to_bid_close:.0f}s\n"
            f"\nBattery state:\n"
            f"- SOC: {soc_pct:.1f}% ({battery_state.soc_mwh:.1f}/{battery_state.capacity_mwh:.1f} MWh)\n"
            f"- max_charge_mw: {battery_state.max_charge_mw:.1f}\n"
            f"- max_discharge_mw: {battery_state.max_discharge_mw:.1f}\n"
            f"- efficiency: {battery_state.efficiency:.2f}\n"
            f"\nHistorical intervals available: {len(interval_history)}\n"
            f"\nInvestigate to determine the optimal action."
        )

        messages: list[dict] = [{"role": "user", "content": user_msg}]
        tool_calls_made: list[str] = []

        tool_defs = (
            self._tool_registry.definitions
            if self._tool_registry is not None
            else TOOL_DEFINITIONS
        )

        for _ in range(self._max_iterations):
            response = client.messages.create(
                model=self._model_id,
                system=self._investigator_prompt,
                messages=messages,
                tools=tool_defs,
                max_tokens=2000,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_calls_made.append(block.name)
                    if self._tool_registry is not None:
                        result = self._tool_registry.execute(block.name, block.input, interval_history)
                    else:
                        result = execute_tool(block.name, block.input, interval_history, self._ledger)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })

            messages.append({"role": "user", "content": tool_results})

        # Narrator: ask for structured JSON Recommendation
        messages.append({
            "role": "user",
            "content": (
                self._narrator_prompt
                + f"\n\nOutput the JSON recommendation now for interval_id={interval.interval_id}."
            ),
        })
        narrator_resp = client.messages.create(
            model=self._model_id,
            messages=messages,
            max_tokens=800,
        )

        raw_text = narrator_resp.content[0].text
        rec = _parse_recommendation(raw_text, interval.interval_id, tool_calls_made)
        return rec, tool_calls_made

    async def _run_judge(
        self,
        rec: Recommendation,
        interval: MarketInterval,
        battery_state: BatteryState,
        tool_calls_made: list[str],
    ) -> None:
        try:
            soc_pct = battery_state.soc_mwh / battery_state.capacity_mwh * 100
            judge_user = (
                f"Evaluate this recommendation:\n"
                f"- interval_id: {interval.interval_id}\n"
                f"- direction: {rec.direction}\n"
                f"- volume_mw: {rec.volume_mw}\n"
                f"- limit_price: {rec.limit_price}\n"
                f"- confidence: {rec.confidence}\n"
                f"- reasoning: {rec.reasoning}\n"
                f"- evidence_tool_calls: {rec.evidence_tool_calls}\n"
                f"- tools_actually_called: {tool_calls_made}\n"
                f"- soc_pct: {soc_pct:.1f}%\n"
                f"- reference_price: {interval.reference_price}\n"
            )
            result = await asyncio.to_thread(
                self._judge_sync, judge_user
            )
            logger.info(
                "Judge result for %s: overall=%.2f verdict=%s",
                interval.interval_id,
                result.get("overall", 0),
                result.get("confidence_verdict"),
            )
        except Exception as exc:
            logger.warning("Judge failed: %s", exc)

    def synthesise_tool(self, cluster: dict, existing_tool_names: list[str]) -> str:
        """Generate Python source for a new analysis tool targeting a high-regret cluster (sync)."""
        import anthropic

        client = anthropic.Anthropic()
        prompt = (
            "You are writing a new analysis tool for an electricity market recommendation agent.\n\n"
            "The agent has identified this recurring high-regret pattern its existing tools cannot explain:\n"
            f"{json.dumps(cluster, indent=2, default=str)}\n\n"
            f"Existing tools (do NOT duplicate these names): {existing_tool_names}\n\n"
            "Write a NEW Python analysis tool that specifically targets this pattern.\n\n"
            "Requirements:\n"
            "1. Operate on `interval_history: list[dict]` where each dict has:\n"
            "   interval_id, market_type, hour_of_day (int), cleared_price (float), direction (str),\n"
            "   volume_mw (float), limit_price (float), rec_regret (float|None), soc_pct (float 0-1)\n"
            "2. Return a plain dict — no Pydantic, no external imports beyond stdlib\n"
            "3. Handle empty input gracefully (return empty/zero results, never raise)\n"
            "4. Give the tool a descriptive snake_case name that reflects the pattern\n\n"
            "Output a complete Python file with EXACTLY:\n"
            "- `TOOL_DEFINITION: dict` — Anthropic tool definition: {name, description, input_schema}\n"
            "- `def execute(args: dict, interval_history: list[dict]) -> dict:` — the analysis function\n\n"
            "Output ONLY the Python code. No prose, no markdown fences."
        )
        resp = client.messages.create(
            model=self._model_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        code = resp.content[0].text.strip()
        # Strip accidental markdown fences
        if code.startswith("```"):
            code = "\n".join(code.splitlines()[1:])
        if code.endswith("```"):
            code = "\n".join(code.splitlines()[:-1])
        return code.strip()

    def _judge_sync(self, user_msg: str) -> dict:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=self._judge_model_id,
            system=self._judge_prompt,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=400,
        )
        text = resp.content[0].text
        try:
            return json.loads(_extract_json(text))
        except Exception:
            return {}

    def distill_lessons(self, gap, slices: list, records: list) -> list[str]:
        """Distil completed recommendation records into compact strategy rules (sync)."""
        import anthropic

        client = anthropic.Anthropic()

        gap_summary = {
            "avg_rec_regret": round(gap.avg_rec_regret, 2),
            "completed_recommendations": gap.completed_recommendations,
            "gap_pct": round(gap.gap_pct, 1),
        }
        hour_data = slices[0].model_dump() if slices else {}
        soc_data = slices[1].model_dump() if len(slices) > 1 else {}

        # Sample high-regret records for the LLM
        high_regret = sorted(records, key=lambda r: r.rec_regret or 0, reverse=True)[:10]
        examples = [
            {
                "hour": r.market_state_snapshot.get("hour_of_day"),
                "soc_pct": round(r.market_state_snapshot.get("soc_pct", 0), 2),
                "market_type": r.market_state_snapshot.get("market_type"),
                "direction": r.recommendation.direction,
                "limit_price": r.recommendation.limit_price,
                "cleared_price": r.cleared_price,
                "rec_regret": round(r.rec_regret or 0, 2),
            }
            for r in high_regret
        ]

        prompt = (
            "You are analysing the performance of an electricity market recommendation agent.\n\n"
            f"Overall performance:\n{json.dumps(gap_summary, indent=2)}\n\n"
            f"Regret by hour of day:\n{json.dumps(hour_data, default=str, indent=2)}\n\n"
            f"Regret by SOC regime:\n{json.dumps(soc_data, default=str, indent=2)}\n\n"
            f"Top 10 highest-regret cases:\n{json.dumps(examples, indent=2)}\n\n"
            "Identify 3–7 compact, actionable rules to guide future recommendations. "
            "Focus on specific patterns: which hours are bad, which SOC regimes cause problems, "
            "whether limit prices are set too high/low, etc.\n\n"
            'Output ONLY a JSON array of strings: ["rule 1", "rule 2", ...]'
        )

        try:
            resp = client.messages.create(
                model=self._model_id,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
            )
            text = resp.content[0].text
            raw = json.loads(_extract_json(text))
            if isinstance(raw, list):
                return [str(r) for r in raw if r]
        except Exception as exc:
            logger.warning("distill_lessons parse failed: %s", exc)
        return []


def _hard_gate(rec: Recommendation, tool_calls_made: list[str]) -> bool:
    """All evidence_tool_calls must have been actually called. Non-none must have evidence."""
    if rec.direction == "none":
        return True
    if not tool_calls_made:
        return False
    if not rec.evidence_tool_calls:
        return False
    called_set = set(tool_calls_made)
    for tool in rec.evidence_tool_calls:
        if tool not in called_set:
            return False
    if rec.volume_mw < 0 or rec.limit_price < 0:
        return False
    return True


def _parse_recommendation(
    text: str,
    interval_id: str,
    tool_calls_made: list[str],
) -> Recommendation:
    try:
        raw = json.loads(_extract_json(text))
        direction = raw.get("direction", "none")
        if direction not in ("charge", "discharge", "none"):
            direction = "none"

        # Only keep evidence that was actually called
        called_set = set(tool_calls_made)
        evidence = [t for t in raw.get("evidence_tool_calls", []) if t in called_set]

        return Recommendation(
            recommendation_id=str(uuid.uuid4()),
            interval_id=interval_id,
            direction=direction,
            volume_mw=max(0.0, float(raw.get("volume_mw", 0.0))),
            limit_price=max(0.0, float(raw.get("limit_price", 0.0))),
            confidence=raw.get("confidence", "low") if raw.get("confidence") in ("high", "low") else "low",
            reasoning=str(raw.get("reasoning", ""))[:500],
            evidence_tool_calls=evidence,
            similar_past_interval_ids=raw.get("similar_past_interval_ids", []),
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning("Failed to parse narrator output: %s | text: %s", exc, text[:200])
        return _none_rec(interval_id, "narrator parse error")


def _extract_json(text: str) -> str:
    """Extract the first JSON object from a string."""
    # Strip markdown fences if present
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")
    # Find first { ... }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return text.strip()


def _none_rec(interval_id: str, reason: str) -> Recommendation:
    return Recommendation(
        recommendation_id=str(uuid.uuid4()),
        interval_id=interval_id,
        direction="none",
        volume_mw=0.0,
        limit_price=0.0,
        confidence="low",
        reasoning=reason,
        evidence_tool_calls=[],
        similar_past_interval_ids=[],
        generated_at=datetime.now(timezone.utc),
    )
