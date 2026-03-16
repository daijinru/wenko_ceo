"""Welcome screen — CRT boot sequence displayed on startup."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Static

from overseer.tui.theme import WELCOME_BOOT_LINES, WELCOME_HEADER

# Delay between each boot line (seconds).
_BOOT_LINE_DELAY = 0.45

# Blink interval for the "PRESS ENTER" prompt (seconds).
_BLINK_INTERVAL = 0.7

# The prompt line index (last line of WELCOME_BOOT_LINES).
_PROMPT_INDEX = len(WELCOME_BOOT_LINES) - 1

# An invisible replacement of the same length to keep layout stable.
_PROMPT_BLANK = ""


class WelcomeScreen(Screen):
    """Full-screen Fallout CRT welcome / boot splash.

    Displays the OVERSEER ASCII logo, then plays a line-by-line
    system initialization sequence.  Press Enter to skip the
    animation or proceed to the main HomeScreen once it finishes.
    """

    BINDINGS = [
        ("enter", "continue", "Continue"),
        ("q", "quit_app", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._boot_index = 0
        self._boot_done = False
        self._displayed_lines: list[str] = []
        self._blink_visible = True
        self._blink_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="welcome-container"):
            yield Static(WELCOME_HEADER, id="welcome-art")
            yield Static("", id="welcome-boot")

    def on_mount(self) -> None:
        """Kick off the line-by-line boot sequence after a brief pause."""
        self.set_timer(_BOOT_LINE_DELAY, self._show_next_boot_line)

    # ── boot animation ──

    def _show_next_boot_line(self) -> None:
        """Append the next boot line to the boot area."""
        if self._boot_index >= len(WELCOME_BOOT_LINES):
            self._finish_boot()
            return

        boot_widget = self.query_one("#welcome-boot", Static)
        line = WELCOME_BOOT_LINES[self._boot_index]
        self._displayed_lines.append(line)
        boot_widget.update("\n".join(self._displayed_lines))

        self._boot_index += 1
        self.set_timer(_BOOT_LINE_DELAY, self._show_next_boot_line)

    def _finish_boot(self) -> None:
        """Boot sequence complete — start blinking the prompt line."""
        self._boot_done = True
        self._blink_timer = self.set_interval(_BLINK_INTERVAL, self._toggle_blink)

    def _toggle_blink(self) -> None:
        """Toggle the prompt line between visible and blank."""
        self._blink_visible = not self._blink_visible
        # Swap the last line (the prompt) in our displayed lines.
        if self._blink_visible:
            self._displayed_lines[_PROMPT_INDEX] = WELCOME_BOOT_LINES[_PROMPT_INDEX]
        else:
            self._displayed_lines[_PROMPT_INDEX] = _PROMPT_BLANK
        boot_widget = self.query_one("#welcome-boot", Static)
        boot_widget.update("\n".join(self._displayed_lines))

    # ── actions ──

    def action_continue(self) -> None:
        if not self._boot_done:
            # Skip animation: render all remaining lines at once.
            self._displayed_lines.extend(WELCOME_BOOT_LINES[self._boot_index:])
            self._boot_index = len(WELCOME_BOOT_LINES)
            boot_widget = self.query_one("#welcome-boot", Static)
            boot_widget.update("\n".join(self._displayed_lines))
            self._finish_boot()
            return
        if self._blink_timer is not None:
            self._blink_timer.stop()
        self.dismiss(True)

    async def action_quit_app(self) -> None:
        if self._blink_timer is not None:
            self._blink_timer.stop()
        await self.app.action_quit()
