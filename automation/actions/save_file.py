"""Save to File System — write a Payload's content to a path under a base dir.

Saving an Obsidian note is just this step pointed at a vault folder (CONTEXT.md);
there is no separate Obsidian step.

config:
  base_dir    : root the file is written under (e.g. an Obsidian vault). Paths
                are confined to it — a payload cannot escape via ``..``.
  path_field  : payload field holding the relative path (default "path")
  content_field : payload field holding the body (default "content")
  append      : append instead of overwrite (default False)
"""

from __future__ import annotations

from pathlib import Path

from .base import Action, ActionContext
from ..payload import Payload


class SaveFile(Action):
    type = "save_file"
    required_inputs = ["path", "content"]

    def run(self, inbound: Payload, config: dict, ctx: ActionContext) -> Payload:
        path_field = config.get("path_field", "path")
        content_field = config.get("content_field", "content")
        rel, content = self.require(inbound, path_field, content_field)

        base = Path(config.get("base_dir", ".")).expanduser().resolve()
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            raise ValueError(f"refusing to write outside base_dir: {rel!r}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if config.get("append") and target.exists():
            with target.open("a", encoding="utf-8") as fh:
                fh.write(("\n" if not content.startswith("\n") else "") + content)
        else:
            target.write_text(content, encoding="utf-8")
        ctx.log(f"[save_file] wrote {target}")
        return {"path": str(target)}
