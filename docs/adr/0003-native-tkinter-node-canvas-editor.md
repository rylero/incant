# Native Tkinter node-canvas editor

The Pipeline Editor is built as a **hand-rolled node-graph canvas on Tkinter
`Canvas`** (node drag, edge drawing, hit-testing, pan/zoom), rather than embedding a
web-based node library (React Flow / Svelte Flow) or avoiding a visual graph. This
keeps the entire app a single native CustomTkinter program, consistent with the
runtime UI, at the cost of being the heaviest single build in the plan.

## Considered Options

- **Local web editor (React Flow)** — easiest, nicest splits/merges UX, but adds a
  second tech stack (bundled web app + local server + IPC) and a separate window
  paradigm. Rejected for cohesion, despite being the lower-effort path.
- **Structured non-visual editor** — declare nodes/edges in forms; buildable fast,
  but doesn't deliver the drag-and-wire UX the feature is about.
- **Hand-rolled Tkinter canvas (chosen)** — most effort and hardest to make look
  polished, but one app, one stack, native feel.

## Consequences

- Expect significant editor-only engineering; the canvas should serialize to the same
  plain pipeline-folder format any editor would, so the choice is reversible later.
- The node editor is decoupled from the execution engine — the engine can be built
  and tested against pipeline manifests before the canvas is finished.
