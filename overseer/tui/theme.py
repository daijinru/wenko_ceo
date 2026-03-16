"""Fallout Pip-Boy / CRT terminal theme for Overseer."""

from textual.theme import Theme

FALLOUT_THEME = Theme(
    name="fallout",
    primary="#00ff41",
    secondary="#33cc33",
    warning="#ccaa00",
    error="#ff3333",
    success="#00ff41",
    accent="#66ff66",
    foreground="#33cc33",
    background="#0a0f0a",
    surface="#0d1a0d",
    panel="#102010",
    dark=True,
    luminosity_spread=0.12,
    text_alpha=0.92,
    variables={
        "footer-key-foreground": "#00ff41",
        "block-cursor-background": "#00ff41",
        "block-cursor-foreground": "#0a0f0a",
        "block-cursor-text-style": "none",
        "input-selection-background": "#00ff41 25%",
        "button-color-foreground": "#0a0f0a",
        "button-focus-text-style": "reverse",
    },
)

FALLOUT_BANNER = (
    "[dim]══════════════════════════════════════════════[/dim]\n"
    "[bold]  OVERSEER // AI ACTION FIREWALL[/bold]\n"
    "[dim]  ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL[/dim]\n"
    "[dim]══════════════════════════════════════════════[/dim]"
)

WELCOME_HEADER = (
    "[dim]═══════════════════════════════════════════════════════════════[/dim]\n"
    "\n"
    "\n"
    "[bold]"
    "         ██████  ██    ██ ███████ ██████  ███████ ███████ ███████ ██████\n"
    "        ██    ██ ██    ██ ██      ██   ██ ██      ██      ██      ██   ██\n"
    "        ██    ██ ██    ██ █████   ██████  ███████ █████   █████   ██████\n"
    "        ██    ██  ██  ██  ██      ██   ██      ██ ██      ██      ██   ██\n"
    "         ██████    ████   ███████ ██   ██ ███████ ███████ ███████ ██   ██\n"
    "[/bold]\n"
    "\n"
    "                   ╔══════════════════════════════════╗\n"
    "                   ║   AI  ACTION  FIREWALL  v0.1.0   ║\n"
    "                   ╚══════════════════════════════════╝\n"
    "\n"
    "[dim]          ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL[/dim]\n"
    "[dim]          COPYRIGHT 2075-2077 ROBCO INDUSTRIES[/dim]\n"
    "\n"
    "[dim]          ─────────────────────────────────────────[/dim]\n"
)

# Boot sequence lines shown one-by-one with a delay.
WELCOME_BOOT_LINES = [
    "",
    "[dim]          >  INITIALIZING SYSTEM ...[/dim]",
    "[dim]          >  COGNITIVE KERNEL ............ [/dim][bold][  OK  ][/bold]",
    "[dim]          >  FIREWALL ENGINE ............. [/dim][bold][  OK  ][/bold]",
    "[dim]          >  PERCEPTION BUS .............. [/dim][bold][  OK  ][/bold]",
    "[dim]          >  MCP TOOL REGISTRY ........... [/dim][bold][  OK  ][/bold]",
    "[dim]          >  HUMAN GATE PROTOCOL ......... [/dim][bold][  OK  ][/bold]",
    "",
    "[dim]═══════════════════════════════════════════════════════════════[/dim]",
    "",
    "[bold]          \\[ PRESS ENTER TO CONTINUE ][/bold]",
]
