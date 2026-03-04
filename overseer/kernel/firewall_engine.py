"""FirewallEngine — the sole decision centre of the AI action firewall.

All security judgements are made here. No other component may make
permission, sandbox, or approval decisions.

Consolidates security logic extracted from:
- ToolService: get_permission, needs_human_approval, override_permission,
               filter_args, _rewrite_path_args, _is_path_readable
- LLMService: SYSTEM_PROMPT (PromptPolicy), parse_decision (fail-safe)
- ExecutionService: loop detection, confidence circuit-breaker,
                    auto-escalation, _check_auto_escalate
- ContextService: build_constraint_hints, check_intent_deviation
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from overseer.config import AppConfig
from overseer.core.enums import ToolPermission
from overseer.core.protocols import (
    HelpRequest,
    LLMDecision,
    TaskPlan,
    ToolCall,
)
from overseer.kernel.perception_bus import PerceptionBus, PerceptionStats

logger = logging.getLogger(__name__)


# ── Verdict dataclass ──


@dataclass
class FirewallVerdict:
    """Result of a firewall evaluation on an LLM decision."""

    action: Literal["allow", "needs_human", "block"]
    reason: str = ""
    needs_preview: bool = False
    # Mutated decision (may have tool_calls cleared on loop detection, etc.)
    decision: Optional[LLMDecision] = None


# ── PolicyStore: dual-layer permission management ──


class PolicyStore:
    """Dual-layer permission storage: AdminPolicy (immutable) + UserPolicy (adaptive).

    Final permission = max(AdminPolicy, UserPolicy), i.e. the stricter one wins.

    Extracted from ToolService.get_permission / needs_human_approval / override_permission.
    """

    _PERMISSION_ORDER = {
        ToolPermission.AUTO: 0,
        ToolPermission.NOTIFY: 1,
        ToolPermission.CONFIRM: 2,
        ToolPermission.APPROVE: 3,
    }

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # AdminPolicy: from config file, immutable by self-adaptation
        self._admin_permissions: Dict[str, str] = dict(config.tool_permissions)
        # UserPolicy: runtime adaptive layer
        self._user_overrides: Dict[str, str] = {}
        # MCP tool mapping (set externally when tools are discovered)
        self._mcp_tools: set[str] = set()

    def set_mcp_tools(self, tool_names: set[str]) -> None:
        """Register which tools come from MCP servers."""
        self._mcp_tools = tool_names

    def get_permission(self, tool_name: str) -> ToolPermission:
        """Get effective permission level: max(AdminPolicy, UserPolicy)."""
        admin_level = self._resolve_admin(tool_name)
        user_level = self._resolve_user(tool_name)
        # Take the stricter (higher) of the two
        if user_level is not None:
            if self._PERMISSION_ORDER.get(user_level, 0) > self._PERMISSION_ORDER.get(admin_level, 0):
                return user_level
        return admin_level

    def _resolve_admin(self, tool_name: str) -> ToolPermission:
        """Resolve admin-level permission from config."""
        perms = self._admin_permissions
        if tool_name in perms:
            level = perms[tool_name]
        elif tool_name in self._mcp_tools:
            level = perms.get("mcp_default", "auto")
        else:
            level = perms.get("default", "confirm")
        try:
            return ToolPermission(level)
        except ValueError:
            return ToolPermission.CONFIRM

    def _resolve_user(self, tool_name: str) -> Optional[ToolPermission]:
        """Resolve user-level override (from self-adaptation)."""
        if tool_name not in self._user_overrides:
            return None
        try:
            return ToolPermission(self._user_overrides[tool_name])
        except ValueError:
            return None

    def override_user_permission(self, tool_name: str, level: str) -> None:
        """Set a runtime user-level permission override (self-adaptation).

        Only affects UserPolicy layer — AdminPolicy remains immutable.
        """
        self._user_overrides[tool_name] = level
        logger.info("UserPolicy override: %s → %s", tool_name, level)

    def needs_human_approval(
        self, tool_name: str, args: Optional[Dict[str, Any]] = None,
        readable_checker: Optional[Any] = None,
    ) -> bool:
        """Check if a tool call requires human approval.

        For file_read/file_list, paths outside the readable_paths whitelist
        are dynamically escalated.
        """
        perm = self.get_permission(tool_name)
        # Dynamic escalation for file_read / file_list outside readable_paths
        if perm in (ToolPermission.AUTO, ToolPermission.NOTIFY) and args:
            if tool_name in ("file_read", "file_list"):
                path = args.get("path", "")
                if path and readable_checker and not readable_checker(path):
                    return True
        return perm in (ToolPermission.CONFIRM, ToolPermission.APPROVE)

    def needs_preview(self, tool_name: str) -> bool:
        """Check if a tool call should show a preview before approval."""
        return self.get_permission(tool_name) == ToolPermission.APPROVE


# ── Sandbox: path rewriting and output isolation ──


class Sandbox:
    """Path rewriting and output directory isolation.

    Extracted from ToolService._rewrite_path_args and _is_path_readable.
    """

    _PATH_KEYS = frozenset({
        "path", "file_path", "filepath", "filename",
        "outputPath", "output_path", "savePath", "save_path",
    })

    def __init__(self, config: AppConfig) -> None:
        self._output_dir = Path(config.context.output_dir)
        self._readable_paths = config.context.readable_paths

    def rewrite_path_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Rewrite file-path arguments to resolve into output_dir.

        Strips directory components and prepends the configured output_dir.
        """
        rewritten = dict(args)
        changed = False
        for key in self._PATH_KEYS:
            if key in rewritten and isinstance(rewritten[key], str):
                p = Path(rewritten[key])
                rewritten[key] = str(self._output_dir / p.name)
                changed = True
        if changed:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        return rewritten

    def is_path_readable(self, path: str) -> bool:
        """Check if a path falls within the readable_paths whitelist."""
        try:
            target = Path(path).resolve()
        except (ValueError, OSError):
            return False

        output_dir = self._output_dir.resolve()

        for allowed in self._readable_paths:
            if allowed in (".", "./"):
                allowed_path = Path.cwd().resolve()
            elif allowed.rstrip("/") == "output":
                allowed_path = output_dir
            else:
                allowed_path = Path(allowed).resolve()

            try:
                target.relative_to(allowed_path)
                return True
            except ValueError:
                continue

        return False


