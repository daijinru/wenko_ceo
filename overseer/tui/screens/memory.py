"""Memory screen — full-screen page for browsing and managing memories."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from overseer.models.memory import Memory
from overseer.services.memory_service import MemoryService
from overseer.tui.screens.confirm import ConfirmScreen
from overseer.tui.screens.memory_edit import MemoryEditScreen

CATEGORY_STYLES = {
    "preference": "[underline]preference[/underline]",
    "lesson": "[bold italic]lesson[/bold italic]",
    "domain_knowledge": "[bold]domain_knowledge[/bold]",
    "decision_pattern": "[italic]decision_pattern[/italic]",
}

MAX_PREVIEW_LEN = 60


class MemoryListItem(ListItem):
    """A single Memory entry in the list."""

    def __init__(self, memory: Memory) -> None:
        super().__init__(classes="item-card")
        self.memory_id = memory.id
        self._memory = memory

    def compose(self) -> ComposeResult:
        cat = self._memory.category or "unknown"
        styled_cat = CATEGORY_STYLES.get(cat, f"[dim]{cat}[/dim]")
        content = self._memory.content or ""
        preview = content[:MAX_PREVIEW_LEN] + "…" if len(content) > MAX_PREVIEW_LEN else content
        preview = preview.replace("\n", " ")
        created = self._memory.created_at.strftime("%m-%d %H:%M") if self._memory.created_at else ""
        accesses = self._memory.access_count or 0
        meta = f"[dim]{created}[/dim]  [dim italic]×{accesses}[/dim italic]" if accesses else f"[dim]{created}[/dim]"
        yield Label(f"{styled_cat}  {meta}\n{preview}", classes="item-label")


class MemoryScreen(Screen):
    """Full-screen page for viewing, creating, editing and deleting memories."""

    BINDINGS = [
        ("j", "next_memory", "Next"),
        ("k", "prev_memory", "Prev"),
        ("n", "new_memory", "New"),
        ("e", "edit_memory", "Edit"),
        ("d", "delete_memory", "Delete"),
        ("y", "copy_memory", "Copy"),
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._memory_service = MemoryService()
        self._memories: List[Memory] = []
        self._selected_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="memory-container"):
            with Vertical(id="memory-list-panel"):
                yield Static("Memories", classes="panel-title")
                yield Static("", id="memory-count-label", classes="filter-label")
                yield ListView(id="memory-listview")
            with Vertical(id="memory-detail-panel"):
                yield Static("[dim]Select a memory to view details[/dim]", id="memory-detail-header")
                with VerticalScroll(id="memory-detail-scroll"):
                    yield Static("", id="memory-detail-content")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_list()

    def _refresh_list(self) -> None:
        self._memories = self._memory_service.list_all()
        listview = self.query_one("#memory-listview", ListView)
        listview.clear()
        for mem in self._memories:
            listview.append(MemoryListItem(mem))
        self.query_one("#memory-count-label", Static).update(
            f"Total: [bold]{len(self._memories)}[/bold]"
        )
        if self._selected_id:
            found = any(m.id == self._selected_id for m in self._memories)
            if not found:
                self._selected_id = None
                self._show_detail(None)
            else:
                mem = next((m for m in self._memories if m.id == self._selected_id), None)
                self._show_detail(mem)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MemoryListItem):
            self._selected_id = item.memory_id
            mem = next((m for m in self._memories if m.id == item.memory_id), None)
            self._show_detail(mem)

    def _show_detail(self, mem: Memory | None) -> None:
        header = self.query_one("#memory-detail-header", Static)
        content = self.query_one("#memory-detail-content", Static)

        if mem is None:
            header.update("[dim]Select a memory to view details[/dim]")
            content.update("")
            return

        cat = mem.category or "unknown"
        styled_cat = CATEGORY_STYLES.get(cat, f"[dim]{cat}[/dim]")
        created = mem.created_at.strftime("%Y-%m-%d %H:%M:%S") if mem.created_at else "—"
        updated = mem.updated_at.strftime("%Y-%m-%d %H:%M:%S") if mem.updated_at else "—"
        source = mem.source_co_id[:8] if mem.source_co_id else "—"
        tags = ", ".join(mem.relevance_tags) if mem.relevance_tags else "—"
        accesses = mem.access_count or 0

        header.update(
            f"[bold]Memory Detail[/bold]  {styled_cat}  [dim]n: new  e: edit  d: delete  y: copy  q: back[/dim]"
        )
        detail_text = (
            f"[bold underline]Category:[/bold underline]  {styled_cat}\n"
            f"[bold underline]Created:[/bold underline]   {created}\n"
            f"[bold underline]Updated:[/bold underline]   {updated}\n"
            f"[bold underline]Accesses:[/bold underline]  {accesses}\n"
            f"[bold underline]Source CO:[/bold underline] {source}\n"
            f"[bold underline]Tags:[/bold underline]      {tags}\n"
            f"\n[bold underline]Content:[/bold underline]\n{mem.content}"
        )
        content.update(detail_text)

    # ── Navigation ──

    def action_next_memory(self) -> None:
        listview = self.query_one("#memory-listview", ListView)
        if listview.index is None:
            if len(listview.children) > 0:
                listview.index = 0
        elif listview.index < len(listview.children) - 1:
            listview.index += 1
        self._emit_selected(listview)

    def action_prev_memory(self) -> None:
        listview = self.query_one("#memory-listview", ListView)
        if listview.index is None:
            if len(listview.children) > 0:
                listview.index = 0
        elif listview.index > 0:
            listview.index -= 1
        self._emit_selected(listview)

    def _emit_selected(self, listview: ListView) -> None:
        if listview.index is not None:
            items = list(listview.children)
            if 0 <= listview.index < len(items):
                item = items[listview.index]
                if isinstance(item, MemoryListItem):
                    self._selected_id = item.memory_id
                    mem = next((m for m in self._memories if m.id == item.memory_id), None)
                    self._show_detail(mem)

    # ── Create ──

    def action_new_memory(self) -> None:
        def on_result(result: Optional[Dict[str, Any]]) -> None:
            if result is None:
                return
            self._memory_service.save(
                category=result["category"],
                content=result["content"],
                tags=result["tags"],
            )
            self.notify("Memory created")
            self._refresh_list()

        self.app.push_screen(MemoryEditScreen(), callback=on_result)

    # ── Edit ──

    def action_edit_memory(self) -> None:
        if self._selected_id is None:
            self.notify("No memory selected", severity="warning")
            return

        mem = next((m for m in self._memories if m.id == self._selected_id), None)
        if mem is None:
            return

        existing = {
            "category": mem.category,
            "content": mem.content,
            "tags": mem.relevance_tags or [],
        }
        memory_id = mem.id

        def on_result(result: Optional[Dict[str, Any]]) -> None:
            if result is None:
                return
            self._memory_service.update(
                memory_id,
                category=result["category"],
                content=result["content"],
                tags=result["tags"],
            )
            self.notify("Memory updated")
            self._refresh_list()

        self.app.push_screen(MemoryEditScreen(existing=existing), callback=on_result)

    # ── Delete ──

    def action_delete_memory(self) -> None:
        if self._selected_id is None:
            self.notify("No memory selected", severity="warning")
            return

        mem = next((m for m in self._memories if m.id == self._selected_id), None)
        if mem is None:
            return

        preview = (mem.content or "")[:40]

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            if self._memory_service.delete(mem.id):
                self.notify("Memory deleted")
                self._refresh_list()
            else:
                self.notify("Failed to delete memory", severity="error")

        self.app.push_screen(
            ConfirmScreen("Delete Memory", f'Delete memory "{preview}…"?'),
            callback=on_confirm,
        )

    # ── Copy ──

    def action_copy_memory(self) -> None:
        if self._selected_id is None:
            self.notify("No memory selected", severity="warning")
            return

        mem = next((m for m in self._memories if m.id == self._selected_id), None)
        if mem is None:
            return

        cat = mem.category or "unknown"
        created = mem.created_at.strftime("%Y-%m-%d %H:%M:%S") if mem.created_at else "—"
        updated = mem.updated_at.strftime("%Y-%m-%d %H:%M:%S") if mem.updated_at else "—"
        source = mem.source_co_id or "—"
        tags = ", ".join(mem.relevance_tags) if mem.relevance_tags else "—"
        accesses = mem.access_count or 0

        text = (
            f"Category:  {cat}\n"
            f"Created:   {created}\n"
            f"Updated:   {updated}\n"
            f"Accesses:  {accesses}\n"
            f"Source CO: {source}\n"
            f"Tags:      {tags}\n"
            f"\n{mem.content}"
        )

        from overseer.tui.widgets.execution_log import _copy_to_system_clipboard
        if _copy_to_system_clipboard(text):
            self.notify("Memory copied to clipboard")
        else:
            self.app.copy_to_clipboard(text)
            self.notify("Memory copied to clipboard (OSC 52)")

    # ── Back ──

    def action_go_back(self) -> None:
        self.app.pop_screen()
