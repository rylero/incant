"""Command-line driver for the automation engine.

Lets you exercise pipelines end-to-end with no UI, no canvas, and no GPU — the
decoupling ADR-0003 calls for. Examples:

    python -m automation list
    python -m automation run pipelines/save-note --transcript "remember to call mom"
    python -m automation route --transcript "file a bug: word mode is broken"

Add ``--guard`` to answer Guards interactively at the terminal.
"""

from __future__ import annotations

import argparse
import sys

from .manifest import load_pipeline, discover_pipelines
from .engine import Engine, GuardRequest, GuardDecision, auto_approve
from .actions import ActionContext
from .payload import render
from . import router


def _ctx() -> ActionContext:
    return ActionContext(
        log=lambda m: print(m, file=sys.stderr),
        type_text=lambda t: print(f"[type@cursor] {t}"),
    )


def _interactive_guard(req: GuardRequest) -> GuardDecision:
    print("\n--- GUARD ---", file=sys.stderr)
    print(f"step: {req.node.id} ({req.node.type})", file=sys.stderr)
    print(f"next: {req.next_step}", file=sys.stderr)
    print(f"payload:\n{render(req.inbound)}", file=sys.stderr)
    ans = input("[a]pprove / [r]efactor / [s]top? ").strip().lower()
    if ans.startswith("s"):
        return GuardDecision("stop")
    if ans.startswith("r"):
        return GuardDecision("refactor", input("feedback: ").strip())
    return GuardDecision("approve")


def _build_engine(interactive: bool) -> Engine:
    return Engine(ctx=_ctx(), guard=_interactive_guard if interactive else auto_approve)


def cmd_list(_args) -> int:
    pipes = discover_pipelines(_args.root)
    if not pipes:
        print(f"no pipelines under {_args.root}/")
        return 0
    for p in pipes:
        print(f"{p.name}\n    {p.when_to_use}\n    ({p.folder})")
    return 0


def cmd_run(args) -> int:
    pipeline = load_pipeline(args.folder)
    result = _build_engine(args.guard).run(pipeline, args.transcript)
    print(result.summary())
    return 0 if result.ok else 1


def cmd_route(args) -> int:
    pipes = discover_pipelines(args.root)
    r = router.route(args.transcript, pipes)
    if not r.matched:
        print(f"no route ({r.reason}) — fallback: AI-clean then type at cursor")
        return 0
    print(f"routed → {r.pipeline.name} ({r.reason})", file=sys.stderr)
    result = _build_engine(args.guard).run(r.pipeline, args.transcript)
    print(result.summary())
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="automation", description=__doc__)
    ap.add_argument("--root", default="pipelines", help="pipelines folder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list installed pipelines")
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="run one pipeline folder")
    p_run.add_argument("folder")
    p_run.add_argument("--transcript", required=True)
    p_run.add_argument("--guard", action="store_true", help="answer guards interactively")
    p_run.set_defaults(func=cmd_run)

    p_route = sub.add_parser("route", help="route a transcript to a pipeline and run it")
    p_route.add_argument("--transcript", required=True)
    p_route.add_argument("--guard", action="store_true")
    p_route.set_defaults(func=cmd_route)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
