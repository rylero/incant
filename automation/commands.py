"""The command registry — voice-routable n8n workflows (ADR-0004 pivot).

A Command is what the Routing Pass selects: a name + "when to use" description
(matched against the transcript, exactly like the old Pipeline) plus the n8n
webhook to fire and the Credential holding its JWT passphrase. Stored in a plain
``commands.json`` at the repo root — no secrets, just the webhook URLs.

This replaces ``discover_pipelines`` as the routing target: incant no longer
executes a DAG itself, it hands the transcript to n8n (see ``automation.n8n``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "commands.json"


@dataclass
class Command:
    name: str
    when_to_use: str             # routing description (duck-types as a Pipeline)
    webhook: str                 # n8n production webhook URL
    auth: str | None = "n8n"     # Credential name for the JWT passphrase, or None


def load_commands(path: str | Path = DEFAULT_FILE) -> list[Command]:
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Command(
            name=c["name"],
            when_to_use=c.get("when_to_use", ""),
            webhook=c["webhook"],
            auth=c.get("auth", "n8n"),
        )
        for c in data.get("commands", [])
    ]
