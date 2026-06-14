"""Trigger n8n workflows from incant over localhost webhooks (ADR-0004 pivot).

incant stays the voice front-end: it transcribes a command, the Routing Pass
picks a workflow, and this module POSTs the transcript to that workflow's webhook
and returns whatever the workflow's *Respond to Webhook* node replies. n8n owns
the integrations, credentials, and execution.

Webhooks here are protected with n8n's **JWT Auth**: the webhook node validates a
HS256-signed token against a shared passphrase. We mint that token per request
and send it as ``Authorization: Bearer <jwt>``. The passphrase is a named
Credential (default ``n8n`` → ``{"secret": "..."}``), never committed.

Stdlib only — no ``requests``/``PyJWT`` dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import credentials


class N8nError(RuntimeError):
    """The webhook was unreachable, rejected auth, or returned non-2xx."""


# --------------------------------------------------------------------------- #
# JWT (HS256)
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_jwt(secret: str, *, ttl_s: int = 300, claims: dict[str, Any] | None = None) -> str:
    """Sign a short-lived HS256 JWT with ``secret`` (n8n's JWT Auth passphrase)."""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload: dict[str, Any] = {"iat": now, "exp": now + ttl_s}
    if claims:
        payload.update(claims)

    def seg(obj: dict) -> str:
        return _b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{seg(header)}.{seg(payload)}"
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


# --------------------------------------------------------------------------- #
# Trigger
# --------------------------------------------------------------------------- #
@dataclass
class WebhookResult:
    status: int
    text: str
    json: Any | None = None

    @property
    def reply(self) -> str:
        """A single string to surface/type back, if the workflow returned one."""
        if isinstance(self.json, dict):
            for key in ("reply", "text", "message", "result"):
                if isinstance(self.json.get(key), str):
                    return self.json[key]
        return self.text


def trigger(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    credential: str | None = "n8n",
    timeout_s: float = 60.0,
) -> WebhookResult:
    """POST ``payload`` as JSON to an n8n webhook, signed with a JWT if secured.

    ``secret`` takes precedence; otherwise the passphrase is resolved from the
    named ``credential`` (its ``secret`` field). Pass ``credential=None`` and no
    ``secret`` for an unauthenticated webhook.
    """
    if secret is None and credential is not None:
        secret = _secret_from_credential(credential)

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {mint_jwt(secret)}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read().decode("utf-8", "replace")
            return WebhookResult(status=resp.status, text=text, json=_maybe_json(text))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        if exc.code in (401, 403):
            raise N8nError(
                f"n8n rejected auth ({exc.code}) — passphrase mismatch with the "
                f"webhook's JWT Auth credential. {detail}"
            ) from exc
        raise N8nError(f"n8n webhook returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise N8nError(
            f"could not reach n8n at {url} — is the container up "
            f"(docker compose -f n8n/docker-compose.yml ps)? {exc.reason}"
        ) from exc


def _secret_from_credential(name: str) -> str:
    cred = credentials.resolve(name)            # raises CredentialError if missing
    secret = cred.get("secret") or cred.get("passphrase")
    if not secret:
        raise N8nError(
            f"credential {name!r} has no 'secret' field for the n8n JWT passphrase"
        )
    return secret


def _maybe_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
