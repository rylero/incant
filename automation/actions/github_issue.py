"""Create GitHub Issue — open an issue via the ``gh`` CLI.

``gh`` is an accepted hard dependency (ADR-0002): it manages its own auth, so no
Credential is needed. The repo defaults to whatever ``gh`` resolves in the
current directory unless ``repo`` is set.

config:
  repo : "owner/name" (optional; default = gh's current-repo resolution)
"""

from __future__ import annotations

import subprocess

from .base import Action, ActionContext
from ..payload import Payload


class CreateGitHubIssue(Action):
    type = "github_issue"
    required_inputs = ["title", "body"]

    def run(self, inbound: Payload, config: dict, ctx: ActionContext) -> Payload:
        title, body = self.require(inbound, "title", "body")
        cmd = ["gh", "issue", "create", "--title", title, "--body", body]
        if config.get("repo"):
            cmd += ["--repo", config["repo"]]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "the `gh` CLI was not found on PATH; install it and run "
                "`gh auth login` to create issues"
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(f"`gh issue create` failed: {proc.stderr.strip()}")
        url = proc.stdout.strip()
        ctx.log(f"[github_issue] created {url}")
        return {"url": url}
