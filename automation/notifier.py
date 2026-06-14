"""Lightweight HTTP listener for n8n callbacks.

Endpoints (n8n reaches incant at ``http://host.docker.internal:18765``):

  POST /notify   — pop a Windows toast notification
  POST /approve  — queue an approval request; UI shows a dialog

Runs as a daemon thread inside the UI process — no separate server.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.request
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

from .credentials import resolve as resolve_credential
from .n8n import mint_jwt

log = logging.getLogger(__name__)

NOTIFY_PORT = 18767

# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #


@dataclass
class Notification:
    title: str = "incant"
    message: str = ""
    level: str = "info"  # info | warn | error


@dataclass
class ApprovalRequest:
    id: str
    title: str
    message: str
    callback_url: str


# --------------------------------------------------------------------------- #
# Notification dispatch (winotify Windows toast)
# --------------------------------------------------------------------------- #


def _show(toast: Notification) -> None:
    try:
        from winotify import Notification as WinToast

        t = WinToast(
            app_id="incant",
            title=toast.title,
            msg=toast.message,
            duration="short",
        )
        t.show()
    except Exception as exc:
        log.warning("desktop notification failed: %s", exc)


# --------------------------------------------------------------------------- #
# Approval queue (HTTP thread -> UI thread bridge)
# --------------------------------------------------------------------------- #

_approval_queue: queue.Queue[ApprovalRequest] = queue.Queue()
_pending: dict[str, ApprovalRequest] = {}


def pop_approval() -> ApprovalRequest | None:
    """Called by the UI thread to drain pending approvals (non-blocking)."""
    try:
        req = _approval_queue.get_nowait()
        _pending[req.id] = req
        return req
    except queue.Empty:
        return None


def respond_approval(approval_id: str, approved: bool) -> None:
    """POST the user's decision back to n8n's callback URL (JWT-signed)."""
    req = _pending.pop(approval_id, None)
    if req is None:
        log.warning("approval %s not found (already responded?)", approval_id)
        return
    try:
        secret = resolve_credential("n8n")["secret"]
        token = mint_jwt(secret)
        body = json.dumps({"approved": approved, "id": approval_id}).encode()
        r = urllib.request.Request(
            req.callback_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        urllib.request.urlopen(r, timeout=30)
    except Exception as exc:
        log.error("callback to n8n failed: %s", exc)
        _show(
            Notification(
                title="incant",
                message="Callback to n8n failed — see log",
                level="error",
            )
        )


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    """Routes POST /notify and POST /approve to the right handler."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        data: dict[str, Any] = json.loads(body) if body else {}

        try:
            if self.path == "/notify":
                self._handle_notify(data)
            elif self.path == "/approve":
                self._handle_approve(data)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
                return
        except Exception as exc:
            log.exception("handler failed")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _handle_notify(self, data: dict[str, Any]) -> None:
        toast = Notification(
            title=data.get("title", "incant"),
            message=data.get("message", ""),
            level=data.get("level", "info"),
        )
        _show(toast)

    def _handle_approve(self, data: dict[str, Any]) -> None:
        req = ApprovalRequest(
            id=data.get("id", ""),
            title=data.get("title", "Approval Required"),
            message=data.get("message", ""),
            callback_url=data.get("callback_url", ""),
        )
        _show(
            Notification(title=f"⏳ {req.title}", message="Waiting for your decision…")
        )
        _approval_queue.put(req)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

_server: HTTPServer | None = None
_thread: threading.Thread | None = None


def start(port: int = NOTIFY_PORT) -> HTTPServer:
    """Start the listener daemon (idempotent)."""
    global _server, _thread
    if _server is not None:
        return _server

    _server = HTTPServer(("0.0.0.0", port), _Handler)
    _thread = threading.Thread(
        target=_server.serve_forever, daemon=True, name="notifier"
    )
    _thread.start()
    log.info("listener on port %d", port)
    return _server


def stop() -> None:
    """Shut the listener down."""
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    _thread = None
