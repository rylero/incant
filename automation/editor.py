"""Pipeline Editor — a hand-rolled node-graph editor on a tkinter Canvas.

ADR-0003: incant ships its own native node editor rather than embedding a web
view. CustomTkinter has no canvas widget, so the scene is a plain
``tkinter.Canvas`` embedded in a CustomTkinter window; the palette, toolbar and
inspector around it are CustomTkinter widgets.

The editor only reads and writes the portable pipeline-folder format the engine
already consumes (``automation.manifest``). Node layout (x/y) round-trips through
the manifest as harmless extra keys, so a saved pipeline reopens where you left
it. Nothing here knows how to *run* a pipeline — that stays in the engine.

Run standalone with ``python -m automation.editor`` or open it from the app
header via :func:`open_editor`.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import customtkinter as ctk

from .manifest import (
    Edge,
    Node,
    Pipeline,
    discover_pipelines,
    load_pipeline,
    save_pipeline,
)
from .actions import AI_TYPE, REGISTRY

# --- scene geometry / palette ------------------------------------------------

NODE_W = 170
NODE_H = 76
PORT_R = 7                                   # port hit/draw radius

BG = "#242424"
NODE_FILL = "#3a3a3a"
NODE_AI_FILL = "#36506b"                     # AI steps tinted blue-grey
NODE_OUTLINE = "#555555"
SELECT_OUTLINE = "#3b8ed0"
PORT_FILL = "#9aa0a6"
EDGE_COLOR = "#8a8a8a"
EDGE_SELECT = "#3b8ed0"
TEXT_COLOR = "#e6e6e6"
SUBTEXT_COLOR = "#a8a8a8"

# Action types offered in the palette. "ai" is not in REGISTRY (engine-special),
# so it is listed explicitly first.
PALETTE: list[tuple[str, str]] = [
    (AI_TYPE, "AI step"),
    ("save_file", "Save to File"),
    ("type_cursor", "Type at Cursor"),
    ("github_issue", "GitHub Issue"),
    ("send_email", "Send Email"),
]

# Per-type config fields shown in the inspector: (key, label, kind).
# kind: "text" | "bool". AI handles its own (model + system prompt) specially.
CONFIG_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "save_file": [
        ("base_dir", "Base dir", "text"),
        ("path_field", "Path field", "text"),
        ("content_field", "Content field", "text"),
        ("append", "Append", "bool"),
    ],
    "type_cursor": [
        ("text_field", "Text field", "text"),
    ],
    "github_issue": [
        ("repo", "Repo (owner/name)", "text"),
    ],
    "send_email": [
        ("credential", "Credential name", "text"),
        ("from", "From (override)", "text"),
    ],
}

CLAUDE_MODELS = ["sonnet", "opus", "haiku"]
BACKENDS = ["claude", "ollama", "fake"]


def _type_label(t: str) -> str:
    for key, label in PALETTE:
        if key == t:
            return label
    return t


class PipelineEditor(ctk.CTkFrame):
    """The editor widget. Embed in any CTk parent or use :func:`main`."""

    def __init__(self, master, folder: str | Path | None = None):
        super().__init__(master, fg_color="transparent")

        self.pipeline = Pipeline(name="untitled", when_to_use="")
        self.selected_node: str | None = None
        self.selected_edge: tuple[str, str, str] | None = None

        # interaction state
        self._mode: str | None = None           # node | edge | pan
        self._drag_node: str | None = None
        self._drag_dx = 0
        self._drag_dy = 0
        self._edge_from: str | None = None
        self._temp_line: int | None = None
        self._new_counter = 0

        self._build_layout()

        if folder:
            self._load(folder)
        else:
            self._refresh_pipeline_fields()
            self.redraw()

    # -- layout ----------------------------------------------------------------
    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # toolbar across the top
        bar = ctk.CTkFrame(self)
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", padx=6, pady=(6, 0))
        ctk.CTkButton(bar, text="New", width=60, command=self.new_pipeline).pack(side="left", padx=4, pady=6)
        ctk.CTkButton(bar, text="Open", width=60, command=self.open_dialog).pack(side="left", padx=4, pady=6)
        ctk.CTkButton(bar, text="Save", width=60, command=self.save).pack(side="left", padx=4, pady=6)
        ctk.CTkButton(bar, text="Delete", width=70, fg_color="#7a3b3b",
                      hover_color="#974646", command=self.delete_selected).pack(side="left", padx=4, pady=6)
        self.status = ctk.CTkLabel(bar, text="", text_color=SUBTEXT_COLOR)
        self.status.pack(side="right", padx=10)

        # palette strip on the left
        palette = ctk.CTkFrame(self, width=140)
        palette.grid(row=1, column=0, sticky="ns", padx=(6, 0), pady=6)
        ctk.CTkLabel(palette, text="Add step", text_color=SUBTEXT_COLOR).pack(pady=(8, 4))
        for key, label in PALETTE:
            ctk.CTkButton(palette, text=label, width=120,
                          command=lambda k=key: self.add_node(k)).pack(padx=10, pady=3)

        # canvas scene in the centre
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.canvas.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        # inspector on the right
        self.inspector = ctk.CTkScrollableFrame(self, width=280, label_text="Inspector")
        self.inspector.grid(row=1, column=2, sticky="ns", padx=(0, 6), pady=6)
        self._build_inspector()

    def _build_inspector(self) -> None:
        insp = self.inspector

        ctk.CTkLabel(insp, text="Pipeline", anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        self.name_var = tk.StringVar()
        ctk.CTkEntry(insp, textvariable=self.name_var, placeholder_text="name").pack(fill="x", padx=8, pady=2)
        ctk.CTkLabel(insp, text="When to use (routing)", anchor="w",
                     text_color=SUBTEXT_COLOR).pack(fill="x", padx=8, pady=(4, 0))
        self.when_box = ctk.CTkTextbox(insp, height=70)
        self.when_box.pack(fill="x", padx=8, pady=2)

        # node-specific section gets rebuilt each selection
        self.node_section = ctk.CTkFrame(insp, fg_color="transparent")
        self.node_section.pack(fill="x", padx=0, pady=(8, 6))

    # -- pipeline-level fields -------------------------------------------------
    def _refresh_pipeline_fields(self) -> None:
        self.name_var.set(self.pipeline.name)
        self.when_box.delete("1.0", "end")
        self.when_box.insert("1.0", self.pipeline.when_to_use)

    def _commit_pipeline_fields(self) -> None:
        self.pipeline.name = self.name_var.get().strip() or "untitled"
        self.pipeline.when_to_use = self.when_box.get("1.0", "end").strip()

    # -- node creation / deletion ---------------------------------------------
    def _unique_id(self, base: str) -> str:
        existing = {n.id for n in self.pipeline.nodes}
        if base not in existing:
            return base
        i = 2
        while f"{base}-{i}" in existing:
            i += 1
        return f"{base}-{i}"

    def add_node(self, type_key: str) -> None:
        self._new_counter += 1
        node = Node(id=self._unique_id(type_key), type=type_key)
        # stagger new nodes so they don't stack exactly
        node.x = 60 + (self._new_counter % 6) * 30
        node.y = 60 + (self._new_counter % 6) * 30
        if type_key == AI_TYPE:
            node.config = {"model": {"backend": "claude", "model": "sonnet"}}
        self.pipeline.nodes.append(node)
        self.select_node(node.id)
        self.redraw()

    def delete_selected(self) -> None:
        if self.selected_node:
            nid = self.selected_node
            self.pipeline.nodes = [n for n in self.pipeline.nodes if n.id != nid]
            self.pipeline.edges = [e for e in self.pipeline.edges
                                   if e.src != nid and e.dst != nid]
            self.selected_node = None
            self._clear_node_section()
        elif self.selected_edge:
            s, d, h = self.selected_edge
            self.pipeline.edges = [e for e in self.pipeline.edges
                                   if not (e.src == s and e.dst == d and e.handle == h)]
            self.selected_edge = None
        self.redraw()

    # -- new / open / save -----------------------------------------------------
    def new_pipeline(self) -> None:
        self.pipeline = Pipeline(name="untitled", when_to_use="")
        self.selected_node = self.selected_edge = None
        self._clear_node_section()
        self._refresh_pipeline_fields()
        self.redraw()
        self._set_status("new pipeline")

    def open_dialog(self) -> None:
        pipes = discover_pipelines()
        if not pipes:
            self._set_status("no pipelines found under ./pipelines")
            return
        win = ctk.CTkToplevel(self)
        win.title("Open pipeline")
        win.geometry("320x360")
        win.transient(self.winfo_toplevel())
        ctk.CTkLabel(win, text="Pick a pipeline").pack(pady=8)
        for p in pipes:
            folder = p.folder

            def pick(f=folder, w=win):
                w.destroy()
                self._load(f)

            ctk.CTkButton(win, text=p.name, command=pick).pack(fill="x", padx=16, pady=3)
        win.after(60, win.lift)

    def _load(self, folder: str | Path) -> None:
        self.pipeline = load_pipeline(folder)
        self.selected_node = self.selected_edge = None
        self._clear_node_section()
        self._auto_layout_if_needed()
        self._refresh_pipeline_fields()
        self.redraw()
        self._set_status(f"opened {self.pipeline.name}")

    def _auto_layout_if_needed(self) -> None:
        """Give nodes a left-to-right layout if the manifest carried no x/y."""
        if any(n.x or n.y for n in self.pipeline.nodes):
            return
        # depth = longest path from a root; lay columns by depth
        depth: dict[str, int] = {}

        def d(nid: str) -> int:
            if nid in depth:
                return depth[nid]
            ins = self.pipeline.in_edges(nid)
            depth[nid] = 0 if not ins else 1 + max(d(e.src) for e in ins)
            return depth[nid]

        col_count: dict[int, int] = {}
        for n in self.pipeline.nodes:
            c = d(n.id)
            row = col_count.get(c, 0)
            col_count[c] = row + 1
            n.x = 60 + c * (NODE_W + 80)
            n.y = 60 + row * (NODE_H + 50)

    def save(self) -> None:
        self._commit_pipeline_fields()
        if self.selected_node:
            self._commit_node_section()
        try:
            folder = save_pipeline(self.pipeline)
        except Exception as exc:                       # surface, don't crash editor
            self._set_status(f"save failed: {exc}")
            return
        self._set_status(f"saved → {folder}")

    # -- selection -------------------------------------------------------------
    def select_node(self, nid: str) -> None:
        if self.selected_node and self.selected_node != nid:
            self._commit_node_section()
        self.selected_node = nid
        self.selected_edge = None
        self._build_node_section()

    def select_edge(self, key: tuple[str, str, str]) -> None:
        if self.selected_node:
            self._commit_node_section()
        self.selected_node = None
        self.selected_edge = key
        self._clear_node_section()

    def _clear_node_section(self) -> None:
        for w in self.node_section.winfo_children():
            w.destroy()

    # -- inspector: node section ----------------------------------------------
    def _build_node_section(self) -> None:
        self._clear_node_section()
        node = self.pipeline.node(self.selected_node)
        sec = self.node_section

        ctk.CTkLabel(sec, text=f"{_type_label(node.type)}", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(fill="x", padx=8, pady=(2, 4))

        ctk.CTkLabel(sec, text="Step id", anchor="w", text_color=SUBTEXT_COLOR).pack(fill="x", padx=8)
        self.id_var = tk.StringVar(value=node.id)
        ctk.CTkEntry(sec, textvariable=self.id_var).pack(fill="x", padx=8, pady=2)

        self.guard_var = tk.BooleanVar(value=node.guard)
        ctk.CTkCheckBox(sec, text="Guard (review before run)",
                        variable=self.guard_var).pack(fill="x", padx=8, pady=(6, 2))
        self.delay_var = tk.BooleanVar(value=node.delay_until_finished)
        ctk.CTkCheckBox(sec, text="Delay until finished",
                        variable=self.delay_var).pack(fill="x", padx=8, pady=2)

        self._cfg_vars: dict[str, tk.Variable] = {}
        self._prompt_box = None
        self._backend_var = None
        self._model_var = None

        if node.type == AI_TYPE:
            self._build_ai_fields(sec, node)
        else:
            self._build_config_fields(sec, node)

        ctk.CTkButton(sec, text="Apply", command=self._apply_node).pack(fill="x", padx=8, pady=(10, 4))

        req = self._required_for(node.type)
        if req:
            ctk.CTkLabel(sec, text=f"needs: {', '.join(req)}", anchor="w",
                         text_color=SUBTEXT_COLOR).pack(fill="x", padx=8, pady=(0, 4))

    def _build_ai_fields(self, sec, node: Node) -> None:
        model_cfg = node.config.get("model", {})
        ctk.CTkLabel(sec, text="Backend", anchor="w", text_color=SUBTEXT_COLOR).pack(fill="x", padx=8, pady=(6, 0))
        self._backend_var = tk.StringVar(value=model_cfg.get("backend", "claude"))
        ctk.CTkOptionMenu(sec, values=BACKENDS, variable=self._backend_var).pack(fill="x", padx=8, pady=2)

        ctk.CTkLabel(sec, text="Model", anchor="w", text_color=SUBTEXT_COLOR).pack(fill="x", padx=8, pady=(4, 0))
        self._model_var = tk.StringVar(value=model_cfg.get("model", "sonnet"))
        # editable combo: claude names suggested, but ollama models are freeform
        ctk.CTkComboBox(sec, values=CLAUDE_MODELS, variable=self._model_var).pack(fill="x", padx=8, pady=2)

        ctk.CTkLabel(sec, text="System prompt", anchor="w",
                     text_color=SUBTEXT_COLOR).pack(fill="x", padx=8, pady=(6, 0))
        self._prompt_box = ctk.CTkTextbox(sec, height=140)
        self._prompt_box.pack(fill="x", padx=8, pady=2)
        self._prompt_box.insert("1.0", node.system_prompt)

    def _build_config_fields(self, sec, node: Node) -> None:
        for key, label, kind in CONFIG_FIELDS.get(node.type, []):
            if kind == "bool":
                var = tk.BooleanVar(value=bool(node.config.get(key, False)))
                ctk.CTkCheckBox(sec, text=label, variable=var).pack(fill="x", padx=8, pady=(6, 2))
            else:
                ctk.CTkLabel(sec, text=label, anchor="w",
                             text_color=SUBTEXT_COLOR).pack(fill="x", padx=8, pady=(6, 0))
                var = tk.StringVar(value=str(node.config.get(key, "")))
                ctk.CTkEntry(sec, textvariable=var).pack(fill="x", padx=8, pady=2)
            self._cfg_vars[key] = var

    def _commit_node_section(self) -> None:
        """Write inspector fields back to the selected node (silent; no rebuild)."""
        if not self.selected_node:
            return
        try:
            node = self.pipeline.node(self.selected_node)
        except KeyError:
            return
        self._apply_node(rebuild=False)

    def _apply_node(self, rebuild: bool = True) -> None:
        node = self.pipeline.node(self.selected_node)

        new_id = self.id_var.get().strip()
        if new_id and new_id != node.id:
            if any(n.id == new_id for n in self.pipeline.nodes):
                self._set_status(f"id {new_id!r} already in use")
            else:
                old = node.id
                for e in self.pipeline.edges:
                    if e.src == old:
                        e.src = new_id
                    if e.dst == old:
                        e.dst = new_id
                node.id = new_id
                self.selected_node = new_id

        node.guard = self.guard_var.get()
        node.delay_until_finished = self.delay_var.get()

        if node.type == AI_TYPE:
            node.config["model"] = {
                "backend": self._backend_var.get(),
                "model": self._model_var.get().strip() or "sonnet",
            }
            if self._prompt_box is not None:
                node.system_prompt = self._prompt_box.get("1.0", "end").strip()
        else:
            cfg = dict(node.config)
            for key, var in self._cfg_vars.items():
                val = var.get()
                if isinstance(val, bool):
                    cfg[key] = val
                elif str(val).strip():
                    cfg[key] = str(val).strip()
                else:
                    cfg.pop(key, None)
            node.config = cfg

        if rebuild:
            self.redraw()
            self._set_status(f"applied {node.id}")

    def _required_for(self, type_key: str) -> list[str]:
        if type_key == AI_TYPE:
            return []
        action = REGISTRY.get(type_key)
        return list(getattr(action, "required_inputs", [])) if action else []

    # -- canvas geometry helpers ----------------------------------------------
    def _node_box(self, n: Node) -> tuple[int, int, int, int]:
        return n.x, n.y, n.x + NODE_W, n.y + NODE_H

    def _in_port(self, n: Node) -> tuple[int, int]:
        return n.x, n.y + NODE_H // 2

    def _out_port(self, n: Node) -> tuple[int, int]:
        return n.x + NODE_W, n.y + NODE_H // 2

    # -- drawing ---------------------------------------------------------------
    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        for e in self.pipeline.edges:
            self._draw_edge(e)
        for n in self.pipeline.nodes:
            self._draw_node(n)

    def _draw_node(self, n: Node) -> None:
        c = self.canvas
        x0, y0, x1, y1 = self._node_box(n)
        fill = NODE_AI_FILL if n.type == AI_TYPE else NODE_FILL
        outline = SELECT_OUTLINE if n.id == self.selected_node else NODE_OUTLINE
        width = 2 if n.id == self.selected_node else 1
        tag = f"node:{n.id}"

        self._rounded(x0, y0, x1, y1, 12, fill=fill, outline=outline, width=width, tags=(tag,))
        c.create_text(x0 + 12, y0 + 16, anchor="w", fill=TEXT_COLOR,
                      text=_type_label(n.type), font=("Segoe UI", 10, "bold"), tags=(tag,))
        c.create_text(x0 + 12, y0 + 36, anchor="w", fill=SUBTEXT_COLOR,
                      text=n.id, font=("Segoe UI", 9), tags=(tag,))
        flags = []
        if n.guard:
            flags.append("guard")
        if n.delay_until_finished:
            flags.append("delay")
        if flags:
            c.create_text(x0 + 12, y0 + 56, anchor="w", fill="#d0a85a",
                          text=" · ".join(flags), font=("Segoe UI", 8), tags=(tag,))

        ix, iy = self._in_port(n)
        ox, oy = self._out_port(n)
        c.create_oval(ix - PORT_R, iy - PORT_R, ix + PORT_R, iy + PORT_R,
                      fill=PORT_FILL, outline="", tags=(tag, f"portin:{n.id}"))
        c.create_oval(ox - PORT_R, oy - PORT_R, ox + PORT_R, oy + PORT_R,
                      fill=PORT_FILL, outline="", tags=(tag, f"portout:{n.id}"))

    def _draw_edge(self, e: Edge) -> None:
        try:
            src = self.pipeline.node(e.src)
            dst = self.pipeline.node(e.dst)
        except KeyError:
            return
        x0, y0 = self._out_port(src)
        x1, y1 = self._in_port(dst)
        key = (e.src, e.dst, e.handle)
        selected = key == self.selected_edge
        color = EDGE_SELECT if selected else EDGE_COLOR
        tag = f"edge:{e.src}|{e.dst}|{e.handle}"
        # cubic-ish curve via a midpoint control to read as a flow line
        mx = (x0 + x1) / 2
        self.canvas.create_line(
            x0, y0, mx, y0, mx, y1, x1, y1,
            fill=color, width=2 if selected else 2, smooth=True,
            arrow="last", tags=(tag, "edge"),
        )
        if e.handle and e.handle != "in":
            self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2 - 8, fill=SUBTEXT_COLOR,
                                    text=e.handle, font=("Segoe UI", 8), tags=(tag,))

    def _rounded(self, x0, y0, x1, y1, r, **kw):
        pts = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    # -- hit testing -----------------------------------------------------------
    def _hit(self, cx: float, cy: float):
        items = self.canvas.find_overlapping(cx - 2, cy - 2, cx + 2, cy + 2)
        for item in reversed(items):                 # topmost first
            for t in self.canvas.gettags(item):
                if t.startswith("portout:"):
                    return ("portout", t.split(":", 1)[1])
                if t.startswith("portin:"):
                    return ("portin", t.split(":", 1)[1])
        for item in reversed(items):
            for t in self.canvas.gettags(item):
                if t.startswith("node:"):
                    return ("node", t.split(":", 1)[1])
                if t.startswith("edge:"):
                    s, d, h = t.split(":", 1)[1].split("|")
                    return ("edge", (s, d, h))
        return None

    # -- mouse events ----------------------------------------------------------
    def _on_press(self, ev) -> None:
        cx, cy = self.canvas.canvasx(ev.x), self.canvas.canvasy(ev.y)
        hit = self._hit(cx, cy)

        if hit and hit[0] == "portout":
            self._mode = "edge"
            self._edge_from = hit[1]
            sx, sy = self._out_port(self.pipeline.node(hit[1]))
            self._temp_line = self.canvas.create_line(sx, sy, cx, cy,
                                                      fill=SELECT_OUTLINE, width=2, dash=(4, 3))
        elif hit and hit[0] == "node":
            self._mode = "node"
            self._drag_node = hit[1]
            n = self.pipeline.node(hit[1])
            self._drag_dx = cx - n.x
            self._drag_dy = cy - n.y
            self.select_node(hit[1])
            self.redraw()
        elif hit and hit[0] == "edge":
            self._mode = None
            self.select_edge(hit[1])
            self.redraw()
        else:
            self._mode = "pan"
            self.canvas.scan_mark(ev.x, ev.y)
            if self.selected_node or self.selected_edge:
                self.selected_node = self.selected_edge = None
                self._clear_node_section()
                self.redraw()

    def _on_motion(self, ev) -> None:
        cx, cy = self.canvas.canvasx(ev.x), self.canvas.canvasy(ev.y)
        if self._mode == "node" and self._drag_node:
            n = self.pipeline.node(self._drag_node)
            n.x = int(cx - self._drag_dx)
            n.y = int(cy - self._drag_dy)
            self.redraw()
        elif self._mode == "edge" and self._temp_line is not None:
            sx, sy = self._out_port(self.pipeline.node(self._edge_from))
            self.canvas.coords(self._temp_line, sx, sy, cx, cy)
        elif self._mode == "pan":
            self.canvas.scan_dragto(ev.x, ev.y, gain=1)

    def _on_release(self, ev) -> None:
        cx, cy = self.canvas.canvasx(ev.x), self.canvas.canvasy(ev.y)
        if self._mode == "edge":
            if self._temp_line is not None:
                self.canvas.delete(self._temp_line)
                self._temp_line = None
            hit = self._hit(cx, cy)
            if hit and hit[0] == "portin" and hit[1] != self._edge_from:
                self._add_edge(self._edge_from, hit[1])
            self._edge_from = None
        self._mode = None
        self._drag_node = None

    def _add_edge(self, src: str, dst: str) -> None:
        existing = self.pipeline.in_edges(dst)
        if any(e.src == src for e in existing):
            self._set_status("edge already exists")
            return
        # auto-name inbound handle for merges: in, in2, in3, ...
        handle = "in"
        if existing:
            handle = f"in{len(existing) + 1}"
        self.pipeline.edges.append(Edge(src=src, dst=dst, handle=handle))
        self.redraw()
        self._set_status(f"{src} → {dst} [{handle}]")

    # -- misc ------------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)


def open_editor(parent_app) -> ctk.CTkToplevel:
    """Open the editor in a Toplevel over the running app."""
    win = ctk.CTkToplevel(parent_app)
    win.title("incant — Pipeline Editor")
    win.geometry("1100x680")
    editor = PipelineEditor(win)
    editor.pack(fill="both", expand=True)
    win.after(120, win.lift)
    return win


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    root.title("incant — Pipeline Editor")
    root.geometry("1100x680")
    editor = PipelineEditor(root)
    editor.pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
