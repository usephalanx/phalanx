"""
Command Parser — parses /forge Slack commands into structured WorkOrderRequest.

Design (evidence in EXECUTION_PLAN.md §B, AD-005):
  - Single entry point: all FORGE work starts with a /forge Slack command.
  - AP-001: No multiple entry points or duplicate coordination.
  - Pure function parsing — no I/O. Makes it trivially unit-testable.

Supported command formats:
  /forge build <title>               — create a work order
  /forge build <title> --priority P1 — with priority
  /forge status [run_id]             — query run status
  /forge cancel <run_id>             — cancel a run
  /forge help                        — show help

Priority mapping:
  P0 → 90, P1 → 75, P2 → 50 (default), P3 → 25, P4 → 10
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class CommandType(StrEnum):
    BUILD = "build"
    STATUS = "status"
    CANCEL = "cancel"
    HELP = "help"
    UNKNOWN = "unknown"


_PRIORITY_MAP: dict[str, int] = {
    "P0": 90,
    "P1": 75,
    "P2": 50,
    "P3": 25,
    "P4": 10,
}

# Pattern: --priority P0 or --priority=P0
_PRIORITY_PATTERN = re.compile(r"--priority[= ]?(P[0-4])", re.IGNORECASE)
# Pattern: --description "..." or --desc "..."
_DESCRIPTION_PATTERN = re.compile(
    r"--(?:description|desc)[= ]?[\"']?(.+?)[\"']?(?:--|$)", re.IGNORECASE
)


@dataclass
class ParsedCommand:
    command_type: CommandType
    raw_text: str
    title: str = ""
    description: str = ""
    priority: int = 50  # Default P2
    run_id: str | None = None
    tags: list[str] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.parse_error is None


class CommandParseError(ValueError):
    pass


def parse_command(text: str) -> ParsedCommand:
    """
    Parse a raw /forge command text into a ParsedCommand.

    Args:
        text: The message text after /forge, e.g. "build Add OAuth login"

    Returns ParsedCommand — always succeeds; set parse_error on bad input.
    """
    text = text.strip()
    raw_text = text

    if not text:
        return ParsedCommand(
            command_type=CommandType.HELP,
            raw_text=raw_text,
        )

    # Extract priority flag first (then remove from text)
    priority = 50
    priority_match = _PRIORITY_PATTERN.search(text)
    if priority_match:
        priority_str = priority_match.group(1).upper()
        priority = _PRIORITY_MAP.get(priority_str, 50)
        text = _PRIORITY_PATTERN.sub("", text).strip()

    # Extract description flag
    description = ""
    desc_match = _DESCRIPTION_PATTERN.search(text)
    if desc_match:
        description = desc_match.group(1).strip()
        text = _DESCRIPTION_PATTERN.sub("", text).strip()

    # Split into command + rest
    parts = text.split(maxsplit=1)
    if not parts:
        return ParsedCommand(command_type=CommandType.HELP, raw_text=raw_text)

    verb = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if verb == "build":
        if not rest:
            return ParsedCommand(
                command_type=CommandType.BUILD,
                raw_text=raw_text,
                parse_error="Usage: /forge build <title>",
            )
        return ParsedCommand(
            command_type=CommandType.BUILD,
            raw_text=raw_text,
            title=rest[:200],  # cap title length
            description=description or rest,
            priority=priority,
        )

    if verb == "status":
        return ParsedCommand(
            command_type=CommandType.STATUS,
            raw_text=raw_text,
            run_id=rest or None,
        )

    if verb == "cancel":
        if not rest:
            return ParsedCommand(
                command_type=CommandType.CANCEL,
                raw_text=raw_text,
                parse_error="Usage: /forge cancel <run_id>",
            )
        return ParsedCommand(
            command_type=CommandType.CANCEL,
            raw_text=raw_text,
            run_id=rest,
        )

    if verb == "help":
        return ParsedCommand(command_type=CommandType.HELP, raw_text=raw_text)

    return ParsedCommand(
        command_type=CommandType.UNKNOWN,
        raw_text=raw_text,
        parse_error=f"Unknown command: {verb!r}. Try /forge help.",
    )


HELP_TEXT = """\
*FORGE — AI Team OS*

Available commands:
• `/forge build <title>` — Start a new work order
• `/forge build <title> --priority P0|P1|P2|P3|P4` — With priority (default P2)
• `/forge status` — List active runs
• `/forge status <run_id>` — Show status of a specific run
• `/forge cancel <run_id>` — Cancel an active run
• `/forge help` — Show this help

Priority levels: P0 (critical) → P4 (low). Default: P2.
"""
