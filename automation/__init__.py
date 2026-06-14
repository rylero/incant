"""incant automation engine — voice-routed DAG pipelines.

This package is the execution layer for command mode. It is intentionally
decoupled from the UI and the (future) node-canvas editor (see
docs/adr/0003): pipelines are plain folders under ``pipelines/`` and can be
run and tested from the CLI before any editor exists.

Layers:
  manifest    — load/save the pipeline folder format (DAG + config)
  models      — AI backends (claude -p CLI, Ollama) behind one interface
  actions     — the Action Type catalog (building blocks of a pipeline)
  engine      — the DAG executor (guards, delay-until-finished, halt-and-surface)
  router      — the Routing Pass (pick a pipeline by name + "when to use")
  credentials — named secrets resolved outside pipeline folders
"""

from .manifest import Pipeline, Node, Edge, load_pipeline, save_pipeline
from .payload import Payload
from .engine import Engine, RunResult, GuardDecision

__all__ = [
    "Pipeline",
    "Node",
    "Edge",
    "load_pipeline",
    "save_pipeline",
    "Payload",
    "Engine",
    "RunResult",
    "GuardDecision",
]
