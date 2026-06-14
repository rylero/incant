"""Model backends for the Routing Pass and the no-route AI-clean fallback.

Two real backends, one interface:

  - ClaudeCliModel : Claude via the ``claude -p`` headless CLI, on the user's
                      subscription. NOT the Anthropic HTTP API (ADR-0001 — the
                      user has a subscription, not API credits). Auth is whatever
                      ``claude`` is logged into; there is no key management here.
  - OllamaModel    : a local model via the Ollama HTTP API at localhost:11434.

Both expose ``complete(system, user) -> str``.

A FakeModel is provided for tests so routing can run end-to-end with no CLI,
no network, and no GPU.
"""

from __future__ import annotations

import subprocess
import httpx
from dataclasses import dataclass
from typing import Protocol


class Model(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class ModelError(RuntimeError):
    """A backend failed (CLI missing, non-zero exit, unparseable output)."""


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
@dataclass
class ClaudeCliModel:
    """Claude through ``claude -p`` (print mode). Model defaults to Sonnet."""

    model: str = "sonnet"
    timeout_s: float = 120.0

    def complete(self, system: str, user: str) -> str:
        cmd = ["claude", "-p", "--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]
        try:
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True, timeout=self.timeout_s
            )
        except FileNotFoundError as exc:  # claude CLI not installed
            raise ModelError(
                "the `claude` CLI was not found on PATH; install it and run "
                "`claude login` to use the Claude backend"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ModelError(
                f"`claude -p` timed out after {self.timeout_s:.0f}s"
            ) from exc
        if proc.returncode != 0:
            raise ModelError(
                f"`claude -p` exited {proc.returncode}: {proc.stderr.strip()}"
            )
        return proc.stdout.strip()


@dataclass
class OllamaModel:
    """A local model via the Ollama HTTP API (``http://localhost:11434/api/generate``)."""

    model: str = "llama3.2"
    base_url: str = "http://localhost:11434"
    timeout_s: float = 120.0

    def complete(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}" if system else user
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False},
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("response", "").strip()
        except httpx.ConnectError as exc:
            raise ModelError(
                f"could not connect to Ollama at {self.base_url}; is Ollama running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ModelError(
                f"Ollama request timed out after {self.timeout_s:.0f}s"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ModelError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except Exception as exc:
            raise ModelError(f"Ollama request failed: {exc}") from exc


@dataclass
class FakeModel:
    """Deterministic backend for tests: returns ``canned``, or echoes the input."""

    canned: str = ""

    def complete(self, system: str, user: str) -> str:
        return self.canned or user


def make_model(spec: dict | None) -> Model:
    """Build a backend from a model spec, e.g. ``{"backend": "claude", "model": "sonnet"}``."""
    spec = spec or {}
    backend = spec.get("backend", "claude")
    model = spec.get("model")
    if backend == "claude":
        return ClaudeCliModel(model=model or "sonnet")
    if backend == "ollama":
        return OllamaModel(model=model or "llama3.2")
    if backend == "fake":
        return FakeModel(canned=spec.get("canned", ""))
    raise ModelError(f"unknown model backend {backend!r}")
