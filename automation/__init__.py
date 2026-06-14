"""incant automation — voice front-end that triggers n8n workflows.

incant is the voice front-end: dictation, command mode, and the n8n webhook
integration. n8n owns the integrations, credentials, and execution.

  n8n         — JWT-signed webhook client
  command     — POST a transcript to an n8n webhook
  notifier    — HTTP listener for n8n callbacks (desktop notifications)
  credentials — named secrets resolved outside the repo
  commands    — (legacy) the command registry
  router      — (legacy) the Routing Pass
"""

from .command import run_command, CommandOutcome
from . import n8n
from . import notifier

__all__ = [
    "run_command",
    "CommandOutcome",
    "n8n",
    "notifier",
]