# ── PromptPolicy: security-related system prompt management ──


class PromptPolicy:
    """Manages the security-critical system prompt injected into LLM calls.

    Extracted from LLMService.SYSTEM_PROMPT. The content is security policy,
    not LLM capability — it belongs in the kernel.
    """

    SYSTEM_PROMPT = """\
你是 Overseer（AI 动作防火墙）的认知引擎。

你的职责：根据给定的目标和已积累的上下文，决定下一步该做什么。

重要：请全程使用中文回复。每个回复必须以一个 JSON 决策块结尾，用 ```decision``` 围栏包裹。

输出格式要求（严格遵守）：
1. 分析部分要精简，不超过 200 字，聚焦于可操作的洞察。
2. decision 块中的字段值要简短：title 不超过 20 字，description 不超过 50 字，reflection 不超过 100 字。
3. 禁止在 decision 块中放置大段文本，详细内容放在分析部分。
4. 必须确保 decision 块 JSON 完整闭合，这是最高优先级。

```decision
{
  "next_action": {"title": "简短标题", "description": "简短描述"},
  "tool_calls": [],
  "human_required": false,
  "human_reason": null,
  "options": [],
  "task_complete": false,
  "confidence": 0.8,
  "reflection": "简短反思",
  "help_request": null,
  "subtask_complete": false
}
```

规则：
- 当你需要用户做出选择、提供信息或确认关键操作时，设置 "human_required": true，在 "human_reason" 中解释原因，并提供 "options"。
- 当目标已完全达成时，**不要直接**设置 "task_complete": true。而是先设置 "human_required": true，在 "human_reason" 中提供完整的任务总结报告（包括：完成了哪些工作、关键产出物、遇到的问题及处理方式），并提供 "options": ["确认完成", "补充修改"]。只有当用户确认完成后，才在下一步设置 "task_complete": true。human_reason 中的总结报告不受 200 字限制。
- 使用 "tool_calls" 来调用工具。每个 tool_call 格式为 {"tool": "工具名", "args": {参数}}。tool 字段必须是可用工具列表中的精确工具名。
- **严格遵守工具参数**：只使用 Available Tools 中列出的参数名。不要自行发明或添加未定义的参数（如 "query"），未定义的参数会被系统丢弃。
- **避免重复调用**：如果一个工具已经调用过且结果不理想，不要用相同参数再次调用。应改用其他工具或请求用户帮助。
- 反思时，诚实评估你是否在朝目标推进。

求助协议：
- 当你缺少关键信息、尝试了多种方法仍无法推进时，使用 "help_request" 字段明确说明：
  - missing_information: 你缺少哪些具体信息
  - attempted_approaches: 你已经尝试了哪些方法
  - specific_question: 你想问用户的具体问题
  - suggested_human_actions: 建议用户可以做什么来帮助你
- 请求帮助不是失败，而是高效的问题解决策略。主动求助优于盲目尝试。

子任务协议：
- 如果当前上下文包含 "Current Subtask"，请围绕该子任务工作。
- 当子任务的成功标准已满足时，设置 "subtask_complete": true。
- 不要越界处理其他子任务的内容。
"""

    def get_system_prompt(self) -> str:
        return self.SYSTEM_PROMPT


