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
system prompt, a Model, and the output fields inferred from its downstream
steps. v1 catalog: AI step, Save to File System, Type at Cursor, Create GitHub
Issue, Send Email. (Saving an Obsidian note is just a File System write to a
vault path — no separate step.)
_Avoid_: command, skill, integration

**Pipeline**:
A user-authored directed acyclic graph (DAG) of steps (each step an Action Type)
that runs when the Routing Pass selects it in command mode. Its input is the
command-mode transcript. Supports splits (one step's output fans out to several)
and merges (a step consumes the output of several). Built in the Pipeline Editor.
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

**Payload**:
The structured set of named fields passed along an edge between steps. An
integration step declares the fields it requires (email: `to`, `subject`,
`body`); an AI step placed before it is shaped to produce exactly those fields.
A merge step receives a Payload per inbound edge.
_Avoid_: message, data, blob

**Split**:
A step with several outgoing edges. Outputs are not copied — each branch's
downstream step may need a different format, so an AI step generates a separate,
tailored Payload per outgoing edge, each shaped to that branch's required fields.
_Avoid_: fan-out, branch, fork

**Merge**:
A step (in practice always an AI step) with several inbound edges. Each inbound
edge is a named handle the step references by name. Wait-for-all join: the step
runs only once every inbound branch has produced its Payload.
_Avoid_: fan-in, join, combine

**Delay Until Finished**:
A per-step flag that defers a step until every other step in the pipeline has
completed successfully — used to push an irreversible step (e.g. Send Email) to
the very end, so an upstream failure (halt-and-surface) prevents it from ever
running. A delayed step is a sink: it can have no outgoing edges.
_Avoid_: defer, final step, last

**Guard**:
A per-step toggle, set in the editor, that pauses a pipeline before the step
runs. It opens a popup showing the Payload the step is about to use and what the
next step is, offering three actions: Approve (continue), Refactor with AI
(regenerate the Payload from feedback), or Stop (abort the whole pipeline).
_Avoid_: confirmation, gate, checkpoint
