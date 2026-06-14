"""End-to-end engine tests — no CLI, no network, no GPU (ADR-0003 decoupling).

A Scripted model returns canned JSON so splits/merges/guards/delay can be
exercised deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation.manifest import Pipeline, Node, Edge, save_pipeline, load_pipeline
from automation.engine import Engine, GuardRequest, GuardDecision
from automation.actions import ActionContext


class Scripted:
    """Model returning queued raw replies; records prompts it was given."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.replies.pop(0) if self.replies else "{}"


def factory(model):
    return lambda _spec: model


def capture_ctx():
    typed: list[str] = []
    logs: list[str] = []
    ctx = ActionContext(log=logs.append, type_text=typed.append)
    return ctx, typed, logs


# --------------------------------------------------------------------------- #
def test_linear_ai_then_save(tmp_path: Path):
    model = Scripted(json.dumps({"path": "ideas/call-mom.md", "content": "# Call mom\n\nCall mom."}))
    pipe = Pipeline(
        name="Save Note", when_to_use="save a note",
        nodes=[
            Node(id="compose", type="ai", config={"model": {"backend": "fake"}}),
            Node(id="write", type="save_file",
                 config={"base_dir": str(tmp_path), "path_field": "path",
                         "content_field": "content"}),
        ],
        edges=[Edge("compose", "write")],
    )
    ctx, _, _ = capture_ctx()
    res = Engine(ctx=ctx, model_factory=factory(model)).run(pipe, "remind me to call mom")

    assert res.ok, res.summary()
    written = tmp_path / "ideas" / "call-mom.md"
    assert written.read_text(encoding="utf-8").startswith("# Call mom")
    # the transcript reached the model as the rendered input
    assert "remind me to call mom" in model.calls[0][1]


def test_split_tailors_payload_per_edge(tmp_path: Path):
    # one AI step feeds two integration steps that need different fields
    model = Scripted(
        json.dumps({"path": "n.md", "content": "body"}),       # edge → save_file
        json.dumps({"text": "typed version"}),                  # edge → type_cursor
    )
    pipe = Pipeline(
        name="Split", when_to_use="x",
        nodes=[
            Node(id="ai", type="ai", config={"model": {"backend": "fake"}}),
            Node(id="save", type="save_file", config={"base_dir": str(tmp_path)}),
            Node(id="type", type="type_cursor"),
        ],
        edges=[Edge("ai", "save"), Edge("ai", "type")],
    )
    ctx, typed, _ = capture_ctx()
    res = Engine(ctx=ctx, model_factory=factory(model)).run(pipe, "hello")

    assert res.ok, res.summary()
    assert (tmp_path / "n.md").read_text() == "body"
    assert typed == ["typed version"]
    # two model calls — one tailored per outgoing edge (a split is not a copy)
    assert len(model.calls) == 2


def test_merge_waits_for_all_inbound():
    # two AI sources merge into a third AI step that types the joined result
    model = Scripted(
        json.dumps({"text": "left"}),
        json.dumps({"text": "right"}),
        json.dumps({"text": "joined"}),
    )
    pipe = Pipeline(
        name="Merge", when_to_use="x",
        nodes=[
            Node(id="a", type="ai", config={"model": {"backend": "fake"}}),
            Node(id="b", type="ai", config={"model": {"backend": "fake"}}),
            Node(id="m", type="ai", config={"model": {"backend": "fake"}}),
            Node(id="out", type="type_cursor"),
        ],
        edges=[Edge("a", "m", "left"), Edge("b", "m", "right"), Edge("m", "out")],
    )
    ctx, typed, _ = capture_ctx()
    res = Engine(ctx=ctx, model_factory=factory(model)).run(pipe, "go")

    assert res.ok, res.summary()
    assert typed == ["joined"]
    # the merge step saw both handles in its prompt
    merge_prompt = model.calls[2][1]
    assert "[left]" in merge_prompt and "[right]" in merge_prompt


def test_guard_stop_aborts():
    model = Scripted(json.dumps({"text": "draft"}))
    pipe = Pipeline(
        name="Guarded", when_to_use="x",
        nodes=[
            Node(id="ai", type="ai", config={"model": {"backend": "fake"}}, guard=True),
            Node(id="out", type="type_cursor"),
        ],
        edges=[Edge("ai", "out")],
    )
    ctx, typed, _ = capture_ctx()
    res = Engine(ctx=ctx, guard=lambda r: GuardDecision("stop"),
                 model_factory=factory(model)).run(pipe, "go")

    assert not res.ok and res.stopped
    assert typed == []  # downstream never ran


