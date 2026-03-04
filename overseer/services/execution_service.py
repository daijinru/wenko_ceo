"""Execution service — pure orchestration layer for the cognitive loop.

After Milestone 1 refactoring, this service is ~400 lines (down from 1210).
All security logic lives in kernel/firewall_engine.py.
All HITL logic lives in kernel/human_gate.py.
All perception logic lives in kernel/perception_bus.py.
Plugins are accessed via kernel/registry.py.

This file is ONLY orchestration: it sequences calls to kernel + plugins
and manages the while-True loop, checkpoints, and TUI callbacks.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from overseer.config import get_config
from overseer.core.enums import COStatus, ExecutionStatus
from overseer.core.plugin_protocols import (
    ContextPlugin,
    LLMPlugin,
    MemoryPlugin,
    PlanPlugin,
    ToolPlugin,
)
from overseer.core.protocols import LLMDecision, ToolCall
from overseer.database import get_session
from overseer.kernel.firewall_engine import FirewallEngine
from overseer.kernel.human_gate import HumanGate, Intent
from overseer.kernel.perception_bus import PerceptionBus
from overseer.kernel.registry import PluginRegistry
from overseer.models.cognitive_object import CognitiveObject
from overseer.models.execution import Execution
from overseer.services.artifact_service import ArtifactService
from overseer.services.cognitive_object_service import CognitiveObjectService
from overseer.services.memory_extractor import MemoryExtractor

logger = logging.getLogger(__name__)


class ExecutionService:
    """Pure orchestration engine — sequences kernel + plugin calls.

    TUI-facing API is unchanged from the pre-refactor version:
    - __init__(session), set_callbacks(...), provide_human_response(decision, text)
    - Direct access to .tool_service / .llm_service for TUI panels
    """

    def __init__(self, session: Session | None = None):
        self._session = session
        cfg = get_config()

        # Kernel components
        self._perception = PerceptionBus()
        self._firewall = FirewallEngine(cfg, self._perception)
        self._human_gate = HumanGate()

        # Plugin registry (creates default Service implementations)
        self._registry = PluginRegistry.create_default(cfg, session)

        # Direct service references for TUI backward compatibility
        self.co_service = CognitiveObjectService(session)
        self.artifact_service = ArtifactService(session)
        # Expose plugin instances so TUI can access them directly
        self.tool_service = self._registry.get(ToolPlugin)
        self.llm_service = self._registry.get(LLMPlugin)
        self.context_service = self._registry.get(ContextPlugin)
        self.memory_service = self._registry.get(MemoryPlugin)
        self.planning_service = self._registry.get(PlanPlugin)

        # Memory extraction (orchestration-layer judgment, not a plugin)
        self._memory_extractor = MemoryExtractor()

        # TUI callbacks
        self._on_step_update: Optional[Callable] = None
        self._on_human_required: Optional[Callable] = None
        self._on_tool_confirm: Optional[Callable] = None
        self._on_complete: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_info: Optional[Callable] = None
        self._on_stream_chunk: Optional[Callable] = None

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = get_session()
        return self._session

    # ── TUI interface (unchanged) ──

    def set_callbacks(
        self,
        on_step_update: Optional[Callable] = None,
        on_human_required: Optional[Callable] = None,
        on_tool_confirm: Optional[Callable] = None,
        on_complete: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_info: Optional[Callable] = None,
        on_stream_chunk: Optional[Callable] = None,
    ) -> None:
        """Set callback functions for TUI communication."""
        self._on_step_update = on_step_update
        self._on_human_required = on_human_required
        self._on_tool_confirm = on_tool_confirm
        self._on_complete = on_complete
        self._on_error = on_error
        self._on_info = on_info
        self._on_stream_chunk = on_stream_chunk

    def provide_human_response(self, decision: str, text: str = "") -> None:
        """Called by TUI when human makes a decision."""
        self._human_gate.provide_response(decision, text)

    # ── Checkpoint / Resume ──

    def _save_checkpoint(
        self,
        co_id: str,
        pause_reason: str,
        *,
        elapsed_seconds: float,
        announced_subtask_id: int | None,
        pending_hitl: Dict[str, Any] | None = None,
        pending_tool_confirm: Dict[str, Any] | None = None,
        wrap_up_injected: bool = False,
    ) -> None:
        """Persist all ephemeral state into co.context['_checkpoint']."""
        co = self.co_service.get(co_id)
        if co is None:
            return
        ctx = copy.deepcopy(co.context or {})
        ctx["_checkpoint"] = {
            # FirewallEngine loop state
            **self._firewall.get_loop_state(),
            # PerceptionBus confidence window
            "confidence_history": list(self._perception.get_stats().confidence_window),
            "elapsed_seconds_at_pause": elapsed_seconds,
            # HumanGate state
            **self._human_gate.get_state(),
            # PerceptionBus tool outputs
            "last_tool_outputs": self._perception.get_tool_outputs_snapshot(),
            # Orchestration state
            "announced_subtask_id": announced_subtask_id,
            "pending_hitl": pending_hitl,
            "pending_tool_confirm": pending_tool_confirm,
            "paused_at_step": ctx.get("step_count", 0),
            "pause_reason": pause_reason,
            "wrap_up_injected": wrap_up_injected,
        }
        co.context = ctx
        self.co_service.session.commit()
        logger.info(
            "Checkpoint saved for CO %s at step %d (reason: %s)",
            co_id[:8], ctx.get("step_count", 0), pause_reason,
        )

    def _restore_checkpoint(self, co_id: str) -> Dict[str, Any] | None:
        """Restore ephemeral state from co.context['_checkpoint']."""
        co = self.co_service.get(co_id)
        if co is None:
            return None
        cp = (co.context or {}).get("_checkpoint")
        if not cp:
            return None

        # Restore kernel state
        self._firewall.restore_loop_state(cp)
        self._human_gate.restore_state(cp)
        self._perception.restore_tool_outputs(cp.get("last_tool_outputs", {}))

        # Restore confidence history into perception
        for c in cp.get("confidence_history", []):
            self._perception.record_confidence(c)

        logger.info(
            "Checkpoint restored for CO %s from step %d (reason: %s)",
            co_id[:8], cp.get("paused_at_step", 0), cp.get("pause_reason", "unknown"),
        )

        return {
            "elapsed_seconds_at_pause": cp.get("elapsed_seconds_at_pause", 0.0),
            "announced_subtask_id": cp.get("announced_subtask_id"),
            "pause_reason": cp.get("pause_reason", "pause"),
            "human_decision": cp.get("human_decision"),
            "wrap_up_injected": cp.get("wrap_up_injected", False),
        }

    def _clear_checkpoint(self, co_id: str) -> None:
        """Remove checkpoint data from context after successful resume."""
        co = self.co_service.get(co_id)
        if co is None:
            return
        ctx = copy.deepcopy(co.context or {})
        if "_checkpoint" in ctx:
            del ctx["_checkpoint"]
            co.context = ctx
            self.co_service.session.commit()

    # ── Preference persistence ──

    def _persist_preferences(self, co_id: str) -> None:
        """Write stable implicit preferences to Memory for future reuse."""
        stats = self._perception.get_stats()
        all_tools = set(stats.approval_counts.keys()) | set(stats.reject_counts.keys())
        memory = self._registry.get(MemoryPlugin)

        for tool in all_tools:
            approved = stats.approval_counts.get(tool, 0)
            rejected = stats.reject_counts.get(tool, 0)
            total = approved + rejected
            if total < 3:
                continue
            reject_rate = rejected / total
            if reject_rate >= 0.7:
                content = (
                    f"User tends to reject tool '{tool}' "
                    f"(reject rate {reject_rate:.0%}, n={total}). "
                    f"Consider avoiding this tool or requesting confirmation beforehand."
                )
            elif reject_rate <= 0.1 and total >= 5:
                content = (
                    f"User consistently approves tool '{tool}' "
                    f"(approve rate {1 - reject_rate:.0%}, n={total}). "
                    f"This tool can likely be used with auto permission."
                )
            else:
                continue
            existing = memory.query_by_tags(["implicit_preference", tool], category="preference")
            if existing:
                memory.update(existing[0].id, content=content)
            else:
                memory.save(
                    category="preference",
                    content=content,
                    tags=["implicit_preference", tool],
                    source_co_id=co_id,
                )

    # ── Working Memory → Long-term Memory bridge ──

    def _bridge_working_memory(self, co_id: str) -> None:
        """Persist high-quality WorkingMemory findings into long-term memory.

        On task completion, ``failed_approaches`` are saved as ``lesson``
        memories and ``key_findings`` as ``domain_knowledge`` memories.
        """
        co = self.co_service.get(co_id)
        if co is None:
            return
        wm_data = (co.context or {}).get("working_memory")
        if not wm_data:
            return

        from overseer.core.protocols import WorkingMemory

        try:
            wm = WorkingMemory(**wm_data) if isinstance(wm_data, dict) else wm_data
        except Exception:
            logger.debug("Failed to parse working_memory for CO %s", co_id[:8])
            return

        memory = self._registry.get(MemoryPlugin)

        for approach in wm.failed_approaches:
            if approach.strip():
                memory.save(
                    category="lesson",
                    content=approach.strip(),
                    tags=["from_working_memory", "failed_approach"],
                    source_co_id=co_id,
                )

        for finding in wm.key_findings:
            stripped = finding.strip()
            if not stripped:
                continue
            # Skip purely procedural descriptions (very short or generic)
            if len(stripped) < 15:
                continue
            memory.save(
                category="domain_knowledge",
                content=stripped,
                tags=["from_working_memory", "key_finding"],
                source_co_id=co_id,
            )

    # ── Helper methods ──

    def _drain_mcp_stderr(self, co_id: str) -> None:
        """Forward any new MCP subprocess stderr lines to TUI."""
        tools = self._registry.get(ToolPlugin)
        lines = tools.drain_stderr()
        if lines and self._on_info:
            for line in lines:
                self._on_info(co_id, line)

    async def _run_planning_phase(self, co_id: str) -> bool:
        """Generate a task plan via LLM. Returns True if plan was generated."""
        co = self.co_service.get(co_id)
        if co is None:
            return False

        if self._on_info:
            self._on_info(co_id, "[Phase] Entering planning phase...")

        memory = self._registry.get(MemoryPlugin)
        tools = self._registry.get(ToolPlugin)
        planner = self._registry.get(PlanPlugin)

        memories = memory.retrieve_as_text(co.title + " " + co.description, limit=3)
        available_tools = tools.list_tools()

        try:
            plan = await planner.generate_plan(co, memories, available_tools)
            llm = self._registry.get(LLMPlugin)
            self._perception.record_token_usage(llm.last_usage())
            if plan and plan.subtasks:
                planner.store_plan(co, plan)
                subtask_titles = [st.title for st in plan.subtasks]
                if self._on_info:
                    self._on_info(
                        co_id,
                        f"[Phase] Planning complete: {len(plan.subtasks)} subtasks — "
                        + ", ".join(subtask_titles),
                    )
                return True
        except Exception as e:
            logger.warning("Planning phase failed, falling back to flat execution: %s", e)

        if self._on_info:
            self._on_info(co_id, "[Phase] Planning skipped, using flat execution mode")
        return False

    async def _run_checkpoint(self, co_id: str) -> None:
        """At subtask boundary, reflect on progress and optionally revise plan."""
        co = self.co_service.get(co_id)
        if co is None:
            return
        planner = self._registry.get(PlanPlugin)

        if self._on_info:
            self._on_info(co_id, "[Phase] Checkpoint: reviewing progress...")

        revised = await planner.checkpoint_reflect(co)
        llm = self._registry.get(LLMPlugin)
        self._perception.record_token_usage(llm.last_usage())
        if revised:
            if self._on_info:
                self._on_info(co_id, "[Phase] Plan revised at checkpoint")
        else:
            progress = planner.get_plan_progress_text(co)
            if self._on_info and progress:
                self._on_info(co_id, f"[Phase] {progress}")

    async def _compress_working_memory(self, co_id: str) -> None:
        """Compress accumulated findings into WorkingMemory at subtask boundary."""
        co = self.co_service.get(co_id)
        if co is None:
            return
        ctx_plugin = self._registry.get(ContextPlugin)
        llm = self._registry.get(LLMPlugin)

        wm = await ctx_plugin.compress_to_working_memory(co, llm)
        self._perception.record_token_usage(llm.last_usage())
        if wm:
            if self._on_info:
                self._on_info(co_id, "[Phase] Context compressed to working memory")
        else:
            ctx_plugin.compress_if_needed(co)

    # ── Main cognitive loop ──

    async def run_loop(self, co_id: str) -> None:
        """Main cognitive loop for a CognitiveObject.

        Orchestrates kernel (firewall, human gate, perception) and
        plugins (LLM, tools, planning, memory, context) in sequence.
        """
        co = self.co_service.get(co_id)
        if co is None:
            logger.error("CognitiveObject not found: %s", co_id)
            return

        # Retrieve plugins
        llm = self._registry.get(LLMPlugin)
        tools = self._registry.get(ToolPlugin)
        ctx_plugin = self._registry.get(ContextPlugin)
        memory = self._registry.get(MemoryPlugin)
        planner = self._registry.get(PlanPlugin)
        firewall = self._firewall
        gate = self._human_gate
        perception = self._perception

        # Connect to MCP servers
        try:
            mcp_lines = await tools.connect()
            if mcp_lines and self._on_info:
                for line in mcp_lines:
                    self._on_info(co_id, line)
        except Exception as e:
            logger.warning("MCP connection failed, using builtin tools: %s", e)

        # Set MCP tool names on PolicyStore for correct default permission
        mcp_tool_names = set()
        if hasattr(tools, '_mcp_tool_map'):
            mcp_tool_names = set(tools._mcp_tool_map.keys())
        firewall.policy.set_mcp_tools(mcp_tool_names)

        # Set status to running
        self.co_service.update_status(co_id, COStatus.RUNNING)
        step_number = (co.context or {}).get("step_count", 0)

        # Attempt to restore from checkpoint
        _cp = self._restore_checkpoint(co_id)
        _is_resuming = _cp is not None

        if _cp:
            _elapsed_offset = _cp["elapsed_seconds_at_pause"]
            _announced_subtask_id = _cp["announced_subtask_id"]
            _resume_reason = _cp["pause_reason"]
            self._clear_checkpoint(co_id)
        else:
            _elapsed_offset = 0.0
            _announced_subtask_id = None
            _resume_reason = None

        _loop_start_time = asyncio.get_event_loop().time()
        cfg = get_config()
        _max_steps = cfg.execution.max_steps
        _wrap_up_injected = _cp.get("wrap_up_injected", False) if _cp else False

        # ── Planning phase ──
        if cfg.planning.enabled and not (co.context or {}).get("plan"):
            await self._run_planning_phase(co_id)
            co = self.co_service.get(co_id)

        # ── Resume signal injection ──
        if _is_resuming:
            co = self.co_service.get(co_id)
            _human_decision = _cp.get("human_decision")
            if _human_decision:
                _decision_text = (
                    f"System: execution resumed after {_resume_reason}. "
                    f"User responded to the pending HITL request: "
                    f"decision=\"{_human_decision.get('choice', '')}\", "
                    f"feedback=\"{_human_decision.get('text', '')}\". "
                    f"Continue based on the user's response."
                )
                ctx_plugin.merge_step_result(co, step_number, "system:resumed", _decision_text)
                self.co_service.session.commit()
            else:
                ctx_plugin.merge_step_result(
                    co, step_number, "system:resumed",
                    f"System: execution resumed after {_resume_reason}. "
                    f"Continue from where you left off at step {step_number}. "
                    f"Do NOT repeat work already done. Review the accumulated findings above "
                    f"and pick up the next logical action.",
                )
                self.co_service.session.commit()
            if self._on_info:
                if _human_decision:
                    self._on_info(
                        co_id,
                        f"[System] Resumed from checkpoint (step {step_number}) "
                        f"— user decision: {_human_decision.get('choice', '')}"
                        + (f", feedback: {_human_decision.get('text', '')}" if _human_decision.get('text') else ""),
                    )
                else:
                    self._on_info(co_id, f"[System] Resumed from checkpoint (step {step_number})")

        try:
            while True:
                step_number += 1
                co = self.co_service.get(co_id)

                # ── Step limit enforcement ──
                if _max_steps > 0 and step_number > _max_steps + 1 and _wrap_up_injected:
                    logger.warning("CO %s exceeded max_steps+1 (%d), forcing PAUSED", co_id[:8], _max_steps + 1)
                    if self._on_info:
                        self._on_info(co_id, f"[System] 步数上限已超出 — LLM 未在收尾步完成，强制暂停")
                    _force_elapsed = (asyncio.get_event_loop().time() - _loop_start_time) + _elapsed_offset
                    self._save_checkpoint(
                        co_id, "step_limit_exceeded",
                        elapsed_seconds=_force_elapsed,
                        announced_subtask_id=_announced_subtask_id,
                        wrap_up_injected=_wrap_up_injected,
                    )
                    self._persist_preferences(co_id)
                    self.co_service.update_status(co_id, COStatus.PAUSED)
                    await tools.disconnect()
                    await llm.close()
                    if self._on_complete:
                        self._on_complete(co_id, "paused")
                    return
                elif _max_steps > 0 and step_number > _max_steps and not _wrap_up_injected:
                    _wrap_up_injected = True
                    ctx_plugin.merge_step_result(
                        co, step_number - 1, "system:step_limit",
                        f"[URGENT — 步数上限已达到 ({_max_steps})] "
                        f"你已使用完全部 {_max_steps} 个执行步骤。这是你的最后一步。你必须：\n"
                        f"1. 总结目前已完成的所有工作\n"
                        f"2. 列出所有未完成的事项\n"
                        f"3. 在决策块中设置 task_complete: true\n"
                        f"4. 不要启动任何新的工作或工具调用",
                    )
                    self.co_service.session.commit()
                    if self._on_info:
                        self._on_info(co_id, f"[System] 步数上限 ({_max_steps}) 已达到 — 注入收尾信号")

                # Announce current subtask if changed
                current_subtask = planner.get_current_subtask(co)
                if current_subtask and current_subtask.id != _announced_subtask_id:
                    _announced_subtask_id = current_subtask.id
                    plan = (co.context or {}).get("plan", {})
                    total = len(plan.get("subtasks", []))
                    if self._on_info:
                        self._on_info(co_id, f"[Phase] Starting subtask {current_subtask.id}/{total}: {current_subtask.title}")

                # Drain MCP stderr
                self._drain_mcp_stderr(co_id)

                # ── 1. Create Execution record ──
                execution = Execution(
                    cognitive_object_id=co_id,
                    sequence_number=step_number,
                    status=ExecutionStatus.RUNNING_LLM,
                )
                self.session.add(execution)
                self.session.commit()
                self.session.refresh(execution)

                if self._on_step_update:
                    self._on_step_update(execution, "running_llm")

                # ── 2. Build prompt ──
                memories_text = memory.retrieve_as_text(co.title + " " + co.description, limit=3)
                available_tools = tools.list_tools()
                elapsed = (asyncio.get_event_loop().time() - _loop_start_time) + _elapsed_offset

                # Build constraints from firewall
                constraints = firewall.build_constraints(co.context or {})

                prompt = ctx_plugin.build_prompt(
                    co, memories_text, available_tools,
                    elapsed_seconds=elapsed, max_steps=_max_steps,
                    constraint_hints=constraints,
                )
                execution.prompt = prompt
                self.session.commit()

                # ── 3. Call LLM ──
                try:
                    _stream_cb = None
                    if self._on_stream_chunk:
                        _cid = co_id
                        def _stream_cb(text: str) -> None:
                            self._on_stream_chunk(_cid, text)

                    system_prompt = firewall.get_system_prompt()
                    llm_result = await llm.call(
                        prompt, system_prompt=system_prompt,
                        stream=bool(_stream_cb), on_chunk=_stream_cb,
                    )
                    response = llm_result.content
                except Exception as e:
                    logger.error("LLM call failed at step %d: %s", step_number, e)
                    execution.status = ExecutionStatus.FAILED
                    execution.llm_response = f"LLM Error: {e}"
                    self.session.commit()
                    if self._on_error:
                        self._on_error(str(e))
                    self.co_service.update_status(co_id, COStatus.PAUSED)
                    try:
                        _llm_err_elapsed = (asyncio.get_event_loop().time() - _loop_start_time) + _elapsed_offset
                        self._save_checkpoint(
                            co_id, "error",
                            elapsed_seconds=_llm_err_elapsed,
                            announced_subtask_id=_announced_subtask_id,
                            wrap_up_injected=_wrap_up_injected,
                        )
                    except Exception:
                        logger.debug("Failed to save checkpoint on LLM error", exc_info=True)
                    await tools.disconnect()
                    await llm.close()
                    return

                execution.llm_response = response
                execution.token_usage = llm_result.usage.model_dump()
                perception.record_token_usage(llm_result.usage)
                self.session.commit()

                # ── 4. Parse decision (via firewall — fail-safe) ──
                decision = firewall.parse_decision(response)
                execution.llm_decision = decision.model_dump()
                execution.title = decision.next_action.title if decision.next_action else f"Step {step_number}"
                self.session.commit()

                if self._on_step_update:
                    self._on_step_update(execution, "llm_done")

                # ── 4.5 Record confidence into perception ──
                perception.record_confidence(decision.confidence)

                # ── 5. Firewall evaluation (help escalation, confidence breaker, loop detection) ──
                verdict = firewall.evaluate(decision)
                decision = verdict.decision or decision

                # ── 6. Handle tool calls ──
                if decision.tool_calls and verdict.action != "block":
                    # Pre-filter args via firewall
                    _removed_params: dict[str, list[str]] = {}
                    for tc in decision.tool_calls:
                        schema = None
                        for t in available_tools:
                            if t.get("name") == tc.tool:
                                schema = t.get("parameters")
                                break
                        filtered_args, removed = firewall.filter_args(tc.tool, tc.args, schema)
                        if removed:
                            _removed_params[tc.tool] = removed
                            logger.info("Pre-filtered args for %s: removed %s", tc.tool, removed)
                        tc.args = filtered_args

                    execution.status = ExecutionStatus.RUNNING_TOOL
                    execution.tool_calls = [tc.model_dump() for tc in decision.tool_calls]
                    self.session.commit()

                    if self._on_step_update:
                        self._on_step_update(execution, "running_tool")

                    all_results = []
                    for tc in decision.tool_calls:
                        # Per-tool permission check via firewall
                        needs_approval, needs_preview = firewall.check_tool_permission(tc)

                        if needs_approval:
                            execution.status = ExecutionStatus.AWAITING_HUMAN
                            self.session.commit()
                            if self._on_tool_confirm:
                                self._on_tool_confirm(execution, tc)

                            # Save checkpoint before tool approval wait
                            _tool_elapsed = (asyncio.get_event_loop().time() - _loop_start_time) + _elapsed_offset
                            self._save_checkpoint(
                                co_id, "tool_confirm_wait",
                                elapsed_seconds=_tool_elapsed,
                                announced_subtask_id=_announced_subtask_id,
                                pending_tool_confirm={"tool_name": tc.tool, "tool_args": tc.args},
                                wrap_up_injected=_wrap_up_injected,
                            )

                            # Time the approval wait
                            _approval_start = time.monotonic()
                            human = await gate.wait_for_human()
                            _approval_elapsed = time.monotonic() - _approval_start

                            is_approved = human["decision"] != "reject"
                            perception.record_approval(tc.tool, is_approved, _approval_elapsed)

                            # Hesitation detection (skip if user was simply away)
                            if (firewall.hesitation_threshold
                                    <= _approval_elapsed
                                    < firewall.absence_threshold):
                                logger.info("User hesitated %.1fs on tool '%s'", _approval_elapsed, tc.tool)
                                ctx_plugin.merge_step_result(
                                    co, step_number, "perception:hesitation",
                                    f"System: user took {_approval_elapsed:.0f}s to respond "
                                    f"to '{tc.tool}' (decision: {human['decision']}). "
                                    f"User may be uncertain about this operation — "
                                    f"consider explaining your intent more clearly next time.",
                                )
                                if self._on_info:
                                    self._on_info(co_id, f"[Perception] User hesitated {_approval_elapsed:.0f}s on '{tc.tool}'")

                            if not is_approved:
                                all_results.append({
                                    "tool": tc.tool,
                                    "status": "rejected",
                                    "reason": human.get("text", "User rejected"),
                                })
                                # Check auto-escalation via firewall
                                escalated = firewall.should_escalate(tc.tool)
                                if escalated:
                                    ctx_plugin.merge_step_result(
                                        co,
                                        (co.context or {}).get("step_count", 0),
                                        "perception:tool_avoidance",
                                        f"System: user has rejected tool '{tc.tool}' multiple consecutive times. "
                                        f"STOP using this tool. Find an alternative approach or ask for guidance.",
                                    )
                                    if self._on_info:
                                        self._on_info(
                                            co_id,
                                            f"[Perception] Tool '{tc.tool}' permission escalated to 'approve' "
                                            f"after consecutive rejections",
                                        )
                                continue
                            execution.status = ExecutionStatus.RUNNING_TOOL
                            self.session.commit()

                        # Sandbox args via firewall before execution
                        tc = firewall.sandbox_args(tc)

                        # Execute tool
                        result = await tools.execute(tc)
                        all_results.append({"tool": tc.tool, **result})

                        # Record artifact if file was written
                        _path_arg = (
                            tc.args.get("path") or tc.args.get("file_path")
                            or tc.args.get("filepath") or tc.args.get("filename")
                            or tc.args.get("outputPath") or tc.args.get("output_path")
                            or tc.args.get("savePath") or tc.args.get("save_path")
                        )
                        if _path_arg and result.get("status") == "ok":
                            self.artifact_service.record(
                                co_id=co_id,
                                execution_id=execution.id,
                                name=_path_arg.split("/")[-1],
                                file_path=result.get("path", _path_arg),
                                artifact_type="document",
                            )
                            ctx_plugin.add_artifact(co, result.get("path", _path_arg))

                        # Merge tool result with perception enrichment
                        result_summary = ctx_plugin.summarize_tool_result(tc.tool, result)
                        removed = _removed_params.get(tc.tool)
                        if removed:
                            result_summary = (
                                f"[WARNING: parameters {removed} are NOT accepted by "
                                f"this tool and were ignored. Only use parameters listed "
                                f"in the tool schema.] {result_summary}"
                            )

                        # Perception: classify + diff detection
                        classification = perception.classify_result(tc.tool, result)
                        diff_note = perception.detect_repeat(tc.tool, result_summary, tc.args)
                        enriched = f"[{classification}]{diff_note or ''} {result_summary}"
                        ctx_plugin.merge_step_result(co, step_number, f"tool:{tc.tool}", enriched)

                    execution.tool_results = all_results
                    self.session.commit()

                    # Log tool results
                    from overseer.logging_config import log_tool_result
                    for tr in all_results:
                        log_tool_result(tr, co_id=co_id, step_number=step_number)

                    # Intent-result deviation detection via firewall
                    intent_desc = decision.next_action.description if decision.next_action else ""
                    deviation = firewall.check_deviation(intent_desc, all_results)
                    if deviation:
                        logger.info("Intent-result deviation: %s", deviation)
                        ctx_plugin.merge_step_result(co, step_number, "perception:deviation", f"System: {deviation}")
                        if self._on_info:
                            self._on_info(co_id, f"[Perception] {deviation}")

                # ── 7. Handle HITL decision request ──
                if decision.human_required:
                    execution.status = ExecutionStatus.AWAITING_HUMAN
                    self.session.commit()

                    if self._on_human_required:
                        self._on_human_required(
                            execution,
                            decision.human_reason or "Your input is needed.",
                            decision.options,
                        )

                    self.co_service.update_status(co_id, COStatus.PAUSED)

                    _hitl_elapsed_ts = (asyncio.get_event_loop().time() - _loop_start_time) + _elapsed_offset
                    self._save_checkpoint(
                        co_id, "hitl_wait",
                        elapsed_seconds=_hitl_elapsed_ts,
                        announced_subtask_id=_announced_subtask_id,
                        pending_hitl={
                            "reason": decision.human_reason or "Your input is needed.",
                            "options": decision.options or [],
                        },
                        wrap_up_injected=_wrap_up_injected,
                    )

                    # Time the human response
                    _hitl_start = time.monotonic()
                    human = await gate.wait_for_human()
                    _hitl_elapsed = time.monotonic() - _hitl_start

                    # Hesitation detection for HITL decisions (skip if user was away)
                    if (firewall.hesitation_threshold
                            <= _hitl_elapsed
                            < firewall.absence_threshold):
                        ctx_plugin.merge_step_result(
                            co, step_number, "perception:hesitation",
                            f"System: user took {_hitl_elapsed:.0f}s to respond to HITL request. "
                            f"User may need more context or is uncertain about the direction.",
                        )
                        if self._on_info:
                            self._on_info(co_id, f"[Perception] User hesitated {_hitl_elapsed:.0f}s on HITL decision")

                    execution.human_decision = human["decision"]
                    execution.human_input = human.get("text", "")

                    # Parse intent via HumanGate
                    intent = gate.parse_intent(human)

                    if intent == Intent.FORCE_ABORT:
                        logger.info("User insisted on abort, force-aborting")
                        execution.status = ExecutionStatus.REJECTED
                        self.session.commit()
                        self._persist_preferences(co_id)
                        self.co_service.update_status(co_id, COStatus.ABORTED)
                        await tools.disconnect()
                        await llm.close()
                        if self._on_complete:
                            self._on_complete(co_id, "aborted")
                        return

                    if intent == Intent.ABORT:
                        # Graceful abort: inject wrap-up signal
                        execution.status = ExecutionStatus.APPROVED
                        self.session.commit()
                        self.co_service.update_status(co_id, COStatus.RUNNING)
                        ctx_plugin.merge_step_result(
                            co, step_number, "system:user_stop_request",
                            "[URGENT — User wants to STOP] "
                            "The user has requested to end this task. "
                            "You MUST do the following in your NEXT response:\n"
                            "1. Provide a brief summary of what has been accomplished so far.\n"
                            "2. Set task_complete: true in your decision.\n"
                            "3. Do NOT start any new work or tool calls.\n"
                            "4. Do NOT ask for confirmation — just finish.",
                        )
                        if self._on_info:
                            self._on_info(co_id, "[System] User requested stop — guiding LLM to wrap up gracefully")
                        continue

                    # Non-abort: continue
                    execution.status = ExecutionStatus.APPROVED
                    self.session.commit()
                    self.co_service.update_status(co_id, COStatus.RUNNING)

                    # Build decision text with system signals
                    decision_text = gate.build_decision_text(human, intent)
                    ctx_plugin.merge_step_result(co, step_number, "human_decision", decision_text)

                # ── 8. Merge non-tool step result ──
                if not decision.tool_calls:
                    summary = response[:300] if response else "No response"
                    ctx_plugin.merge_step_result(co, step_number, execution.title, summary)

                execution.status = ExecutionStatus.COMPLETED
                self.session.commit()

                if self._on_step_update:
                    self._on_step_update(execution, "completed")

                # ── 9. Self-evaluation (every N steps) ──
                if step_number % cfg.reflection.interval == 0:
                    try:
                        reflection_response = await llm.reflect(co.context)
                        perception.record_token_usage(llm.last_usage())
                        reflection_decision = firewall.parse_decision(reflection_response)
                        reflection_text = reflection_decision.reflection or reflection_response[:200]
                        ctx_plugin.merge_reflection(co, reflection_text)

                        # Stagnation detection via perception
                        _NO_PROGRESS_INDICATORS = [
                            "没有进展", "未取得进展", "停滞", "陷入", "原地踏步",
                            "no progress", "stuck", "stagnant", "not making progress",
                            "going in circles", "没有推进", "无法推进", "效果不佳",
                            "repeated", "重复", "ineffective", "无效",
                        ]
                        text_lower = reflection_text.lower()
                        if any(ind in text_lower for ind in _NO_PROGRESS_INDICATORS):
                            logger.warning("Reflection indicates no progress: %s", reflection_text[:100])
                            perception.record_stagnation(reflection_text)
                            ctx_plugin.merge_step_result(
                                co, step_number, "meta_perception",
                                "System: self-reflection indicates lack of progress. "
                                "Consider changing your approach entirely — "
                                "use different tools, reframe the problem, "
                                "or ask the user for clarification.",
                            )
                            if self._on_info:
                                self._on_info(co_id, "[Meta] Reflection detected stagnation, strategy switch hint injected")
                    except Exception as e:
                        logger.warning("Reflection failed: %s", e)

                # ── 10. Memory extraction ──
                extraction = self._memory_extractor.evaluate(
                    co_id, response, execution.title
                )
                if extraction:
                    memory.save(
                        category=extraction["category"],
                        content=extraction["content"],
                        tags=extraction["tags"],
                        source_co_id=co_id,
                    )

                # ── 10.5 Inject approval stats periodically ──
                if step_number % cfg.reflection.interval == 0:
                    summary_text = perception.build_approval_summary()
                    if summary_text:
                        ctx_plugin.merge_step_result(
                            co, step_number, "perception:user_preferences",
                            f"System: implicit user tool preferences — {summary_text}. "
                            f"Adjust your approach based on these signals.",
                        )

                # ── 11. Context compression ──
                ctx_plugin.compress_if_needed(co)

                # ── 11.5 Subtask completion handling ──
                if decision.subtask_complete and cfg.planning.enabled:
                    current_st = planner.get_current_subtask(co)
                    result_summary = decision.reflection or ""
                    next_st = planner.advance_subtask(co, result_summary)

                    if current_st and self._on_info:
                        self._on_info(co_id, f"[Phase] Subtask '{current_st.title}' completed")

                    if cfg.planning.checkpoint_on_subtask_complete:
                        await self._run_checkpoint(co_id)
                    if cfg.planning.compress_after_subtask:
                        await self._compress_working_memory(co_id)

                    # Reset firewall loop detection for new subtask
                    firewall.restore_loop_state({
                        "last_tool_sig": "",
                        "repeat_count": 0,
                        "last_tool_names": "",
                        "name_repeat_count": 0,
                    })

                    co = self.co_service.get(co_id)
                    if planner.all_subtasks_done(co):
                        if self._on_info:
                            self._on_info(co_id, "[Phase] All subtasks completed")

                # ── 12. Check completion ──
                if decision.task_complete:
                    self._persist_preferences(co_id)
                    self._bridge_working_memory(co_id)
                    self._clear_checkpoint(co_id)
                    self.co_service.update_status(co_id, COStatus.COMPLETED)
                    await tools.disconnect()
                    await llm.close()
                    if self._on_complete:
                        self._on_complete(co_id, "completed")
                    return

        except asyncio.CancelledError:
            logger.info("Execution loop cancelled for CO %s", co_id[:8])
            try:
                self._persist_preferences(co_id)
            except Exception:
                logger.debug("Failed to persist preferences on cancel", exc_info=True)
            try:
                _existing_cp = (self.co_service.get(co_id).context or {}).get("_checkpoint", {})
                _cancel_elapsed = (asyncio.get_event_loop().time() - _loop_start_time) + _elapsed_offset
                self._save_checkpoint(
                    co_id, "user_stop",
                    elapsed_seconds=_cancel_elapsed,
                    announced_subtask_id=_announced_subtask_id,
                    pending_hitl=_existing_cp.get("pending_hitl"),
                    pending_tool_confirm=_existing_cp.get("pending_tool_confirm"),
                    wrap_up_injected=_wrap_up_injected,
                )
            except Exception:
                logger.debug("Failed to save checkpoint on cancel", exc_info=True)
            self.co_service.update_status(co_id, COStatus.PAUSED)
            await tools.disconnect()
            await llm.close()
            raise
        except Exception as e:
            logger.error("Execution loop failed for CO %s: %s", co_id[:8], e, exc_info=True)
            try:
                self._persist_preferences(co_id)
            except Exception:
                logger.debug("Failed to persist preferences on error exit", exc_info=True)
            try:
                _err_elapsed = (asyncio.get_event_loop().time() - _loop_start_time) + _elapsed_offset
                self._save_checkpoint(
                    co_id, "error",
                    elapsed_seconds=_err_elapsed,
                    announced_subtask_id=_announced_subtask_id,
                    wrap_up_injected=_wrap_up_injected,
                )
            except Exception:
                logger.debug("Failed to save checkpoint on error exit", exc_info=True)
            self.co_service.update_status(co_id, COStatus.FAILED)
            await tools.disconnect()
            await llm.close()
            if self._on_error:
                self._on_error(str(e))
