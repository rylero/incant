"""Action base class — the contract every integration step implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..payload import Payload


@dataclass
class ActionContext:
    """Side-channel passed to every step at run time.

    ``log`` is how a step reports progress to the activity log; ``type_text`` is
    the cursor-typing hook (injected so the engine stays free of the keyboard
    dependency and tests can capture output).
    """

    log: Callable[[str], None] = lambda _msg: None
    type_text: Callable[[str], None] = lambda _text: None
    extras: dict[str, Any] = field(default_factory=dict)


class Action:
    """An integration step. Subclasses set ``type`` and ``required_inputs``.

    ``required_inputs`` are the Payload fields this step needs; the engine uses
    them to tell an upstream AI step exactly which fields to produce on the edge
    feeding this step.
    """

    type: str = ""
    required_inputs: list[str] = []

    def run(self, inbound: Payload, config: dict, ctx: ActionContext) -> Payload:
        """Perform the effect. Return a Payload for any downstream steps.

        Raise on failure — the engine halts the pipeline and surfaces it.
        """
        raise NotImplementedError

    # -- helper for subclasses -------------------------------------------
    @staticmethod
    def require(inbound: Payload, *names: str) -> tuple[str, ...]:
        missing = [n for n in names if not inbound.get(n)]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")
        return tuple(inbound[n] for n in names)
