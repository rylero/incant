"""Type at Cursor — type a Payload's text into the focused app.

The actual keystroke emission is injected as ``ctx.type_text`` so the engine and
this step never import ``keyboard`` directly (keeps the engine headless and
testable). The UI wires ``ctx.type_text`` to ``keyboard.write``.

config:
  text_field : payload field to type (default "text")
"""

from __future__ import annotations

from .base import Action, ActionContext
from ..payload import Payload


class TypeAtCursor(Action):
    type = "type_cursor"
    required_inputs = ["text"]

    def run(self, inbound: Payload, config: dict, ctx: ActionContext) -> Payload:
        field = config.get("text_field", "text")
        (text,) = self.require(inbound, field)
        ctx.type_text(text)
        ctx.log(f"[type_cursor] typed {len(text)} chars")
        return {}
