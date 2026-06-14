# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Install deps: `uv sync`
- Run GUI: `uv run ui`
- Run CLI dictation: `uv run stt`
- Run tests: `uv run pytest` (single test: `uv run pytest tests/test_segmenter.py::test_name`)

## Environment

- Python is pinned to 3.12 (`.python-version`, `requires-python = ">=3.12,<3.13"`) — required by the CUDA 12 / cuDNN 9 wheels for faster-whisper on Blackwell GPUs. Don't bump to 3.13.
- `stt.py` sets up CUDA DLL paths at import time. Always go through `uv run stt` / `uv run ui`, or `import stt` first, before touching `faster_whisper` directly — otherwise `cublas64_12.dll not found`.
- Command-mode automation (voice commands → actions) requires the local n8n stack: `cd n8n && docker compose up -d` (http://localhost:5678). incant POSTs transcripts to webhooks registered in `commands.json`; n8n executes them.
- Secrets: copy `.credentials.example.json` → `.credentials.json` (gitignored), or set `INCANT_CRED_<NAME>` env vars. Resolved via `automation/credentials.py`.
