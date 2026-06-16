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


def load_all() -> list[dict]:
    if not _LOG_PATH.exists():
        return []
    entries = []
    with _LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return sorted(entries, key=lambda x: x.get("ts", 0))


def search(entries: list[dict], query: str) -> list[dict]:
    if not query.strip():
        return entries
    q = query.lower()
    return [
        e for e in entries
        if q in e.get("output", "").lower() or q in e.get("raw", "").lower()
    ]


def session_entries(entries: list[dict], session: str) -> list[dict]:
    return [e for e in entries if e.get("session") == session]


def sessions(entries: list[dict]) -> list[dict]:
    """Group entries by session; return one summary dict per session, newest first."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        sid = e.get("session", "")
        groups.setdefault(sid, []).append(e)
    result = []
    for sid, es in groups.items():
        es = sorted(es, key=lambda x: x.get("ts", 0))
        result.append({
            "session": sid,
            "ts": es[0].get("ts", 0),
            "ts_last": es[-1].get("ts", 0),
            "count": len(es),
            "preview": es[0].get("output", es[0].get("raw", "")),
            "entries": es,
        })
    return sorted(result, key=lambda x: x["ts"], reverse=True)


def search_sessions(summaries: list[dict], query: str) -> list[dict]:
    if not query.strip():
        return summaries
    q = query.lower()
    return [
        s for s in summaries
        if any(
            q in e.get("output", "").lower() or q in e.get("raw", "").lower()
            for e in s["entries"]
        )
    ]