# ── FirewallEngine: the sole decision centre ──


class FirewallEngine:
    """The sole decision centre of the firewall kernel.

    All security judgements — permission checks, loop detection, confidence
    circuit-breaking, decision parsing, sandbox enforcement — happen here.
    No other component makes security decisions.
    """

    def __init__(self, config: AppConfig, perception: PerceptionBus) -> None:
        self._config = config
        self._perception = perception
        self._policy = PolicyStore(config)
        self._sandbox = Sandbox(config)
        self._prompt_policy = PromptPolicy()

        # Loop detection state
        self._last_tool_sig: str = ""
        self._repeat_count: int = 0
        self._last_tool_names: str = ""
        self._name_repeat_count: int = 0

        # Confidence circuit-breaker settings
        self._low_confidence_window = 3
        self._low_confidence_threshold = 0.3

        # Auto-escalation settings
        self._auto_escalate_threshold = 3
        self._hesitation_threshold = 30.0   # seconds — lower bound for hesitation
        self._absence_threshold = 120.0     # seconds — upper bound; beyond this = user was away

    @property
    def policy(self) -> PolicyStore:
        return self._policy

    @property
    def sandbox(self) -> Sandbox:
        return self._sandbox

    @property
    def hesitation_threshold(self) -> float:
        return self._hesitation_threshold

    @property
    def absence_threshold(self) -> float:
        return self._absence_threshold

    def get_policy_summary(self) -> Dict[str, Any]:
        """Return a read-only summary of the current policy state."""
        admin = self._policy._admin_permissions
        user = self._policy._user_overrides
        return {
            "admin_rules": dict(admin),
            "user_overrides": dict(user),
            "mcp_tools_count": len(self._policy._mcp_tools),
            "output_dir": str(self._sandbox._output_dir),
            "readable_paths": list(self._sandbox._readable_paths),
            "low_confidence_window": self._low_confidence_window,
            "low_confidence_threshold": self._low_confidence_threshold,
            "auto_escalate_threshold": self._auto_escalate_threshold,
            "hesitation_threshold": self._hesitation_threshold,
            "absence_threshold": self._absence_threshold,
        }

    # ── Core decision parsing ──

    def parse_decision(self, response: str) -> LLMDecision:
        """Extract the decision JSON block from LLM response.

        Fail-safe: if parsing fails, returns human_required=True.
        This is a security policy (not an LLM capability).

        Extracted from LLMService.parse_decision().
        """
        # Try to find ```decision ... ``` block
        pattern = r"```decision\s*\n(.*?)\n```"
        match = re.search(pattern, response, re.DOTALL)
        if match:
            try:
                return self._normalize_decision(json.loads(match.group(1)))
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Failed to parse decision block: %s", e)

        # Fallback: try to find any JSON block that looks like a decision
        json_pattern = r"\{[^{}]*\"task_complete\"[^{}]*\}"
        match = re.search(json_pattern, response, re.DOTALL)
        if match:
            try:
                return self._normalize_decision(json.loads(match.group(0)))
            except (json.JSONDecodeError, Exception):
                pass

        # Last resort: fail-safe default — ask for human help
        logger.warning("Could not parse decision from LLM response, requesting human input")
        return LLMDecision(
            human_required=True,
            human_reason="无法确定下一步操作，请查看回复内容并给予指引。",
            options=["继续", "终止"],
        )

    def _normalize_decision(self, data: dict) -> LLMDecision:
        """Build LLMDecision from a dict, normalising tool_calls and help_request."""
        raw_tcs = data.pop("tool_calls", [])
        raw_help = data.pop("help_request", None)
        raw_plan_rev = data.pop("plan_revision", None)
        decision = LLMDecision(**data)
        decision.tool_calls = [ToolCall.from_llm(tc) for tc in raw_tcs]
        if raw_help and isinstance(raw_help, dict):
            decision.help_request = HelpRequest(**raw_help)
        if raw_plan_rev and isinstance(raw_plan_rev, dict):
            decision.plan_revision = TaskPlan(**raw_plan_rev)
        return decision

    # ── Five-layer evaluation pipeline ──

    def evaluate(self, decision: LLMDecision) -> FirewallVerdict:
        """Run the five-layer check pipeline on a parsed decision.

        Layers:
        1. Help request escalation
        2. Confidence circuit-breaker
        3. Loop detection (exact args + same tool name)
        4. Permission check (deferred to per-tool execution)
        5. Task completion protocol

        Returns a FirewallVerdict indicating the action to take.
        """
        stats = self._perception.get_stats()

        # Layer 1: Help request → force HITL
        if decision.help_request and not decision.human_required:
            hr = decision.help_request
            decision.human_required = True
            parts = []
            if hr.specific_question:
                parts.append(f"问题：{hr.specific_question}")
            if hr.attempted_approaches:
                parts.append(f"已尝试：{', '.join(hr.attempted_approaches)}")
            if hr.missing_information:
                parts.append(f"缺少信息：{', '.join(hr.missing_information)}")
            decision.human_reason = "\n".join(parts) if parts else "需要帮助以继续推进。"
            decision.options = (hr.suggested_human_actions or []) + ["跳过此步骤", "终止"]

        # Layer 2: Confidence circuit-breaker
        conf_window = stats.confidence_window
        if (
            len(conf_window) >= self._low_confidence_window
            and all(c < self._low_confidence_threshold for c in conf_window[-self._low_confidence_window:])
            and not decision.human_required
            and not decision.task_complete
        ):
            avg_conf = sum(conf_window[-self._low_confidence_window:]) / self._low_confidence_window
            logger.warning(
                "Low confidence detected: avg=%.2f over last %d steps",
                avg_conf, self._low_confidence_window,
            )
            decision.human_required = True
            decision.human_reason = (
                f"系统检测到连续 {self._low_confidence_window} 步置信度偏低"
                f"（平均 {avg_conf:.2f}），当前策略可能无效。请决定下一步方向。"
            )
            decision.options = ["换一种方式继续", "提供更多信息", "终止"]

        # Layer 3: Loop detection (on tool calls)
        if decision.tool_calls:
            loop_verdict = self._check_loop(decision)
            if loop_verdict:
                return loop_verdict

        # If human is required, return needs_human verdict
        if decision.human_required:
            return FirewallVerdict(
                action="needs_human",
                reason=decision.human_reason or "需要人类决策",
                decision=decision,
            )

        return FirewallVerdict(action="allow", decision=decision)

    def _check_loop(self, decision: LLMDecision) -> Optional[FirewallVerdict]:
        """Loop detection: exact arg repeats + same-tool-name repeats.

        Dynamic thresholds: lower when confidence is low.

        Extracted from ExecutionService.run_loop lines 713-759.
        """
        tool_sig = json.dumps(
            [{"t": tc.tool, "a": tc.args} for tc in decision.tool_calls],
            sort_keys=True,
        )
        if tool_sig == self._last_tool_sig:
            self._repeat_count += 1
        else:
            self._repeat_count = 0
            self._last_tool_sig = tool_sig

        tool_names = json.dumps(sorted(tc.tool for tc in decision.tool_calls))
        if tool_names == self._last_tool_names:
            self._name_repeat_count += 1
        else:
            self._name_repeat_count = 0
            self._last_tool_names = tool_names

        # Dynamic thresholds based on confidence
        stats = self._perception.get_stats()
        conf_window = stats.confidence_window
        avg_conf = (
            sum(conf_window) / len(conf_window) if conf_window else 0.5
        )
        exact_threshold = 2 if avg_conf >= 0.5 else 1
        name_threshold = 3 if avg_conf >= 0.5 else 2

        is_loop = (
            self._repeat_count >= exact_threshold
            or self._name_repeat_count >= name_threshold
        )
        if is_loop:
            reason = "exact args" if self._repeat_count >= exact_threshold else "same tool"
            logger.warning(
                "Loop detected (%s): tool repeated %d times",
                reason, max(self._repeat_count, self._name_repeat_count) + 1,
            )
            decision.tool_calls = []
            decision.human_required = True
            decision.human_reason = "检测到重复工具调用，工具可能未返回有效数据。请选择下一步操作。"
            decision.options = ["换一种方式继续", "终止"]
            return FirewallVerdict(
                action="needs_human",
                reason=f"Loop detected ({reason})",
                decision=decision,
            )
        return None

    # ── Per-tool permission checks ──

    def check_tool_permission(
        self, tc: ToolCall,
    ) -> tuple[bool, bool]:
        """Check if a single tool call needs human approval.

        Returns (needs_approval, needs_preview).
        """
        needs_approval = self._policy.needs_human_approval(
            tc.tool, tc.args,
            readable_checker=self._sandbox.is_path_readable,
        )
        needs_preview = self._policy.needs_preview(tc.tool)
        return needs_approval, needs_preview

    # ── Arg filtering ──

    def filter_args(
        self, tool_name: str, args: Dict[str, Any], tool_schema: Optional[dict] = None,
    ) -> tuple[Dict[str, Any], List[str]]:
        """Filter tool args to only include schema-defined parameters.

        Returns (filtered_args, removed_keys).

        Extracted from ToolService.filter_args().
        """
        if tool_schema is None:
            return args, []
        valid_props = tool_schema.get("properties", {})
        if not valid_props:
            return args, []
        filtered = {k: v for k, v in args.items() if k in valid_props}
        removed = [k for k in args if k not in valid_props]
        return filtered, removed

    # ── Sandbox ──

    def sandbox_args(self, tc: ToolCall) -> ToolCall:
        """Rewrite tool call arguments through the sandbox."""
        rewritten = self._sandbox.rewrite_path_args(tc.args)
        return ToolCall(tool=tc.tool, args=rewritten)

    # ── Self-adaptation (reads PerceptionBus stats, makes judgements) ──

    def should_escalate(self, tool: str) -> Optional[str]:
        """Check if consecutive rejections warrant permission escalation.

        Returns the new permission level string, or None.

        Extracted from ExecutionService._check_auto_escalate().
        """
        stats = self._perception.get_stats()
        count = stats.consecutive_rejects.get(tool, 0)
        if count >= self._auto_escalate_threshold:
            logger.warning(
                "User rejected '%s' %d consecutive times — escalating permission",
                tool, count,
            )
            self._policy.override_user_permission(tool, "approve")
            return "approve"
        return None

    def should_force_hitl(self) -> Optional[str]:
        """Check if confidence is low enough to force human-in-the-loop.

        Returns a reason string, or None.
        """
        stats = self._perception.get_stats()
        conf_window = stats.confidence_window
        if (
            len(conf_window) >= self._low_confidence_window
            and all(c < self._low_confidence_threshold
                    for c in conf_window[-self._low_confidence_window:])
        ):
            avg = sum(conf_window[-self._low_confidence_window:]) / self._low_confidence_window
            return (
                f"连续 {self._low_confidence_window} 步置信度偏低"
                f"（平均 {avg:.2f}），当前策略可能无效。"
            )
        return None

    # ── Constraint building (reads context + perception data) ──

    def build_constraints(self, co_context: dict) -> List[str]:
        """Generate pre-emptive constraint warnings from past failures.

        Extracted from ContextService.build_constraint_hints().
        """
        hints: List[str] = []

        # From working memory
        working_mem = co_context.get("working_memory")
        if working_mem and working_mem.get("failed_approaches"):
            for fa in working_mem["failed_approaches"]:
                hints.append(fa)

        # From recent findings: extract errors and avoidance signals
        findings = co_context.get("accumulated_findings", [])
        seen_errors: set[str] = set()
        for f in findings:
            value = f.get("value", "")
            key = f.get("key", "")
            # Error results
            if value.startswith("[error]") and key.startswith("tool:"):
                tool_name = key[5:]  # strip "tool:" prefix
                error_sig = f"{tool_name}:{value[:60]}"
                if error_sig not in seen_errors:
                    seen_errors.add(error_sig)
                    hints.append(f"Tool '{tool_name}' previously failed: {value[8:80]}...")
            # Tool avoidance signals from perception
            if key == "perception:tool_avoidance":
                hints.append(value)
            # Same-as-previous warnings
            if "[SAME as previous call" in value:
                tool_name = key[5:] if key.startswith("tool:") else key
                hints.append(f"Calling '{tool_name}' with the same args returned identical results. Try different parameters.")

        return hints[:10]  # cap to avoid prompt bloat

    def check_deviation(
        self, intent_description: str, tool_results: list[dict],
    ) -> Optional[str]:
        """Check if tool results deviate from the stated intent.

        Returns a deviation warning string, or None.

        Extracted from ContextService.check_intent_deviation().
        """
        if not intent_description or not tool_results:
            return None

        all_errors = all(r.get("status") == "error" for r in tool_results)
        if all_errors:
            return (
                f"Intent was '{intent_description}', but all tool calls failed. "
                f"The current approach is not working."
            )

        all_empty = all(
            PerceptionBus.classify_result("", r) == "empty" for r in tool_results
        )
        if all_empty:
            return (
                f"Intent was '{intent_description}', but all tools returned empty results. "
                f"The data or resource may not exist."
            )

        error_count = sum(1 for r in tool_results if r.get("status") == "error")
        if 0 < error_count < len(tool_results):
            failed = [r.get("tool", "?") for r in tool_results if r.get("status") == "error"]
            return (
                f"Intent was '{intent_description}', but {error_count}/{len(tool_results)} "
                f"tool calls failed ({', '.join(failed)}). Review partial results."
            )

        return None

    # ── PromptPolicy ──

    def get_system_prompt(self) -> str:
        """Return the security-constrained system prompt for LLM calls."""
        return self._prompt_policy.get_system_prompt()

    # ── Loop state management (for checkpoint/restore) ──

    def get_loop_state(self) -> Dict[str, Any]:
        """Serialise loop detection state for checkpointing."""
        return {
            "last_tool_sig": self._last_tool_sig,
            "repeat_count": self._repeat_count,
            "last_tool_names": self._last_tool_names,
            "name_repeat_count": self._name_repeat_count,
        }

    def restore_loop_state(self, state: Dict[str, Any]) -> None:
        """Restore loop detection state from checkpoint."""
        self._last_tool_sig = state.get("last_tool_sig", "")
        self._repeat_count = state.get("repeat_count", 0)
        self._last_tool_names = state.get("last_tool_names", "")
        self._name_repeat_count = state.get("name_repeat_count", 0)
