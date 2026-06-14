"""The Action Type catalog — building blocks a Pipeline is composed of.

Two families (ADR-0002 / CONTEXT.md):
  - the AI step reshapes text; it is special-cased by the engine because it
    tailors a Payload per outgoing edge.
  - integration steps perform an external effect. Each declares the fields it
    requires (``required_inputs``) so an upstream AI step knows what to emit.

Register a new integration step by adding it to ``REGISTRY``.
"""

from __future__ import annotations

from .base import Action, ActionContext
from .save_file import SaveFile
from .type_cursor import TypeAtCursor
from .github_issue import CreateGitHubIssue
from .send_email import SendEmail

# Integration action types keyed by manifest ``type``. The "ai" type is handled
# directly by the engine, so it is intentionally absent here.
REGISTRY: dict[str, type[Action]] = {
    SaveFile.type: SaveFile,
    TypeAtCursor.type: TypeAtCursor,
    CreateGitHubIssue.type: CreateGitHubIssue,
    SendEmail.type: SendEmail,
}

AI_TYPE = "ai"

__all__ = ["Action", "ActionContext", "REGISTRY", "AI_TYPE"]