def test_guard_refactor_regenerates():
    model = Scripted(
        json.dumps({"text": "first"}),    # initial
        json.dumps({"text": "second"}),   # after refactor feedback
    )
    seen: list[str] = []

    def guard(req: GuardRequest) -> GuardDecision:
        seen.append(req.inbound.get("text", ""))
        # refactor once, then approve
        return GuardDecision("refactor", "make it better") if len(seen) == 1 else GuardDecision("approve")

    pipe = Pipeline(
        name="Refactor", when_to_use="x",
        nodes=[
            Node(id="ai", type="ai", config={"model": {"backend": "fake"}}, guard=True),
            Node(id="out", type="type_cursor"),
        ],
        edges=[Edge("ai", "out")],
    )
    ctx, typed, _ = capture_ctx()
    res = Engine(ctx=ctx, guard=guard, model_factory=factory(model)).run(pipe, "go")

    assert res.ok, res.summary()
    assert seen == ["first", "second"]
    assert typed == ["second"]
    # feedback reached the model on the second call
    assert "make it better" in model.calls[1][0]


def test_delay_until_finished_runs_last_and_is_skipped_on_failure(tmp_path: Path):
    # a delayed sink must not run if an earlier step fails (fail-before-irreversible)
    model = Scripted(json.dumps({"text": "x"}))
    pipe = Pipeline(
        name="Delay", when_to_use="x",
        nodes=[
            Node(id="ai", type="ai", config={"model": {"backend": "fake"}}),
            # save_file with a missing required field → fails at run time
            Node(id="bad", type="save_file", config={"base_dir": str(tmp_path)}),
            Node(id="irreversible", type="type_cursor", delay_until_finished=True),
        ],
        edges=[Edge("ai", "bad")],
    )
    ctx, typed, _ = capture_ctx()
    res = Engine(ctx=ctx, model_factory=factory(model)).run(pipe, "go")

    assert not res.ok
    assert res.failed == "bad"
    assert "irreversible" in res.not_run
    assert typed == []  # delayed irreversible step never fired


def test_halt_and_surface_reports_progress(tmp_path: Path):
    model = Scripted(json.dumps({"path": "n.md", "content": "c"}),
                     json.dumps({"missing": "field"}))
    pipe = Pipeline(
        name="Halt", when_to_use="x",
        nodes=[
            Node(id="ai1", type="ai", config={"model": {"backend": "fake"}}),
            Node(id="ok", type="save_file", config={"base_dir": str(tmp_path)}),
            Node(id="ai2", type="ai", config={"model": {"backend": "fake"}}),
            Node(id="fail", type="github_issue"),  # required title/body absent → fails
        ],
        edges=[Edge("ai1", "ok"), Edge("ok", "ai2"), Edge("ai2", "fail")],
    )
    ctx, _, _ = capture_ctx()
    res = Engine(ctx=ctx, model_factory=factory(model)).run(pipe, "go")

    assert not res.ok
    assert "ai1" in res.completed and "ok" in res.completed
    assert res.failed == "fail"


def test_manifest_round_trip(tmp_path: Path):
    pipe = Pipeline(
        name="Round Trip", when_to_use="prove save/load is lossless",
        nodes=[
            Node(id="ai", type="ai", config={"model": {"backend": "claude", "model": "sonnet"}},
                 guard=True, system_prompt="be concise"),
            Node(id="mail", type="send_email",
                 config={"credential": "smtp"}, delay_until_finished=True),
        ],
        edges=[Edge("ai", "mail", "in")],
    )
    folder = save_pipeline(pipe, tmp_path / "round-trip")
    again = load_pipeline(folder)

    assert again.name == "Round Trip"
    assert again.node("ai").guard is True
    assert again.node("ai").system_prompt == "be concise"
    assert again.node("mail").delay_until_finished is True
    assert again.edges[0].handle == "in"


def test_manifest_persists_editor_layout(tmp_path: Path):
    pipe = Pipeline(
        name="Laid Out", when_to_use="",
        nodes=[Node(id="a", type="ai", x=60, y=120),
               Node(id="b", type="type_cursor", x=300, y=120)],
        edges=[Edge("a", "b", "in")],
    )
    folder = save_pipeline(pipe, tmp_path / "layout")
    again = load_pipeline(folder)
    assert (again.node("a").x, again.node("a").y) == (60, 120)
    assert (again.node("b").x, again.node("b").y) == (300, 120)
