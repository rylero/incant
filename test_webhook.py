"""Send mock transcripts to the incant n8n webhook for testing.

Usage:
    uv run test_webhook.py "email mom I'll be late"
    uv run test_webhook.py "create a todo list for groceries" --verbose
    uv run test_webhook.py --notify    # test the notification callback
"""

from __future__ import annotations

import argparse
import json
import sys
from http.client import HTTPConnection

from automation import n8n
from automation.command import build_payload
from automation.credentials import resolve as resolve_credential
from automation.notifier import NOTIFY_PORT

WEBHOOK_URL = "http://localhost:5678/webhook-test/0c928aae-ae67-41b2-8688-b845d728ae28"

MOCK_TRANSCRIPTS = [
    "email mom I'll be late for dinner",
    "create a todo list for groceries",
    "what's the weather like today",
    "send a slack message to the team",
    "add milk to my shopping list",
    "schedule a meeting for tomorrow at 3pm",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Send mock transcripts to the incant n8n webhook."
    )
    ap.add_argument("transcript", nargs="?", help="a single transcript to send")
    ap.add_argument("--verbose", "-v", action="store_true", help="show full response")
    ap.add_argument(
        "--notify", action="store_true", help="test the notification callback endpoint"
    )
    ap.add_argument(
        "--approve",
        action="store_true",
        help="test the approval dialog (needs UI running)",
    )
    ap.add_argument(
        "--list", action="store_true", help="list available mock transcripts"
    )
    args = ap.parse_args(argv)

    if args.list:
        print("Mock transcripts:")
        for i, t in enumerate(MOCK_TRANSCRIPTS, 1):
            print(f"  {i}. {t}")
        return 0

    if args.notify:
        body = json.dumps({"title": "incant", "message": "test notification from n8n"})
        conn = HTTPConnection("localhost", NOTIFY_PORT)
        conn.request("POST", "/notify", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        print(f"{resp.status} {resp.read().decode()}")
        return 0

    if args.approve:
        body = json.dumps(
            {
                "id": "test-approve-001",
                "title": "Approve Email",
                "message": "To: mom@example.com\nSubject: Running late\nBody: I'll be late for dinner, don't wait up.",
                "callback_url": "http://host.docker.internal:5678/webhook-test/approve-callback",
            }
        )
        conn = HTTPConnection("localhost", NOTIFY_PORT)
        conn.request("POST", "/approve", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        print(f"{resp.status} {resp.read().decode()}")
        return 0

    if args.transcript:
        transcripts = [args.transcript]
    else:
        transcripts = MOCK_TRANSCRIPTS

    secret = resolve_credential("n8n")["secret"]
    credential = None  # already passed via secret=

    for text in transcripts:
        payload = build_payload(text)
        print(f"\n→ {text}")
        try:
            result = n8n.trigger(
                WEBHOOK_URL, payload, secret=secret, credential=credential
            )
            status_icon = "✓" if result.status < 400 else "✗"
            print(
                f"  {status_icon} HTTP {result.status}  reply: {result.reply or '(empty)'}"
            )
            if args.verbose:
                print(f"  body: {result.text[:500]}")
        except n8n.N8nError as exc:
            print(f"  ✗ {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
