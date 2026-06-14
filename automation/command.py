"""Run a voice command: POST the transcript to a webhook.

Flow:

  transcript -> POST {transcript,...} to the configured webhook -> surface reply.

When no webhook is configured the caller falls back to AI-cleaning the transcript
and typing it at the cursor.

    python -m automation.command "email mom that I'll call tonight" --webhook-url http://localhost:5678/webhook-test/xyz
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from . import n8n


@dataclass
class CommandOutcome:
    matched: bool
    reason: str = ""
    reply: str = ""
    sent: bool = False
    error: str | None = None


def build_payload(transcript: str) -> dict:
    """The body incant POSTs to a workflow webhook."""
    return {"transcript": transcript, "source": "incant", "ts": int(time.time())}


def run_command(
    transcript: str,
    *,
    webhook_url: str | None = None,
    credential: str | None = "n8n",
    dry_run: bool = False,
) -> CommandOutcome:
    if not webhook_url:
        return CommandOutcome(matched=False, reason="no webhook configured")

    if dry_run:
        return CommandOutcome(matched=True, reason="dry-run", sent=False)

    try:
        result = n8n.trigger(
            webhook_url, build_payload(transcript), credential=credential
        )
    except n8n.N8nError as exc:
        return CommandOutcome(
            matched=True, reason="webhook", sent=False, error=str(exc)
        )
    return CommandOutcome(matched=True, reason="webhook", sent=True, reply=result.reply)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Send a voice transcript to an n8n webhook."
    )
    ap.add_argument("transcript", help="what the user said")
    ap.add_argument("--webhook-url", help="n8n webhook URL to POST to")
    ap.add_argument(
        "--credential", default="n8n", help="credential name for JWT auth passphrase"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="show the payload but do NOT fire the webhook",
    )
    args = ap.parse_args(argv)

    out = run_command(
        args.transcript,
        webhook_url=args.webhook_url,
        credential=args.credential,
        dry_run=args.dry_run,
    )

    if not out.matched:
        print(f"not sent: {out.reason}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"would POST -> {args.webhook_url}")
        print(f"  credential: {args.credential}")
        print(f"  payload: {build_payload(args.transcript)}")
        return 0
    if out.error:
        print(f"FAILED: {out.error}", file=sys.stderr)
        return 1
    print(f"sent OK  reply: {out.reply or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
