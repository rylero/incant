from __future__ import annotations

import json
import os
import time
from pathlib import Path

_DIR = Path(os.environ.get("APPDATA", Path.home())) / "incant"
_LOG_PATH = _DIR / "history.jsonl"


def log_phrase(session: str, raw: str, output: str) -> None:
    """Append one transcribed phrase to the history log."""
    _DIR.mkdir(parents=True, exist_ok=True)
    entry = json.dumps(
        {"ts": time.time(), "session": session, "raw": raw, "output": output},
        ensure_ascii=False,
    )
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")
