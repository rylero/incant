from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_DIR = Path(os.environ.get("APPDATA", Path.home())) / "incant"
_MAP_PATH = _DIR / "corrections.json"
_LOG_PATH = _DIR / "corrections.jsonl"


def load_map() -> dict[str, str]:
    if _MAP_PATH.exists():
        try:
            return json.loads(_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_map(m: dict[str, str]) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _MAP_PATH.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def record(original: str, corrected: str) -> dict[str, str]:
    """Append correction pair to the log and update the substitution map."""
    _DIR.mkdir(parents=True, exist_ok=True)
    entry = json.dumps(
        {"ts": time.time(), "original": original, "corrected": corrected},
        ensure_ascii=False,
    )
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")

    m = load_map()
    ow = original.strip().split()
    cw = corrected.strip().split()
    if len(ow) == len(cw):
        for o, c in zip(ow, cw):
            if o.lower() != c.lower():
                m[o.lower()] = c
    elif original.lower().strip() != corrected.lower().strip():
        m[original.lower().strip()] = corrected.strip()
    _save_map(m)
    return m


def apply(text: str, corr: dict[str, str]) -> str:
    """Apply word/phrase substitutions, preserving original casing pattern."""
    if not corr:
        return text
    for wrong, right in sorted(corr.items(), key=lambda x: -len(x[0])):
        pat = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)

        def _sub(m: re.Match, r: str = right) -> str:
            s = m.group(0)
            if s.isupper():
                return r.upper()
            if s and s[0].isupper():
                return r[0].upper() + r[1:]
            return r

        text = pat.sub(_sub, text)
    return text


def hotwords(corr: dict[str, str]) -> str | None:
    """Single-word corrected values as a hotwords hint string for Whisper."""
    vals = [v for v in corr.values() if " " not in v]
    return " ".join(vals) if vals else None
