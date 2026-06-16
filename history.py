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


def delete_entry(ts: float) -> bool:
    """Remove the entry with the given timestamp. Returns True if found."""
    if not _LOG_PATH.exists():
        return False
    entries = []
    found = False
    with _LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if not found and abs(e.get("ts", -1) - ts) < 0.001:
                    found = True
                    continue
                entries.append(e)
            except Exception:
                pass
    if not found:
        return False
    with _LOG_PATH.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return True


def patch_last(session: str, raw: str, corrected_output: str) -> bool:
    """Update the output field of the most recent entry matching session+raw.

    Returns True if an entry was found and patched, False otherwise.
    Rewrites the whole file — only called on explicit user correction, so fine.
    """
    if not _LOG_PATH.exists():
        return False
    entries = []
    with _LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    # Find the last entry in this session whose raw matches
    patched = False
    for e in reversed(entries):
        if e.get("session") == session and e.get("raw", "").strip() == raw.strip():
            e["output"] = corrected_output
            patched = True
            break
    if not patched:
        return False
    _DIR.mkdir(parents=True, exist_ok=True)
    with _LOG_PATH.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return True


def clear() -> None:
    if _LOG_PATH.exists():
        _LOG_PATH.unlink()


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
