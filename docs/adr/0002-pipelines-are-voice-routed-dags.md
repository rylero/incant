# Pipelines are voice-routed DAGs

The automation layer is built as user-authored **DAG pipelines** triggered by voice.
A separate **command-mode hotkey** marks an utterance as a command (never typed),
and a **Routing Pass** selects which pipeline to run by matching the transcript
against each pipeline's name + "when to use" description — the same mechanism as
Claude Code skill selection ("a voice skill system"). The chosen pipeline is a graph
of steps (AI + integration Action Types) with **splits** (an AI step emits a
separate, format-tailored payload per outgoing edge) and **merges** (named inbound
handles, wait-for-all join), passing **structured payloads** where integration steps
declare the fields they require.

## Considered Options

- **Trigger boundary**: separate command hotkey (chosen) vs wake-phrase prefix vs
  AI-classify-everything vs per-pipeline trigger phrases. The hotkey keeps fast
  local dictation untouched and gives commands an unambiguous channel; the others
  remain possible future layers on top.
- **Automation shape**: typed DAG of app-shipped step types (chosen) vs an open
  tool-calling agent. The DAG keeps execution auditable and the routing a cheap,
  reliable selection problem; an open agent was rejected for v1 as non-deterministic
  and hard to gate safely.

## Consequences

- Safety is layered: a per-step **Guard** (approve / refactor-with-AI / stop) for
  human-in-the-loop review, and a **Delay Until Finished** flag that defers
  irreversible steps to the end so an upstream failure prevents them from running.
- Failure policy is **halt-and-surface** for v1 (stop the pipeline, report completed
  vs not). Per-branch failure and retries are deferred.
- On no route match, the transcript is AI-cleaned and typed at the cursor — never raw,
  never silent.
- One pipeline runs at a time; command mode is blocked while a run/guard is active.
- Pipelines are stored as portable folders under `pipelines/` (JSON manifest +
  markdown system prompts); secrets are referenced as named Credentials kept outside
  the folder.
