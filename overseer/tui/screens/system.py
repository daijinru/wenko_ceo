"""System screen — full-screen page for viewing kernel and plugin status."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

logger = logging.getLogger(__name__)

# Component types for list items
KERNEL_COMPONENTS = ["FirewallEngine", "HumanGate", "PerceptionBus", "PluginRegistry"]
PLUGIN_PROTOCOLS = ["LLMPlugin", "ToolPlugin", "PlanPlugin", "MemoryPlugin", "ContextPlugin"]


class SectionHeader(ListItem):
    """A non-selectable section header in the list (KERNEL / PLUGINS)."""

    def __init__(self, title: str) -> None:
        super().__init__(classes="section-header")
        self._title = title

    def compose(self) -> ComposeResult:
        yield Label(
            f"[bold reverse] {self._title} [/bold reverse]",
            classes="item-label",
        )


class SystemListItem(ListItem):
    """A single kernel component or plugin entry in the list."""

    def __init__(self, name: str, kind: str, impl_name: str = "") -> None:
        super().__init__(classes="item-card")
        self.component_name = name
        self.kind = kind  # "kernel" or "plugin"
        self._impl_name = impl_name

    def compose(self) -> ComposeResult:
        kind_label = (
            "[dim]kernel[/dim]" if self.kind == "kernel" else "[dim]plugin[/dim]"
        )
        impl_text = f"  [dim]→ {self._impl_name}[/dim]" if self._impl_name else ""
        yield Label(
            f"[bold]{self.component_name}[/bold]  {kind_label}{impl_text}",
            classes="item-label",
        )


class SystemScreen(Screen):
    """Full-screen page for viewing kernel components and registered plugins."""

    BINDINGS = [
        ("j", "next_item", "Next"),
        ("k", "prev_item", "Prev"),
        ("r", "reset_stats", "Reset Stats"),
        ("y", "copy_info", "Copy"),
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
    ]

    def __init__(
        self,
        kernel_data: Dict[str, Any],
        plugin_data: Dict[str, str],
    ) -> None:
        """Create the system screen.

        Args:
            kernel_data: Runtime data from kernel components.
            plugin_data: Protocol name -> implementation class name mapping.
        """
        super().__init__()
        self._kernel_data = kernel_data
        self._plugin_data = plugin_data
        self._selected_name: str | None = None
        self._selected_kind: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="system-container"):
            with Vertical(id="system-list-panel"):
                yield Static("System", classes="panel-title")
                yield Static("", id="system-count-label", classes="filter-label")
                yield ListView(id="system-listview")
            with Vertical(id="system-detail-panel"):
                yield Static(
                    "[dim]Select a component to view details[/dim]",
                    id="system-detail-header",
                )
                with VerticalScroll(id="system-detail-scroll"):
                    yield Static("", id="system-detail-content")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_list()

    def _refresh_list(self) -> None:
        listview = self.query_one("#system-listview", ListView)
        listview.clear()

        # Kernel section
        listview.append(SectionHeader("KERNEL"))
        for name in KERNEL_COMPONENTS:
            listview.append(SystemListItem(name, "kernel"))

        # Plugins section
        listview.append(SectionHeader("PLUGINS"))
        for proto_name in PLUGIN_PROTOCOLS:
            impl_name = self._plugin_data.get(proto_name, "not registered")
            listview.append(SystemListItem(proto_name, "plugin", impl_name))

        kernel_count = len(KERNEL_COMPONENTS)
        plugin_count = len(self._plugin_data)
        self.query_one("#system-count-label", Static).update(
            f"Kernel: [bold]{kernel_count}[/bold]  "
            f"Plugins: [bold]{plugin_count}[/bold]"
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, SystemListItem):
            self._selected_name = item.component_name
            self._selected_kind = item.kind
            if item.kind == "kernel":
                self._show_kernel_detail(item.component_name)
            else:
                self._show_plugin_detail(item.component_name)

    # ── Kernel detail renderers ──

    def _show_kernel_detail(self, name: str) -> None:
        header = self.query_one("#system-detail-header", Static)
        content = self.query_one("#system-detail-content", Static)

        header.update(
            f"[bold]{name}[/bold]  [dim]kernel[/dim]  "
            f"[dim]y: copy  r: reset stats  q: back[/dim]"
        )

        renderer = {
            "FirewallEngine": self._render_firewall,
            "HumanGate": self._render_human_gate,
            "PerceptionBus": self._render_perception,
            "PluginRegistry": self._render_registry,
        }.get(name)

        if renderer:
            content.update(renderer())
        else:
            content.update(f"[dim]No detail available for {name}[/dim]")

    def _render_firewall(self) -> str:
        fw = self._kernel_data.get("firewall", {})
        policy = fw.get("policy_summary", {})
        loop = fw.get("loop_state", {})

        lines = [
            "[bold underline]FirewallEngine[/bold underline]",
            "[dim]The sole decision centre — all security judgements happen here.[/dim]",
            "",
            "[bold underline]Five-Layer Pipeline:[/bold underline]",
            "  L1  Parameter Filtering    (Schema validation)",
            "  L2  Behaviour Interception  (Loop detection)",
            "  L3  Permission Grading      (4-tier AUTO→APPROVE)",
            "  L4  Output Sandbox          (Path rewriting)",
            "  L5  Meta-cognition Breaker  (Confidence circuit-breaker)",
            "",
            "[bold underline]Loop Detection State:[/bold underline]",
            f"  Exact-args repeat count:  {loop.get('repeat_count', 0)}",
            f"  Same-tool repeat count:   {loop.get('name_repeat_count', 0)}",
            "",
            "[bold underline]PolicyStore (Admin):[/bold underline]",
        ]

        admin_rules = policy.get("admin_rules", {})
        if admin_rules:
            for tool, perm in sorted(admin_rules.items()):
                lines.append(f"  {tool}: [bold]{perm}[/bold]")
        else:
            lines.append("  [dim]No admin rules configured[/dim]")

        user_overrides = policy.get("user_overrides", {})
        lines.append("")
        lines.append("[bold underline]PolicyStore (User Overrides):[/bold underline]")
        if user_overrides:
            for tool, perm in sorted(user_overrides.items()):
                lines.append(f"  {tool}: [bold]{perm}[/bold]")
        else:
            lines.append("  [dim]No user overrides[/dim]")

        lines.extend([
            "",
            "[bold underline]Sandbox:[/bold underline]",
            f"  Output dir:     {policy.get('output_dir', '—')}",
            f"  Readable paths: {', '.join(policy.get('readable_paths', [])) or '—'}",
            "",
            "[bold underline]Thresholds:[/bold underline]",
            f"  Low confidence window:    {policy.get('low_confidence_window', '—')}",
            f"  Low confidence threshold: {policy.get('low_confidence_threshold', '—')}",
            f"  Auto-escalate threshold:  {policy.get('auto_escalate_threshold', '—')}",
            f"  Hesitation threshold:     {policy.get('hesitation_threshold', '—')}s",
            f"  MCP tools registered:     {policy.get('mcp_tools_count', 0)}",
        ])

        return "\n".join(lines)

    def _render_human_gate(self) -> str:
        hg = self._kernel_data.get("human_gate", {})

        lines = [
            "[bold underline]HumanGate[/bold underline]",
            "[dim]The sole human-machine communication channel.[/dim]",
            "",
            "[bold underline]State:[/bold underline]",
            f"  Consecutive stops: {hg.get('consecutive_stops', 0)}",
            f"  Pending request:   {'[yellow]Yes[/yellow]' if hg.get('pending') else '[green]No[/green]'}",
            "",
            "[bold underline]Multi-stage Abort Protocol:[/bold underline]",
            "  1st stop → Gentle stop (pause execution)",
            "  2nd stop → Force abort (terminate CO)",
            "",
            "[bold underline]Intent Detection:[/bold underline]",
            "  APPROVE:          approve, yes, ok, 批准, 同意, ...",
            "  REJECT:           reject, no, deny, 拒绝, 不行, ...",
            "  ABORT:            abort, stop, quit, 终止, 停止, 取消, ...",
            "  CONFIRM_COMPLETE: confirm, done, lgtm, 确认完成, ...",
            "  IMPLICIT_STOP:    enough, finish, 够了, ...",
            "  FREETEXT:         (anything else)",
        ]

        return "\n".join(lines)

    def _render_perception(self) -> str:
        perc = self._kernel_data.get("perception", {})
        stats = perc.get("stats", {})

        lines = [
            "[bold underline]PerceptionBus[/bold underline]",
            "[dim]Pure signal recorder — collects, classifies, never judges.[/dim]",
            "",
            "[bold underline]Confidence Window:[/bold underline]",
        ]

        conf_window = stats.get("confidence_window", [])
        if conf_window:
            conf_str = "  " + "  ".join(f"{c:.2f}" for c in conf_window)
            avg = sum(conf_window) / len(conf_window)
            lines.append(conf_str)
            lines.append(f"  Average: {avg:.2f}  (last {len(conf_window)} steps)")
        else:
            lines.append("  [dim]No confidence data yet[/dim]")

        lines.extend([
            "",
            f"[bold underline]Stagnation Count:[/bold underline]  {stats.get('stagnation_count', 0)}",
            "",
        ])

        approval_summary = perc.get("approval_summary", "")
        lines.append("[bold underline]Approval Statistics:[/bold underline]")
        if approval_summary:
            lines.append(approval_summary)
        else:
            lines.append("  [dim]No approval data yet[/dim]")

        return "\n".join(lines)

    def _render_registry(self) -> str:
        lines = [
            "[bold underline]PluginRegistry[/bold underline]",
            "[dim]Manages plugin registration, retrieval, and lifecycle.[/dim]",
            "",
            "[bold underline]Registered Plugins:[/bold underline]",
        ]

        if self._plugin_data:
            for proto, impl in sorted(self._plugin_data.items()):
                lines.append(
                    f"  [bold]{proto}[/bold]  →  {impl}"
                )
        else:
            lines.append("  [dim]No plugins registered[/dim]")

        lines.extend([
            "",
            f"  Total: [bold]{len(self._plugin_data)}[/bold] / 5 protocols",
        ])

        return "\n".join(lines)

    # ── Plugin detail renderer ──

    def _show_plugin_detail(self, proto_name: str) -> None:
        header = self.query_one("#system-detail-header", Static)
        content = self.query_one("#system-detail-content", Static)

        impl_name = self._plugin_data.get(proto_name, "not registered")
        header.update(
            f"[bold]{proto_name}[/bold]  [dim]plugin → {impl_name}[/dim]  "
            f"[dim]y: copy  q: back[/dim]"
        )

        plugin_info = self._kernel_data.get("plugins", {}).get(proto_name, {})

        lines = [
            f"[bold underline]{proto_name}[/bold underline]",
            f"[bold underline]Implementation:[/bold underline]  {impl_name}",
        ]

        desc = _PLUGIN_DESCRIPTIONS.get(proto_name, "")
        if desc:
            lines.extend(["", f"[dim]{desc}[/dim]"])

        # Plugin-specific extra info
        if proto_name == "ToolPlugin":
            tool_count = plugin_info.get("tool_count", 0)
            server_count = plugin_info.get("server_count", 0)
            lines.extend([
                "",
                "[bold underline]Runtime Status:[/bold underline]",
                f"  Discovered tools:  {tool_count}",
                f"  MCP servers:       {server_count}",
            ])

        methods = _PLUGIN_METHODS.get(proto_name, [])
        if methods:
            lines.extend([
                "",
                "[bold underline]Protocol Methods:[/bold underline]",
            ])
            for method in methods:
                lines.append(f"  • {method}")

        lines.extend([
            "",
            "[bold underline]Boundary:[/bold underline]",
            f"  {_PLUGIN_BOUNDARIES.get(proto_name, '')}",
        ])

        content.update("\n".join(lines))

    # ── Navigation ──

    def action_next_item(self) -> None:
        listview = self.query_one("#system-listview", ListView)
        if listview.index is None:
            if len(listview.children) > 0:
                listview.index = 0
        elif listview.index < len(listview.children) - 1:
            listview.index += 1
        # Skip section headers
        self._skip_headers(listview, direction=1)
        self._emit_selected(listview)

    def action_prev_item(self) -> None:
        listview = self.query_one("#system-listview", ListView)
        if listview.index is None:
            if len(listview.children) > 0:
                listview.index = 0
        elif listview.index > 0:
            listview.index -= 1
        # Skip section headers
        self._skip_headers(listview, direction=-1)
        self._emit_selected(listview)

    def _skip_headers(self, listview: ListView, direction: int) -> None:
        """Skip section headers when navigating."""
        items = list(listview.children)
        idx = listview.index
        if idx is None:
            return
        while 0 <= idx < len(items) and isinstance(items[idx], SectionHeader):
            idx += direction
        if 0 <= idx < len(items):
            listview.index = idx

    def _emit_selected(self, listview: ListView) -> None:
        if listview.index is not None:
            items = list(listview.children)
            if 0 <= listview.index < len(items):
                item = items[listview.index]
                if isinstance(item, SystemListItem):
                    self._selected_name = item.component_name
                    self._selected_kind = item.kind
                    if item.kind == "kernel":
                        self._show_kernel_detail(item.component_name)
                    else:
                        self._show_plugin_detail(item.component_name)

    # ── Reset Stats ──

    def action_reset_stats(self) -> None:
        """Reset perception statistics (requires confirmation)."""
        from overseer.tui.screens.confirm import ConfirmScreen

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.app.post_message(ResetStatsRequest())
            self.notify("Perception statistics reset")
            # Refresh detail if PerceptionBus is selected
            if self._selected_name == "PerceptionBus":
                self._show_kernel_detail("PerceptionBus")

        self.app.push_screen(
            ConfirmScreen(
                "Reset Statistics",
                "Reset all PerceptionBus statistics? This clears approval counts, "
                "confidence history, and stagnation data.",
            ),
            callback=on_confirm,
        )

    # ── Copy ──

    def action_copy_info(self) -> None:
        if self._selected_name is None:
            self.notify("No component selected", severity="warning")
            return

        content = self.query_one("#system-detail-content", Static)
        # Get plain text from the rendered content
        text = content.render().plain

        from overseer.tui.widgets.execution_log import _copy_to_system_clipboard
        if _copy_to_system_clipboard(text):
            self.notify("Copied to clipboard")
        else:
            self.app.copy_to_clipboard(text)
            self.notify("Copied to clipboard (OSC 52)")

    # ── Back ──

    def action_go_back(self) -> None:
        self.app.pop_screen()


# ── Internal message for reset ──

from textual.message import Message  # noqa: E402


class ResetStatsRequest(Message):
    """Request to reset PerceptionBus stats (handled by OverseerApp)."""
    pass


# ── Plugin metadata ──

_PLUGIN_DESCRIPTIONS = {
    "LLMPlugin": "Pure reasoning capability. No security prompts, no decision parsing.",
    "ToolPlugin": "Pure tool discovery and execution. No permission checks, no path sandboxing.",
    "PlanPlugin": "Task decomposition and subtask management. Optional plugin.",
    "MemoryPlugin": "Long-term memory storage and retrieval. Pure data capability.",
    "ContextPlugin": "Context assembly and compression. No perception classification.",
}

_PLUGIN_BOUNDARIES = {
    "LLMPlugin": "Only reasoning — security fallback injected by kernel.",
    "ToolPlugin": "Only execution — permissions and sandbox enforced by kernel.",
    "PlanPlugin": "Pure strategy suggestions — no security impact.",
    "MemoryPlugin": "Pure data — perception conclusions stored by kernel.",
    "ContextPlugin": "Assembles kernel signals + plugin data into prompts.",
}

_PLUGIN_METHODS = {
    "LLMPlugin": [
        "call(prompt, tools, system_prompt, stream, on_chunk)",
        "reflect(context)",
        "plan(prompt)",
        "parse_plan(response)",
        "compress(prompt)",
        "parse_working_memory(response)",
        "checkpoint(prompt)",
        "parse_checkpoint(response)",
        "close()",
    ],
    "ToolPlugin": [
        "connect()",
        "disconnect()",
        "list_tools()",
        "list_tools_detailed()",
        "get_tool_schema(tool_name)",
        "execute(tool_call)",
        "drain_stderr()",
    ],
    "PlanPlugin": [
        "generate_plan(co, memories, available_tools)",
        "store_plan(co, plan)",
        "get_current_subtask(co)",
        "advance_subtask(co, result_summary)",
        "all_subtasks_done(co)",
        "checkpoint_reflect(co)",
        "get_plan_progress_text(co)",
    ],
    "MemoryPlugin": [
        "save(category, content, tags, source_co_id)",
        "retrieve_as_text(query, limit)",
        "# extract_and_save → moved to MemoryExtractor (orchestration layer)",
    ],
    "ContextPlugin": [
        "build_prompt(co, memories, available_tools, ...)",
        "merge_step_result(co, step_number, key, value)",
        "merge_tool_result(co, step_number, tool_name, result, ...)",
        "merge_reflection(co, reflection)",
        "add_artifact(co, artifact_path)",
        "compress_if_needed(co, max_chars)",
        "compress_to_working_memory(co, llm_service)",
        "restore_tool_outputs(outputs)",
    ],
}
