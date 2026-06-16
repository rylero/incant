"""AI-driven correction rule synthesis.

Reads history entries where raw != output (i.e. user-corrected transcriptions),
sends them to Claude via the `claude -p` CLI, and gets back a Python function
`apply_rules(text: str) -> str` that encodes the patterns deterministically.

The generated function is saved to %APPDATA%/incant/correction_rules.py and
loaded/cached at runtime so every transcription benefits without an LLM call.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

_DIR = Path(os.environ.get("APPDATA", Path.home())) / "incant"
_CORRECTIONS_LOG = _DIR / "corrections.jsonl"
_RULES_PATH = _DIR / "correction_rules.py"

_SYSTEM = """You are a speech-to-text correction specialist. A user's voice transcriber makes predictable errors. You will receive examples of (raw transcription → what the user actually meant) and must generate a Python function that fixes these errors deterministically.

Requirements:
- Return ONLY valid Python code — no markdown fences, no prose
- Function signature: def apply_rules(text: str) -> str
- Import re at module level (outside the function) if needed
- Use re.sub() with word boundaries for word-level fixes
- Add context-awareness where the same word maps to different things in different contexts
- Preserve original capitalisation where appropriate (use a case-preserving sub if needed)
- Be conservative: only encode patterns you are confident about from the examples
- Do not import anything beyond the Python standard library"""


def _load_corrections_log() -> list[dict]:
    if not _CORRECTIONS_LOG.exists():
        return []
    entries = []
    with _CORRECTIONS_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def _build_examples(history_entries: list[dict], corrections_log: list[dict]) -> str:
    """Format correction examples AND negative examples for the synthesis prompt.

    Negative examples (sentences where a known mis-transcription appeared but
    was intentionally left uncorrected) are essential for context-aware rules —
    they show Claude *when not* to apply a substitution.
    """
    # --- Word-level pairs from the review overlay log -----------------------
    pairs: dict[tuple[str, str], int] = {}
    for e in corrections_log:
        orig = e.get("original", "").strip()
        corr = e.get("corrected", "").strip()
        if orig and corr and orig.lower() != corr.lower():
            pairs[(orig, corr)] = pairs.get((orig, corr), 0) + 1

    # Collect words that have ever been corrected so we can find negatives
    corrected_words: set[str] = {orig.lower() for orig, _ in pairs}

    # --- Full-sentence examples from history --------------------------------
    corrected_sentences: list[tuple[str, str]] = []   # raw → corrected output
    uncorrected_sentences: list[str] = []             # raw that was left as-is

    for e in history_entries:
        raw = e.get("raw", "").strip()
        output = e.get("output", "").strip()
        if not raw:
            continue
        if raw.lower() != output.lower():
            # A correction was applied to this sentence
            corrected_sentences.append((raw, output))
        else:
            # Sentence was left unchanged — check if any known bad words appear
            raw_lower = raw.lower()
            if any(w in raw_lower for w in corrected_words):
                uncorrected_sentences.append(raw)

    lines: list[str] = []

    if pairs:
        lines.append("Known word/phrase corrections (original → corrected) with frequency:")
        for (orig, corr), count in sorted(pairs.items(), key=lambda x: -x[1])[:40]:
            lines.append(f"  [{count}x]  {orig!r}  →  {corr!r}")

    if corrected_sentences:
        lines.append("")
        lines.append("Sentences where user DID correct the transcription (raw → corrected):")
        for raw, out in corrected_sentences[-25:]:
            lines.append(f"  [CORRECTED]  raw: {raw!r}")
            lines.append(f"               out: {out!r}")

    if uncorrected_sentences:
        lines.append("")
        lines.append(
            "Sentences containing a known mis-transcription where user did NOT correct it\n"
            "(these are the negative examples — context where the rule should NOT fire):"
        )
        for raw in uncorrected_sentences[-20:]:
            lines.append(f"  [LEFT AS-IS]  {raw!r}")

    return "\n".join(lines)


def synthesize(history_entries: list[dict] | None = None) -> tuple[Path, str]:
    """Generate correction_rules.py from history. Returns (path, code).

    Raises ValueError if there is no data, ModelError if the CLI call fails.
    """
    import history as hist
    from automation.models import ClaudeCliModel

    if history_entries is None:
        history_entries = hist.load_all()

    corr_log = _load_corrections_log()
    examples = _build_examples(history_entries, corr_log)

    if not examples.strip():
        raise ValueError(
            "No correction history found — make some corrections with the review overlay first."
        )

    user_prompt = f"{examples}\n\nGenerate the apply_rules(text: str) -> str function."

    model = ClaudeCliModel(model="haiku", timeout_s=60.0)
    code = model.complete(_SYSTEM, user_prompt)

    # Strip accidental markdown fences
    code = re.sub(r"^```(?:python)?\s*\n?", "", code.strip(), flags=re.MULTILINE)
    code = re.sub(r"\n?```\s*$", "", code.strip(), flags=re.MULTILINE)
    code = code.strip()

    if "def apply_rules" not in code:
        raise ValueError(f"Generated code does not define apply_rules:\n{code[:300]}")

    _DIR.mkdir(parents=True, exist_ok=True)
    _RULES_PATH.write_text(code, encoding="utf-8")
    invalidate_cache()
    return _RULES_PATH, code


# ---------------------------------------------------------------------------
# Runtime application of generated rules
# ---------------------------------------------------------------------------

# False = not yet loaded; None = loaded but unavailable; callable = ready
_cached_fn: Callable[[str], str] | None | bool = False


def invalidate_cache() -> None:
    global _cached_fn
    _cached_fn = False


def _get_fn() -> Callable[[str], str] | None:
    global _cached_fn
    if _cached_fn is False:
        _cached_fn = _load_fn()
    return _cached_fn  # type: ignore[return-value]


def _load_fn() -> Callable[[str], str] | None:
    if not _RULES_PATH.exists():
        return None
    try:
        code = _RULES_PATH.read_text(encoding="utf-8")
        ns: dict = {}
        exec(compile(code, str(_RULES_PATH), "exec"), ns)  # noqa: S102
        fn = ns.get("apply_rules")
        return fn if callable(fn) else None
    except Exception:
        return None


def apply_generated(text: str) -> str:
    """Apply AI-generated rules to text; returns text unchanged on any error."""
    fn = _get_fn()
    if fn is None:
        return text
    try:
        return fn(text)
    except Exception:
        return text


def rules_exist() -> bool:
    return _RULES_PATH.exists()


def get_rules_code() -> str | None:
    if _RULES_PATH.exists():
        return _RULES_PATH.read_text(encoding="utf-8")
    return None


def delete_rules() -> None:
    if _RULES_PATH.exists():
        _RULES_PATH.unlink()
    invalidate_cache()
