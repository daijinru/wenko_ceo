"""Plugin Protocol definitions — interfaces for replaceable capabilities.

Plugins provide capabilities (reasoning, tool execution, planning, memory, context).
They contain NO security logic — permissions, sandboxing, and approval decisions
belong exclusively to the kernel (FirewallEngine).

These use Python's Protocol (structural subtyping) so existing Services
satisfy the interface without explicit inheritance.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from overseer.core.protocols import (
    LLMResponse,
    Subtask,
    TaskPlan,
    TokenUsage,
    ToolCall,
    WorkingMemory,
)


@runtime_checkable
class LLMPlugin(Protocol):
    """Pure reasoning capability. No security prompts, no decision parsing."""

    async def call(
        self,
        prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse: ...

    async def reflect(self, context: dict) -> str: ...

    async def plan(self, prompt: str) -> str: ...

    def parse_plan(self, response: str) -> Optional[TaskPlan]: ...

    async def compress(self, prompt: str) -> str: ...

    def parse_working_memory(self, response: str) -> Optional[WorkingMemory]: ...

    async def checkpoint(self, prompt: str) -> str: ...

    def parse_checkpoint(self, response: str) -> Dict[str, Any]: ...

    def last_usage(self) -> TokenUsage: ...

    async def close(self) -> None: ...


@runtime_checkable
class ToolPlugin(Protocol):
    """Pure tool discovery and execution. No permission checks, no path sandboxing."""

    async def connect(self) -> List[str]: ...

    async def disconnect(self) -> None: ...

    def list_tools(self) -> List[Dict[str, Any]]: ...

    def list_tools_detailed(self) -> List[Dict[str, Any]]: ...

    def get_tool_schema(self, tool_name: str) -> Optional[Dict[str, Any]]: ...

    async def execute(self, tool_call: ToolCall) -> Dict[str, Any]: ...

    def drain_stderr(self) -> List[str]: ...


@runtime_checkable
class PlanPlugin(Protocol):
    """Task decomposition and subtask management. Optional plugin."""

    async def generate_plan(
        self,
        co: Any,
        memories: list[str],
        available_tools: list[dict],
    ) -> Optional[TaskPlan]: ...

    def store_plan(self, co: Any, plan: TaskPlan) -> None: ...

    def get_current_subtask(self, co: Any) -> Optional[Subtask]: ...

    def advance_subtask(self, co: Any, result_summary: str = "") -> Optional[Subtask]: ...

    def all_subtasks_done(self, co: Any) -> bool: ...

    async def checkpoint_reflect(self, co: Any) -> Optional[TaskPlan]: ...

    def get_plan_progress_text(self, co: Any) -> str: ...


@runtime_checkable
class MemoryPlugin(Protocol):
    """Long-term memory storage and retrieval. Pure data capability."""

    def save(
        self,
        category: str,
        content: str,
        tags: list[str] | None = None,
        source_co_id: str | None = None,
    ) -> Any: ...

    def retrieve_as_text(self, query: str, limit: int = 5) -> List[str]: ...


@runtime_checkable
class ContextPlugin(Protocol):
    """Context assembly and compression. No perception classification."""

    def build_prompt(
        self,
        co: Any,
        memories: list[str] | None = None,
        available_tools: list[dict] | None = None,
        elapsed_seconds: float = 0.0,
        max_steps: int = 0,
        constraint_hints: list[str] | None = None,
    ) -> str: ...

    def merge_step_result(
        self, co: Any, step_number: int, key: str, value: str
    ) -> Dict[str, Any]: ...

    def merge_tool_result(
        self,
        co: Any,
        step_number: int,
        tool_name: str,
        result: str,
        raw_result: dict | None = None,
        tool_args: dict | None = None,
    ) -> Dict[str, Any]: ...

    def merge_reflection(self, co: Any, reflection: str) -> Dict[str, Any]: ...

    def add_artifact(self, co: Any, artifact_path: str) -> None: ...

    @staticmethod
    def summarize_tool_result(
        tool_name: str, result: dict, max_chars: int = 1500
    ) -> str: ...

    @staticmethod
    def estimate_tokens(text: str) -> int: ...

    def compress_if_needed(self, co: Any, max_tokens: int = 0) -> bool: ...

    async def compress_to_working_memory(self, co: Any, llm_service: Any) -> Optional[WorkingMemory]: ...

    def restore_tool_outputs(self, outputs: Dict[str, str]) -> None: ...
