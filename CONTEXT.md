# incant

Local-first voice transcription with voice-triggered automation. Speak and have
text typed at your cursor, or enter command mode to trigger an action by voice.

## Language

**Dictation**:
Spoken audio transcribed and typed at the cursor verbatim. The default behavior,
on its own hotkey. Untouched by the automation layer.
_Avoid_: transcription (that's the act; dictation is the typed-out mode)

**Command Mode**:
A distinct mode entered by a separate hotkey. Everything spoken in command mode
is treated as an instruction to be interpreted, never typed at the cursor.
_Avoid_: dictation mode, voice command (too generic)

**Routing Pass**:
The first AI step inside command mode. Reads the command-mode transcript and
selects which Pipeline the user asked for — if any. Works like Claude Code skill
selection: it matches the transcript against each pipeline's name + "when to use
this" description and picks the best one, or none. Uses a global router Model
(runs before any pipeline is chosen, so it cannot use a per-step Model); the
router defaults to a fast option (Claude Haiku via `claude -p`, or a small
Ollama model). On no match, the fallback is to run the transcript through an AI
cleanup and type the result at the cursor — never raw, never silent.
_Avoid_: classifier, intent parser

**Action Type**:
One kind of step that can appear in a pipeline — a building block from an
app-shipped catalog. Two families: AI steps that reshape text, and integration
steps that perform an external effect. An AI step is configured with an editable
system prompt, a Model, and a user-defined Output Schema. v1 catalog: Input
Source, AI step, Save to File System, Type at Cursor, Create GitHub Issue, Send
Email. (Saving an Obsidian note is just a File System write to a vault path — no
separate step.)
_Avoid_: command, skill, integration

**Pipeline**:
A user-authored directed acyclic graph (DAG) of steps (each step an Action Type)
that runs when the Routing Pass selects it in command mode. It begins from one or
more Input Source steps that introduce Variables (v1: the transcript's
`speech_text`). Supports splits (one step's outputs feed several) and merges (a
step pulls Variables from several). Built in the Pipeline Editor.
Stored as a self-contained folder under `pipelines/`: a JSON manifest (the DAG,
per-step config, name + "when to use" description) plus AI system prompts as
markdown. Portable — sharing a pipeline is sharing its folder.
_Avoid_: automation, workflow, shortcut, sequence

**Credential**:
A named secret (email login, GitHub token, …) stored separately from pipeline
folders, in an OS keyring or gitignored store. Pipelines reference it by name so
they stay shareable and version-controllable without leaking auth.
_Avoid_: secret, token, key

**Pipeline Editor**:
A native node-graph canvas (built on Tkinter `Canvas`) where the user builds a
Pipeline — drops step nodes, wires edges (splits/merges), configures each step,
sets the Model and system prompt for AI steps, and writes how the Routing Pass
should match the pipeline.

**Model**:
The AI backend that powers AI steps (and the Routing Pass). Two backends: a
local Ollama model, or Claude via the `claude -p` CLI (headless, on the user's
subscription — not the Anthropic API). For the `claude -p` backend the user
picks which Claude model; the default is Sonnet.
_Avoid_: provider, LLM, API

**Variable**:
A named value a step produces, addressed as `node_id.field` (e.g.
`input.speech_text`, `compose.title`). Every step writes its outputs into a
run-wide store keyed by step id; downstream steps pull Variables by name through
Templates. Data moves by pull, not by a payload pushed along an edge (ADR-0004).
_Avoid_: parameter, slot, payload field

**Template**:
A field value written as literal text with `{{ node.field }}` placeholders that
are substituted with Variable values when the step runs. _Every_ input field of
_every_ step is a Template: a static value (always email a fixed address) is a
Template with no placeholders; a referenced value (`{{ compose.recipient }}`) or
a mix (`notes/{{ input.speech_text }}.md`) use the same machinery. Substitution
only — no filters or logic in v1. A missing Variable fails the run
(halt-and-surface), never silently empty.
_Avoid_: expression, interpolation, binding

**Output Schema**:
The user-defined list of named output fields on an AI step. Its Model is
instructed to return exactly those fields as JSON; each becomes a Variable
(`node.field`). This is also what the editor's variable picker offers downstream.
_Avoid_: return type, response format

**Input Source**:
A root step that introduces external Variables into a pipeline — v1: the
`speech_text` from the command-mode transcript. Shown as an explicit node so
future sources (clipboard, selection, date) can be added and combined. Replaces
the transcript entering implicitly at graph roots.
_Avoid_: trigger, source, entry

**Split**:
A step with several outgoing edges. The step emits its Variables once; each
branch's downstream step Templates whichever it needs — no copying and no
per-edge model re-run (ADR-0004 superseded the old per-edge tailoring).
_Avoid_: fan-out, branch, fork

**Merge**:
A step with several inbound edges that Templates Variables from several upstream
steps. Wait-for-all join: the step runs only once every inbound branch has
completed. Edges define order and scope; the data itself is pulled by name.
_Avoid_: fan-in, join, combine

**Delay Until Finished**:
A per-step flag that defers a step until every other step in the pipeline has
completed successfully — used to push an irreversible step (e.g. Send Email) to
the very end, so an upstream failure (halt-and-surface) prevents it from ever
running. A delayed step is a sink: it can have no outgoing edges.
_Avoid_: defer, final step, last

**Guard**:
A per-step toggle, set in the editor, that pauses a pipeline before the step
runs. It opens a popup showing the rendered field values the step is about to use
and what the next step is, offering three actions: Approve (continue), Refactor
with AI (re-run the upstream AI step to regenerate its Variables from feedback),
or Stop (abort the whole pipeline).
_Avoid_: confirmation, gate, checkpoint
