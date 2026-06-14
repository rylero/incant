# Variables and templated fields (pull data model)

Data inside a pipeline moves by **pull, not push**. Every step writes its outputs
into a run-wide **Variable** store keyed by step id (`speech_text` from the input,
`compose.title` from an AI step, …). Every input field of every step is a
**Template** — literal text with `{{ node.field }}` placeholders substituted from
that store when the step runs. A static value (e.g. always email a fixed address)
is just a template with no placeholders; a referenced value (`{{ compose.recipient }}`)
and a mix (`notes/{{ input.speech_text }}.md`) use the same machinery.

This **supersedes the Split/Payload half of ADR-0002**. Previously an AI step was
"shaped to produce exactly its downstream's fields" and re-ran the model once per
outgoing edge to tailor a payload. Now an AI step declares a user-defined
**Output Schema** (named fields) and emits them once as Variables; each downstream
branch templates whichever it needs. The transcript stops entering implicitly at
graph roots — an explicit **Input Source** node introduces `speech_text`, so future
sources (clipboard, selection, date) can be added and combined.

## Considered Options

- **Keep push (per-edge AI tailoring)** — the ADR-0002 model: AI re-generates a
  tailored payload per outgoing edge. Rejected: two mechanisms (generation +
  routing), N model calls on an N-way split, and no way to set a field statically
  or reference a value from a non-adjacent step.
- **Hybrid (templates alongside per-edge tailoring)** — keep both. Rejected:
  overlapping mechanisms to learn, document, and debug, for no capability the pull
  model lacks.
- **Pull (chosen)** — one mechanism. AI steps are just producers of named
  Variables; every field is a template. Splits and merges need no special engine
  code, and a field can be literal, AI-produced, or a mix.

## Consequences

- **AI Output Schema**: an AI step carries a user-defined list of output fields; the
  Model is instructed to return exactly those as JSON (reusing `complete_json`),
  which become its Variables. This is also what the editor's variable picker lists.
- **Templating is minimal**: `{{ node.field }}` substitution only — no filters,
  logic, or arithmetic in v1. Qualified names (`{{ input.speech_text }}`, not bare
  `{{ speech_text }}`) so combining multiple Input Sources can't collide.
- **Missing variable fails the run** (halt-and-surface), never silently empty.
- **Edges now mean execution order + scope**, not data flow: a step waits for its
  inbound edges (Kahn topo, unchanged) and may template Variables from any completed
  ancestor. Splits/merges fall out for free; the `Edge.handle` name becomes a
  cosmetic label rather than a data-routing requirement.
- **Engine reframe**: the per-edge `edge_outputs` map becomes a single
  `vars[node_id][field]` store; each node renders its config/prompt templates against
  it, runs, and writes its outputs back. Guard and Delay Until Finished are unchanged.
- The manifest gains AI `outputs` and per-field templates; existing pipelines
  (save-note) migrate to an explicit Input Source + templated fields.
