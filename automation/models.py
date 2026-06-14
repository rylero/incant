"""Model backends for AI steps and the Routing Pass.

Two real backends, one interface:

  - ClaudeCliModel : Claude via the ``claude -p`` headless CLI, on the user's
                     subscription. NOT the Anthropic HTTP API (ADR-0001 — the
                     user has a subscription, not API credits). Auth is whatever
                     ``claude`` is logged into; there is no key management here.
  - OllamaModel    : a local model via the ``ollama`` CLI.

Both expose ``complete(system, user) -> str``. ``complete_json`` wraps that to
coax a structured Payload (the named fields a downstream step requires) out of
the model and parse it back.

A FakeModel is provided for tests so the engine can run end-to-end with no CLI,
no network, and no GPU.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .payload import Payload, render


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
            raise ModelError(f"`claude -p` timed out after {self.timeout_s:.0f}s") from exc
        if proc.returncode != 0:
            raise ModelError(f"`claude -p` exited {proc.returncode}: {proc.stderr.strip()}")
        return proc.stdout.strip()


@dataclass
class OllamaModel:
    """A local model via ``ollama run <model>`` (system folded into the prompt)."""

    model: str = "llama3.2"
    timeout_s: float = 120.0

    def complete(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}" if system else user
        try:
            proc = subprocess.run(
                ["ollama", "run", self.model],
                input=prompt, capture_output=True, text=True, timeout=self.timeout_s,
            )
        except FileNotFoundError as exc:
            raise ModelError(
                "the `ollama` CLI was not found on PATH; install Ollama to use "
                "the local backend"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"`ollama run` timed out after {self.timeout_s:.0f}s") from exc
        if proc.returncode != 0:
            raise ModelError(f"`ollama run` exited {proc.returncode}: {proc.stderr.strip()}")
        return proc.stdout.strip()


@dataclass
class FakeModel:
    """Deterministic backend for tests. Echoes the requested fields.

    For ``complete_json`` it returns each requested field populated with the
    rendered input, so engine wiring (splits, merges, payload shaping) can be
    exercised without any external process.
    """

    canned: str = ""

    def complete(self, system: str, user: str) -> str:
        if self.canned:
            return self.canned
        # If the user prompt asked for a JSON object with specific keys, satisfy it.
        keys = re.findall(r'"([A-Za-z0-9_]+)"', user)
        if keys and "JSON" in user:
            return json.dumps({k: user.split("INPUT:", 1)[-1].strip() for k in keys})
        return user


def make_model(spec: dict | None) -> Model:
    """Build a backend from a manifest model spec, e.g.
    ``{"backend": "claude", "model": "sonnet"}``.
    """
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


# --------------------------------------------------------------------------- #
# Structured output
# --------------------------------------------------------------------------- #
def complete_json(model: Model, system: str, inbound: Payload, fields: list[str]) -> Payload:
    """Ask ``model`` to transform ``inbound`` into a Payload with exactly ``fields``.

    Wraps the plain text backend: instructs the model to emit a JSON object with
    the required keys, then parses it. Raises ModelError if the output can't be
    parsed into the requested fields.
    """
    want = fields or ["text"]
    schema = ", ".join(f'"{f}"' for f in want)
    user = (
        f"Return ONLY a JSON object with these keys: {schema}. "
        f"No prose, no code fence — just the object.\n\nINPUT:\n{render(inbound)}"
    )
    raw = model.complete(system, user)
    data = _parse_json_object(raw)
    if data is None:
        # Single-field fallback: treat the whole reply as that field's value.
        if len(want) == 1:
            return {want[0]: raw.strip()}
        raise ModelError(f"model did not return parseable JSON for fields {want}:\n{raw[:300]}")
    return {f: str(data.get(f, "")) for f in want}


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse_json_object(raw: str) -> dict | None:
    text = raw.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    # Grab the outermost {...} if there's surrounding chatter.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
