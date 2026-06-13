# AI engine and the local-first boundary

Dictation stays 100% local (faster-whisper on-device) — that is the privacy-critical,
always-used path and the README's promise. The new automation layer (command mode)
uses an LLM, and we run it through the **`claude -p` CLI** (headless, on the user's
Claude subscription) or a **local Ollama model** — never the Anthropic HTTP API,
because the user has a subscription, not API credits. This redraws the product's
"fully local" boundary: **dictation = local always; command mode = opt-in, uses
`claude -p`/Ollama**, lighting up only when those tools are present.

## Considered Options

- **Anthropic API** — best DX, but requires API credits the user doesn't have and a
  managed key; rejected.
- **Fully local LLM only** — preserves "fully local" but small models are weakest at
  the compose step (draft an email, pick the right note), and command actions
  (email, GitHub) already require network anyway.
- **`claude -p` + Ollama (chosen)** — top-tier quality with no API bill via the
  existing subscription, local option for the privacy-minded, and a clean seam to add
  backends later.

## Consequences

- Hard dependency on the `claude` CLI being installed and logged in for the Claude
  backend (same posture as the `gh` dependency in ADR-0003-adjacent integration work).
- The AI engine is a subprocess call, not an SDK; auth is whatever `claude` is logged
  into, so there is no key management.
- Per-step Model is selectable (Claude model defaults to Sonnet); the global Routing
  Pass model defaults to a fast option (Haiku or a small Ollama model).
