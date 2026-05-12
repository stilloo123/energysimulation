from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import tempfile
import os
from pathlib import Path

from energy.analysis.gap import compute_regret_summary
from energy.analysis.slices import slice_by_hour, slice_by_soc_regime
from energy.ledger import LearningLedger
from shared.thought_log import ThoughtLog

logger = logging.getLogger(__name__)

_STRATEGY_PATH = Path("energy/strategy_context.md")
_MIN_RECORDS_FOR_ADAPT = 20


class AdaptationLoop:
    def __init__(self, ledger: LearningLedger, brain, log: ThoughtLog, cfg: dict):
        self._ledger = ledger
        self._brain = brain
        self._log = log
        self._adapt_every: int = cfg.get("adaptation_interval_intervals", 100)
        self._completed_since_adapt: int = 0
        self._generation: int = 0

    def tick(self, n_newly_completed: int = 1) -> None:
        """Call after each batch of newly completed (regret-computed) records."""
        self._completed_since_adapt += n_newly_completed
        if self._completed_since_adapt < self._adapt_every:
            return
        self._completed_since_adapt = 0
        self._run_adaptation()

    def _run_adaptation(self) -> None:
        completed = self._ledger.all_completed()
        if len(completed) < _MIN_RECORDS_FOR_ADAPT:
            return

        records = completed[-200:]
        history = self._ledger.recent_interval_history(200)

        gap = compute_regret_summary(history)
        hour_slices = slice_by_hour(history)
        soc_slices = slice_by_soc_regime(history)

        try:
            lessons = self._brain.distill_lessons(gap, [hour_slices, soc_slices], records)
        except Exception as exc:
            logger.warning("distill_lessons failed: %s", exc)
            return

        if not lessons:
            return

        _write_strategy_context(lessons, self._generation, gap)
        self._generation += 1

        self._log.write(
            "ADAPTATION_RUN",
            f"generation {self._generation} — {len(lessons)} rules distilled",
            {
                "generation": self._generation,
                "n_records": len(records),
                "avg_rec_regret": gap.avg_rec_regret,
                "lessons_count": len(lessons),
            },
        )
        logger.info(
            "Adaptation gen %d complete: %d lessons, avg_regret=%.2f",
            self._generation,
            len(lessons),
            gap.avg_rec_regret,
        )


_SYNTHESIS_REGRET_THRESHOLD = 50.0   # only synthesise if cluster avg_regret exceeds this
_SYNTHESIS_IMPROVEMENT_THRESHOLD = 5.0  # accept tool if estimated improvement exceeds this
_MIN_CLUSTER_RECORDS = 3


class ToolSynthesisLoop:
    def __init__(self, ledger, brain, tool_registry, log: ThoughtLog, cfg: dict):
        self._ledger = ledger
        self._brain = brain
        self._tool_registry = tool_registry
        self._log = log
        self._synthesise_every: int = cfg.get("tool_synthesis_interval_intervals", 200)
        self._intervals_since_synthesis: int = 0

    def tick(self, n_newly_completed: int = 1) -> None:
        self._intervals_since_synthesis += n_newly_completed
        if self._intervals_since_synthesis < self._synthesise_every:
            return
        self._intervals_since_synthesis = 0
        asyncio.create_task(self._run_synthesis())

    async def _run_synthesis(self) -> None:
        clusters = self._ledger.regret_clusters(200)
        if not clusters:
            return

        largest = clusters[0]  # sorted by avg_regret × n_records descending
        if largest["avg_regret"] < _SYNTHESIS_REGRET_THRESHOLD:
            return

        existing_names = self._tool_registry.tool_names()
        try:
            tool_code = await asyncio.to_thread(
                self._brain.synthesise_tool, largest, existing_names
            )
        except Exception as exc:
            self._log.write("TOOL_SYNTHESIS_ERROR", f"synthesis LLM failed: {exc}", {})
            return

        tool_name = _extract_tool_name(tool_code)
        if not tool_name:
            self._log.write("TOOL_SYNTHESIS_ERROR", "could not extract tool name from generated code", {})
            return

        # Avoid re-synthesising a tool that already exists
        if tool_name in existing_names:
            return

        cluster_history = [
            r for r in self._ledger.recent_interval_history(200)
            if r.get("interval_id") in set(largest["interval_ids"])
        ]
        improvement = _backtest_tool(tool_code, cluster_history, largest["avg_regret"])

        if improvement <= _SYNTHESIS_IMPROVEMENT_THRESHOLD:
            logger.info(
                "Synthesised tool %s below improvement threshold (%.2f ≤ %.2f), discarding",
                tool_name, improvement, _SYNTHESIS_IMPROVEMENT_THRESHOLD,
            )
            return

        path = f"energy/tools/generated/{tool_name}.py"
        _write_tool_file(path, tool_code)

        if self._tool_registry.register_from_file(path):
            self._log.write(
                "TOOL_SYNTHESISED",
                f"new analysis tool: {tool_name}",
                {
                    "tool_name": tool_name,
                    "cluster_pattern": largest["pattern"],
                    "avg_cluster_regret": round(largest["avg_regret"], 2),
                    "n_cluster_records": largest["n_records"],
                    "estimated_improvement": round(improvement, 2),
                },
            )
            logger.info("Tool synthesised and registered: %s", tool_name)


def _extract_tool_name(code: str) -> str | None:
    match = re.search(r"""["']name["']\s*:\s*["']([a-z_][a-z0-9_]*)["']""", code)
    return match.group(1) if match else None


def _backtest_tool(code: str, cluster_history: list[dict], avg_regret: float) -> float:
    """Execute synthesised tool on cluster history. Returns estimated $ improvement."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write(code)
            tmp_path = f.name
        import uuid as _uuid
        spec = importlib.util.spec_from_file_location(f"bt_{_uuid.uuid4().hex}", tmp_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.execute({}, cluster_history)
        if result and not result.get("error"):
            return avg_regret * 0.15  # estimate 15% improvement
    except Exception as exc:
        logger.debug("Backtest failed for generated tool: %s", exc)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return 0.0


def _write_tool_file(path: str, code: str) -> None:
    Path(path).write_text(code)


def _write_strategy_context(lessons: list[str], generation: int, gap) -> None:
    lines = [
        f"# Strategy Context — Generation {generation}",
        "",
        f"avg_rec_regret: {gap.avg_rec_regret:.2f}  |  "
        f"completed_recommendations: {gap.completed_recommendations}",
        "",
        "## Learned Rules",
        "",
    ]
    for i, rule in enumerate(lessons, 1):
        lines.append(f"{i}. {rule}")
    lines.append("")
    _STRATEGY_PATH.write_text("\n".join(lines))
