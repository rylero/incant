"""Payload — the structured set of named fields passed along an edge.

A Payload is just a mapping of field name -> string value. Integration steps
declare the fields they require (see ``actions``); an AI step placed before
them is shaped to produce exactly those fields. Keeping it a plain dict makes
payloads trivial to serialize, log, and show in a Guard popup.
"""

from __future__ import annotations

from typing import Dict

# A payload is a flat mapping of field name to value. Values are strings so a
# payload round-trips cleanly through the model (JSON) and into integration
# steps that ultimately want text (email body, issue title, file contents).
Payload = Dict[str, str]


def render(payload: Payload) -> str:
    """Render a payload as readable text for a model prompt or a log line."""
    if not payload:
        return "(empty)"
    if list(payload.keys()) == ["transcript"]:
        return payload["transcript"]
    return "\n".join(f"{k}: {v}" for k, v in payload.items())
