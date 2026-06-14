"""Named Credentials — secrets resolved outside pipeline folders.

A pipeline references a credential by name (e.g. an email login). The secret
itself never lives in the pipeline folder, so pipelines stay shareable and
version-controllable without leaking auth (ADR-0002).

Resolution order for a name like ``gmail``:
  1. env var ``INCANT_CRED_GMAIL`` holding a JSON object
  2. the credential store file (default ``.credentials.json`` at the repo root,
     gitignored), under the key ``gmail``

The ``gh`` and ``claude`` CLIs manage their own auth and need no Credential.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STORE_ENV = "INCANT_CREDENTIALS"
# %APPDATA% so the store is writable even when the app is installed under
# Program Files (a non-admin user can't write next to this file there).
DEFAULT_STORE = Path(os.environ.get("APPDATA", Path.home())) / "incant" / "credentials.json"


class CredentialError(RuntimeError):
    pass


def save(name: str, fields: dict[str, str]) -> None:
    """Set credential ``name`` to ``fields`` in the credential store file."""
    store_path = Path(os.environ.get(STORE_ENV, DEFAULT_STORE))
    store: dict = {}
    if store_path.exists():
        try:
            store = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            store = {}
    store[name] = fields
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=2), encoding="utf-8")


def resolve(name: str) -> dict[str, str]:
    """Return the secret fields for credential ``name`` as a dict."""
    env_key = f"INCANT_CRED_{name.upper()}"
    if env_key in os.environ:
        try:
            return json.loads(os.environ[env_key])
        except json.JSONDecodeError as exc:
            raise CredentialError(f"{env_key} is not valid JSON") from exc

    store_path = Path(os.environ.get(STORE_ENV, DEFAULT_STORE))
    if store_path.exists():
        store = json.loads(store_path.read_text(encoding="utf-8"))
        if name in store:
            return store[name]

    raise CredentialError(
        f"credential {name!r} not found — set {env_key} or add it to {store_path}"
    )
