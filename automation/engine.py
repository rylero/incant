"""The DAG executor.

Runs a Pipeline over a command transcript. Implements the safety and topology
rules from ADR-0002 / CONTEXT.md:

  - roots receive ``{"transcript": ...}``; steps pass structured Payloads.
  - an AI step tailors a separate Payload per outgoing edge, each shaped to the
    fields its downstream step requires (a Split is not a copy).
  - a Merge step (always an AI step in practice) waits for every inbound branch
    (topological order guarantees this) and references branches by handle.
  - a Guard pauses before a step: approve / refactor-with-AI / stop.
  - Delay Until Finished holds a (sink) step until every other step has
    succeeded, so an upstream failure prevents an irreversible action.
  - failure policy is halt-and-surface: stop, report completed vs not-run.

The engine is headless: keyboard typing and the guard UI are injected. One
pipeline runs at a time (the caller enforces this).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .manifest import Pipeline, Node
from .payload import Payload, render
from .models import Model, make_model, complete_json
from .actions import REGISTRY, AI_TYPE, ActionContext


# --------------------------------------------------------------------------- #
# Guard plumbing
# --------------------------------------------------------------------------- #
@dataclass
class GuardRequest:
    node: Node
    inbound: Payload          # the payload the step is about to use
    next_step: str            # human label of what runs next ("end" if none)


@dataclass
class GuardDecision:
    action: str               # "approve" | "refactor" | "stop"
    feedback: str = ""        # text feedback when action == "refactor"


# A guard handler decides what to do when a guarded step is reached.
GuardHandler = Callable[[GuardRequest], GuardDecision]


def auto_approve(_req: GuardRequest) -> GuardDecision:
    return GuardDecision("approve")


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    ok: bool
    completed: list[str] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)
    outputs: dict[str, Payload] = field(default_factory=dict)
    failed: str | None = None         # node id that raised
    error: str | None = None          # error message
    stopped: bool = False             # a Guard chose Stop

    def summary(self) -> str:
        if self.stopped:
            return f"stopped at guard; ran {len(self.completed)} step(s)"
        if self.ok:
            return f"ok — {len(self.completed)} step(s)"
        return (
            f"FAILED at {self.failed}: {self.error}\n"
            f"  ran: {', '.join(self.completed) or '(none)'}\n"
            f"  not run: {', '.join(self.not_run) or '(none)'}"
        )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(
        self,
        ctx: ActionContext | None = None,
        guard: GuardHandler = auto_approve,
        model_factory: Callable[[dict | None], Model] = make_model,
    ) -> None:
        self.ctx = ctx or ActionContext()
        self.guard = guard
        self.model_factory = model_factory

    # -- public ----------------------------------------------------------
    def run(self, pipeline: Pipeline, transcript: str) -> RunResult:
        self.pipeline = pipeline
        self._t = transcript
        # edge_outputs[(src, dst, handle)] = payload produced for that edge
        self.edge_outputs: dict[tuple[str, str, str], Payload] = {}
        self.outputs: dict[str, Payload] = {}

        order = self._topo_order()
        delayed = [n for n in order if pipeline.node(n).delay_until_finished]
        main = [n for n in order if not pipeline.node(n).delay_until_finished]

        completed: list[str] = []
        try:
            for nid in main:
                stop = self._run_node(pipeline.node(nid))
                if stop:
                    return RunResult(ok=False, stopped=True, completed=completed,
                                     not_run=[n for n in order if n not in completed],
                                     outputs=self.outputs)
                completed.append(nid)
            # all non-delayed steps succeeded → now the irreversible sinks
            for nid in delayed:
                stop = self._run_node(pipeline.node(nid))
                if stop:
                    return RunResult(ok=False, stopped=True, completed=completed,
                                     not_run=[n for n in delayed if n not in completed],
                                     outputs=self.outputs)
                completed.append(nid)
        except Exception as exc:  # halt-and-surface
            not_run = [n for n in order if n not in completed]
            self.ctx.log(f"[engine] HALT at {self._cur}: {exc}")
            return RunResult(ok=False, failed=self._cur, error=str(exc),
                             completed=completed, not_run=not_run, outputs=self.outputs)

        return RunResult(ok=True, completed=completed, outputs=self.outputs)

    # -- node execution --------------------------------------------------
    def _run_node(self, node: Node) -> bool:
        """Run one node. Returns True if a Guard chose Stop (abort pipeline)."""
        self._cur = node.id

        if node.type == AI_TYPE:
            # Produce per-edge outputs first so a guard can show real payloads.
            self._run_ai_node(node)

        if node.guard:
            decision = self._guard_loop(node)
            if decision.action == "stop":
                self.ctx.log(f"[guard] stop at {node.id}")
                return True

        if node.type == AI_TYPE:
            return False  # AI effect already done above

        # integration step
        inbound = self._consume_inbound(node)
        action = REGISTRY[node.type]()
        out = action.run(inbound, node.config, self.ctx) or {}
        self.outputs[node.id] = out
        self._publish(node, out)
        return False

    def _run_ai_node(self, node: Node, feedback: str = "") -> None:
        model = self.model_factory(node.config.get("model"))
        system = node.system_prompt
        if feedback:
            system = f"{system}\n\nThe user reviewed your last output and asked: {feedback}"
        input_text = self._ai_input_text(node)

        out_edges = self.pipeline.out_edges(node.id)
        if not out_edges:
            # AI sink: one call, store as the node's result for logging.
            result = complete_json(model, system, {"input": input_text}, ["text"])
            self.outputs[node.id] = result
            return

        # Split: a tailored payload per outgoing edge, shaped to that branch's
        # required fields (NOT a copy).
        rep: Payload = {}
        for e in out_edges:
            fields = self._required_fields(e.dst)
            payload = complete_json(model, system, {"input": input_text}, fields)
            self.edge_outputs[(e.src, e.dst, e.handle)] = payload
            rep = payload
        self.outputs[node.id] = rep  # representative, for logging/guard
        self.ctx.log(f"[ai] {node.id} → {render(rep)}")

    # -- guard -----------------------------------------------------------
    def _guard_loop(self, node: Node) -> GuardDecision:
        while True:
            inbound = (self.outputs.get(node.id, {}) if node.type == AI_TYPE
                       else self._consume_inbound(node))
            req = GuardRequest(node=node, inbound=inbound, next_step=self._next_label(node))
            decision = self.guard(req)
            if decision.action != "refactor":
                return decision
            # Regenerate the payload the step will use, from feedback.
            if node.type == AI_TYPE:
                self._run_ai_node(node, feedback=decision.feedback)
            else:
                # Re-run any AI predecessors feeding this step.
                for e in self.pipeline.in_edges(node.id):
                    src = self.pipeline.node(e.src)
                    if src.type == AI_TYPE:
                        self._run_ai_node(src, feedback=decision.feedback)

    # -- payload plumbing ------------------------------------------------
    def _incoming(self, node: Node) -> list[tuple[str, Payload]]:
        """[(handle, payload)] feeding this node; roots get the transcript."""
        in_edges = self.pipeline.in_edges(node.id)
        if not in_edges:
            return [("in", {"transcript": self._t})]
        return [
            (e.handle, self.edge_outputs.get((e.src, e.dst, e.handle), {}))
            for e in in_edges
        ]

    def _consume_inbound(self, node: Node) -> Payload:
        """Single Payload for an integration step (merge = shallow combine)."""
        incoming = self._incoming(node)
        if len(incoming) == 1:
            return incoming[0][1]
        merged: Payload = {}
        for _handle, p in incoming:
            merged.update(p)
        return merged

    def _ai_input_text(self, node: Node) -> str:
        """Rendered input for an AI step; a merge labels each inbound handle."""
        incoming = self._incoming(node)
        if len(incoming) == 1:
            return render(incoming[0][1])
        return "\n\n".join(f"[{handle}]\n{render(p)}" for handle, p in incoming)

    def _publish(self, node: Node, out: Payload) -> None:
        """Put a non-AI step's single output on each of its outgoing edges."""
        for e in self.pipeline.out_edges(node.id):
            self.edge_outputs[(e.src, e.dst, e.handle)] = out

    def _required_fields(self, dst_id: str) -> list[str]:
        dst = self.pipeline.node(dst_id)
        if dst.type == AI_TYPE:
            return ["text"]                       # AI→AI: just carry text
        return list(REGISTRY[dst.type].required_inputs) or ["text"]

    # -- labels / order --------------------------------------------------
    def _next_label(self, node: Node) -> str:
        outs = self.pipeline.out_edges(node.id)
        if not outs:
            return "end"
        return " + ".join(self.pipeline.node(e.dst).type for e in outs)

    def _topo_order(self) -> list[str]:
        nodes = [n.id for n in self.pipeline.nodes]
        indeg = {nid: len(self.pipeline.in_edges(nid)) for nid in nodes}
        queue = [nid for nid in nodes if indeg[nid] == 0]
        order: list[str] = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for e in self.pipeline.out_edges(nid):
                indeg[e.dst] -= 1
                if indeg[e.dst] == 0:
                    queue.append(e.dst)
        if len(order) != len(nodes):
            raise ValueError("pipeline is not a DAG (cycle detected)")
        return order
