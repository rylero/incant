"""Pipeline folder format — load/save the DAG manifest.

A Pipeline is a self-contained, portable folder under ``pipelines/``:

    pipelines/save-note/
        manifest.json        # name, when-to-use, nodes, edges, per-step config
        prompts/<node>.md    # editable system prompt for each AI step

The manifest holds no secrets — only named Credential references (see
``credentials``). Sharing a pipeline is sharing its folder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Node:
    """One step in the pipeline — an instance of an Action Type."""

    id: str
    type: str                                  # action type key, e.g. "ai", "save_file"
    config: dict[str, Any] = field(default_factory=dict)
    guard: bool = False                        # pause for human review before running
    delay_until_finished: bool = False         # defer irreversible step to the end
    system_prompt: str = ""                     # AI steps only; loaded from prompts/<id>.md


@dataclass
class Edge:
    """A directed connection carrying a Payload from one node to another.

    ``handle`` names the inbound slot on the target — a merge step references
    each inbound branch by its handle name.
    """

    src: str
    dst: str
    handle: str = "in"


@dataclass
class Pipeline:
    name: str
    when_to_use: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    folder: Path | None = None                 # source folder, when loaded from disk

    # -- graph helpers ----------------------------------------------------
    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"no node {node_id!r} in pipeline {self.name!r}")

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.src == node_id]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == node_id]

    def roots(self) -> list[Node]:
        """Nodes with no inbound edge — they receive the command transcript."""
        targets = {e.dst for e in self.edges}
        return [n for n in self.nodes if n.id not in targets]


def load_pipeline(folder: str | Path) -> Pipeline:
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))

    nodes: list[Node] = []
    for nd in manifest.get("nodes", []):
        node = Node(
            id=nd["id"],
            type=nd["type"],
            config=nd.get("config", {}),
            guard=nd.get("guard", False),
            delay_until_finished=nd.get("delay_until_finished", False),
        )
        prompt_file = folder / "prompts" / f"{node.id}.md"
        if prompt_file.exists():
            node.system_prompt = prompt_file.read_text(encoding="utf-8")
        nodes.append(node)

    edges = [
        Edge(src=ed["src"], dst=ed["dst"], handle=ed.get("handle", "in"))
        for ed in manifest.get("edges", [])
    ]
    return Pipeline(
        name=manifest["name"],
        when_to_use=manifest.get("when_to_use", ""),
        nodes=nodes,
        edges=edges,
        folder=folder,
    )


def save_pipeline(pipeline: Pipeline, folder: str | Path | None = None) -> Path:
    folder = Path(folder or pipeline.folder or Path("pipelines") / _slug(pipeline.name))
    (folder / "prompts").mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": pipeline.name,
        "when_to_use": pipeline.when_to_use,
        "nodes": [
            {
                "id": n.id,
                "type": n.type,
                "config": n.config,
                "guard": n.guard,
                "delay_until_finished": n.delay_until_finished,
            }
            for n in pipeline.nodes
        ],
        "edges": [{"src": e.src, "dst": e.dst, "handle": e.handle} for e in pipeline.edges],
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    for n in pipeline.nodes:
        if n.system_prompt:
            (folder / "prompts" / f"{n.id}.md").write_text(n.system_prompt, encoding="utf-8")
    pipeline.folder = folder
    return folder


def discover_pipelines(root: str | Path = "pipelines") -> list[Pipeline]:
    """Load every pipeline folder under ``root`` (each has a manifest.json)."""
    root = Path(root)
    if not root.exists():
        return []
    out: list[Pipeline] = []
    for child in sorted(root.iterdir()):
        if (child / "manifest.json").exists():
            out.append(load_pipeline(child))
    return out


def _slug(name: str) -> str:
    return "-".join(name.lower().split())
