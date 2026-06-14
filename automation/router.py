"""The Routing Pass — pick which Command a voice transcript asked for.

Works like Claude Code skill selection (CONTEXT.md): the transcript is matched
against each command's name + "when to use" description, and the best one is
chosen — or none. Uses a fast global router Model (Haiku via ``claude -p`` or a
small Ollama model).

On no match the caller falls back to AI-cleaning the transcript and typing it at
the cursor — never raw, never silent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .commands import Command
from .models import Model, make_model

DEFAULT_ROUTER_MODEL = {"backend": "claude", "model": "haiku"}

_SYSTEM = (
    "You route a spoken command to one automation command. You are given the "
    "transcript and a numbered list of commands, each with a name and a "
    "'when to use' description. Choose the single best match, or none if the "
    "command does not clearly match any. Reply with ONLY a JSON object: "
    '{"choice": <number or null>, "reason": "<short>"}.'
)


@dataclass
class Route:
    command: Command | None
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.command is not None


def route(
    transcript: str,
    commands: list[Command],
    model: Model | None = None,
) -> Route:
    if not commands:
        return Route(None, "no commands registered")
    model = model or make_model(DEFAULT_ROUTER_MODEL)

    listing = "\n".join(
        f"{i + 1}. {c.name} — {c.when_to_use}" for i, c in enumerate(commands)
    )
    user = f"TRANSCRIPT:\n{transcript}\n\nCOMMANDS:\n{listing}"
    raw = model.complete(_SYSTEM, user)

    choice, reason = _parse(raw)
    if choice is None or not (1 <= choice <= len(commands)):
        return Route(None, reason or "no match")
    return Route(commands[choice - 1], reason)


def _parse(raw: str) -> tuple[int | None, str]:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        choice = data.get("choice")
        choice = int(choice) if isinstance(choice, (int, float)) else None
        return choice, str(data.get("reason", ""))
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\d+", raw)        # last-ditch: first integer in the reply
        return (int(m.group()) if m else None), "parsed loosely"
