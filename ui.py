"""
incant — local-first voice transcription with automation.

Control panel: set the hotkey, pick a model and typing mode, and (under
Advanced) tune performance. Press the hotkey anywhere to dictate into the
focused app. The Activity log opens in its own window.

Run:  uv run ui
"""

from __future__ import annotations

import ctypes
import datetime
import json
import math
import os
import queue
import random
import threading
import tkinter as tk
from pathlib import Path

import numpy as np
import keyboard
import customtkinter as ctk
from PIL import Image

import re
import uuid

import stt  # sets up CUDA DLLs on import
import corrections
import history

# Automation layer (command mode): route a transcript to a registered n8n
# workflow and fire its webhook. See automation/ and docs/adr/.
from automation import credentials
from automation.command import run_command
from automation.models import make_model, ModelError
from automation.notifier import (
    ApprovalRequest,
    start as start_notifier,
    stop as stop_notifier,
    pop_approval,
    respond_approval,
)

# Settings live in %APPDATA% so they're writable even when the app is
# installed under Program Files (a non-admin user can't write next to __file__).
SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "incant" / "settings.json"
ASSETS = Path(__file__).with_name("assets")
ICON_PNG = ASSETS / "incant.png"
ICON_ICO = ASSETS / "incant.ico"

MODELS = {
    "small · fastest": "small",
    "medium · balanced": "medium",
    "large-v3 · most accurate": "large-v3",
}
OUTPUT_MODES = {
    "Continuous": "continuous",
    "Word by word": "word",
    "Insert at end": "capture",
}
LANGS = ["auto", "en", "es", "fr", "de", "it", "pt", "zh", "ja"]
DEFAULTS = {
    "hotkey": "ctrl+alt+space",
    "command_hotkey": "ctrl+alt+c",
    "model": "large-v3",
    "language": "",
    "output_mode": "continuous",
    "silence_s": 1.0,  # continuous pause-split gap
    "beam_size": 5,  # accuracy vs speed
    "preroll_s": 0.5,  # audio kept before keypress (first-word onset)
    "mic_rms": 0.004,  # continuous silence gate (mic sensitivity)
    "webhook_url": "",  # n8n webhook for command mode (empty = AI-clean + type)
    "review_hotkey": "ctrl+alt+r",  # pops overlay to correct the last transcript
    "history_mode": "full",         # "full" | "corrections" | "off"
    "snippets": {},  # phrase → expansion text mappings
}

STATUS_COLORS = {
    "load": "#e0a106",
    "warm": "#e0a106",
    "ready": "#2ecc71",
    "record": "#e74c3c",
    "listen": "#e74c3c",
    "transcrib": "#3498db",
    "press": "#2ecc71",
    "fail": "#e74c3c",
    "error": "#e74c3c",
}

# Dark palette — near-black, darker than CTk's default dark-mode grays.
BG = "#101010"          # root window background
SURFACE = "#1a1a1a"      # cards / pill / advanced panel
SURFACE_ALT = "#242424"  # buttons
HOVER = "#2f2f2f"        # button hover

# Overlay pill: small always-on-top waveform HUD shown while transcribing.
OVERLAY_W = 180
OVERLAY_H = 70
OVERLAY_BARS = 10
OVERLAY_BAR_WIDTH = 10
OVERLAY_PAD = 22
OVERLAY_MARGIN_BOTTOM = 56
OVERLAY_BG = "#0d0d0d"
OVERLAY_GAIN = 4.0  # multiplies sqrt(rms) before mapping to bar height
OVERLAY_KEY = "#fe01fe"  # chroma-key color -> transparent (Windows -transparentcolor)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kwargs):
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def _monitor_work_area(hwnd: int) -> tuple[int, int, int, int]:
    """Work-area rect (left, top, right, bottom) of the monitor containing hwnd.

    winfo_screenwidth/height always report the *primary* monitor, which
    places the overlay off-screen on multi-monitor setups where the app
    isn't on the primary display. This follows the app window instead.
    """
    MONITOR_DEFAULTTONEAREST = 2

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long), ("top", ctypes.c_long),
            ("right", ctypes.c_long), ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
            ("rcWork", RECT), ("dwFlags", ctypes.c_ulong),
        ]

    user32 = ctypes.windll.user32
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text()))
        except Exception:  # noqa: BLE001
            pass
    return s


def save_settings(s: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(s, indent=2))
    except Exception:  # noqa: BLE001
        pass


def _status_color(text: str) -> str:
    low = text.lower()
    for key, color in STATUS_COLORS.items():
        if key in low:
            return color
    return "#8a8a8a"


class App:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.settings = load_settings()
        self.snippets: dict[str, str] = dict(self.settings.get("snippets", {}))
        self.model = None
        self.model_lock = threading.Lock()
        self.engine = stt.AudioEngine(
            preroll_s=float(self.settings.get("preroll_s", 0.5))
        )
        self.busy = threading.Event()
        self.hotkey_handle = None
        self.ui_queue: queue.Queue = queue.Queue()
        # command mode (automation)
        self.command_on = False
        self.command_hotkey_handle = None
        # mode state
        self.continuous_on = False
        self.segmenter: stt.PhraseSegmenter | None = None
        self.stitcher: stt.TextStitcher | None = None
        self.phrase_queue: queue.Queue = queue.Queue()
        self.word_streamer: stt.WordStreamer | None = None
        # log
        self._log_lines: list[str] = []
        self._log_win: ctk.CTkToplevel | None = None
        self._snip_win: ctk.CTkToplevel | None = None
        self._log_box: ctk.CTkTextbox | None = None
        self.adv_open = False
        # corrections
        self._corrections: dict[str, str] = corrections.load_map()
        self._last_typed: str = ""
        self._review_hotkey_handle = None
        self._review_win: ctk.CTkToplevel | None = None
        # history
        self._session_id: str = str(uuid.uuid4())[:8]
        self._hist_win: ctk.CTkToplevel | None = None

        root.title("incant")
        root.geometry("540x620")
        root.minsize(420, 380)
        root.configure(fg_color=BG)
        try:
            root.iconbitmap(str(ICON_ICO))
        except Exception:  # noqa: BLE001
            pass

        self._build_ui()
        self._build_overlay()
        self.bind_hotkey(self.settings["hotkey"])
        self.bind_command_hotkey(self.settings["command_hotkey"])
        self.bind_review_hotkey(self.settings.get("review_hotkey", DEFAULTS["review_hotkey"]))
        if self._corrections:
            self.log_line(f"[correct] {len(self._corrections)} correction(s) loaded")
        try:
            self.engine.start_stream()
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[audio] could not open mic: {e}")
        self.reload_model()
        self.root.after(80, self._drain_queue)
        self.root.after(50, self._overlay_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        start_notifier()

    # ----------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)  # body scrolls
        PADX = 22

        # --- Header (logo + title + Activity button) ------------------------
        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=PADX, pady=(18, 4))
        header.grid_columnconfigure(1, weight=1)
        try:
            self.logo_img = ctk.CTkImage(Image.open(ICON_PNG), size=(50, 50))
            ctk.CTkLabel(header, image=self.logo_img, text="").grid(
                row=0, column=0, sticky="w", padx=(0, 12)
            )
        except Exception:  # noqa: BLE001
            self.logo_img = None
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            titles, text="incant", font=ctk.CTkFont(size=28, weight="bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="local voice → text",
            text_color="#7a7a7a",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w")
        header_btns = ctk.CTkFrame(header, fg_color="transparent")
        header_btns.grid(row=0, column=2, sticky="e")
        ctk.CTkButton(
            header_btns,
            text="Snippets",
            width=84,
            height=30,
            fg_color=SURFACE_ALT,
            hover_color=HOVER,
            command=self.open_snippets_window,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            header_btns,
            text="Activity",
            width=84,
            height=30,
            fg_color=SURFACE_ALT,
            hover_color=HOVER,
            command=self.open_log_window,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            header_btns,
            text="History",
            width=84,
            height=30,
            fg_color=SURFACE_ALT,
            hover_color=HOVER,
            command=self.open_history_window,
        ).pack(side="left")

        # --- Scrollable body (so Advanced scrolls on small windows) ---------
        body = ctk.CTkScrollableFrame(root, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=(PADX - 12), pady=0)
        body.grid_columnconfigure(0, weight=1)
        IPADX = 10  # inner padding inside the scroll area

        # --- Status pill ----------------------------------------------------
        pill = ctk.CTkFrame(body, corner_radius=10, fg_color=SURFACE)
        pill.grid(row=0, column=0, sticky="ew", padx=IPADX, pady=(4, 6))
        self.dot = ctk.CTkLabel(
            pill, text="●", font=ctk.CTkFont(size=18), text_color="#e0a106", width=22
        )
        self.dot.pack(side="left", padx=(14, 4), pady=10)
        self.status_var = ctk.StringVar(value="loading model…")
        ctk.CTkLabel(
            pill, textvariable=self.status_var, font=ctk.CTkFont(size=14, weight="bold")
        ).pack(side="left", pady=10)

        # --- Settings card --------------------------------------------------
        card = ctk.CTkFrame(body, corner_radius=12, fg_color=SURFACE)
        card.grid(row=1, column=0, sticky="ew", padx=IPADX, pady=6)
        card.grid_columnconfigure(1, weight=1)

        def label(parent, text, row):
            ctk.CTkLabel(
                parent,
                text=text,
                font=ctk.CTkFont(size=13),
                text_color="#b0b0b0",
                anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=(16, 10), pady=11)

        # Dictation hotkey
        label(card, "Dictation", 0)
        hk_row = ctk.CTkFrame(card, fg_color="transparent")
        hk_row.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=8)
        hk_row.grid_columnconfigure(0, weight=1)
        self.hotkey_var = ctk.StringVar(value=self.settings["hotkey"])
        self.hotkey_entry = ctk.CTkEntry(hk_row, textvariable=self.hotkey_var)
        self.hotkey_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.set_btn = ctk.CTkButton(
            hk_row, text="Set", width=64, command=self.capture_hotkey
        )
        self.set_btn.grid(row=0, column=1)

        # Command hotkey — enters command mode (routes speech to a pipeline)
        label(card, "Command", 1)
        cmd_row = ctk.CTkFrame(card, fg_color="transparent")
        cmd_row.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=8)
        cmd_row.grid_columnconfigure(0, weight=1)
        self.command_hotkey_var = ctk.StringVar(value=self.settings["command_hotkey"])
        self.command_entry = ctk.CTkEntry(cmd_row, textvariable=self.command_hotkey_var)
        self.command_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.cmd_set_btn = ctk.CTkButton(
            cmd_row, text="Set", width=64, command=self.capture_command_hotkey
        )
        self.cmd_set_btn.grid(row=0, column=1)

        # Model
        label(card, "Model", 2)
        cur_model = next(
            (k for k, v in MODELS.items() if v == self.settings["model"]),
            list(MODELS)[-1],
        )
        self.model_menu = ctk.CTkOptionMenu(
            card, values=list(MODELS), command=lambda _v: self.reload_model()
        )
        self.model_menu.set(cur_model)
        self.model_menu.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=8)

        # Typing mode
        label(card, "Typing", 3)
        cur_out = next(
            (
                k
                for k, v in OUTPUT_MODES.items()
                if v == self.settings.get("output_mode")
            ),
            list(OUTPUT_MODES)[0],
        )
        self.output_seg = ctk.CTkSegmentedButton(
            card, values=list(OUTPUT_MODES), command=lambda _v: self.persist()
        )
        self.output_seg.set(cur_out)
        self.output_seg.grid(row=3, column=1, sticky="ew", padx=(0, 14), pady=8)

        # Language
        label(card, "Language", 4)
        self.lang_menu = ctk.CTkOptionMenu(
            card, values=LANGS, width=120, command=lambda _v: self.persist()
        )
        self.lang_menu.set(self.settings.get("language") or "auto")
        self.lang_menu.grid(row=4, column=1, sticky="w", padx=(0, 14), pady=8)

        # Webhook URL (command mode target)
        label(card, "Webhook", 5)
        webhook_row = ctk.CTkFrame(card, fg_color="transparent")
        webhook_row.grid(row=5, column=1, sticky="ew", padx=(0, 14), pady=8)
        webhook_row.grid_columnconfigure(0, weight=1)
        self.webhook_var = ctk.StringVar(value=self.settings.get("webhook_url", ""))
        ctk.CTkEntry(
            webhook_row,
            textvariable=self.webhook_var,
            placeholder_text="http://localhost:5678/webhook-test/…",
        ).grid(row=0, column=0, sticky="ew")
        self.webhook_var.trace_add("write", lambda *_: self.persist())

        # Review hotkey — pops overlay to correct the last transcript
        label(card, "Review", 6)
        rev_row = ctk.CTkFrame(card, fg_color="transparent")
        rev_row.grid(row=6, column=1, sticky="ew", padx=(0, 14), pady=8)
        rev_row.grid_columnconfigure(0, weight=1)
        self.review_hotkey_var = ctk.StringVar(
            value=self.settings.get("review_hotkey", DEFAULTS["review_hotkey"])
        )
        self.review_entry = ctk.CTkEntry(rev_row, textvariable=self.review_hotkey_var)
        self.review_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.rev_set_btn = ctk.CTkButton(
            rev_row, text="Set", width=64, command=self.capture_review_hotkey
        )
        self.rev_set_btn.grid(row=0, column=1)

        # History recording mode
        label(card, "History", 7)
        _HISTORY_LABELS = ["Full", "Corrections", "Off"]
        _HISTORY_VALUES = {"Full": "full", "Corrections": "corrections", "Off": "off"}
        _HISTORY_KEYS = {v: k for k, v in _HISTORY_VALUES.items()}
        cur_hist = _HISTORY_KEYS.get(
            self.settings.get("history_mode", DEFAULTS["history_mode"]), "Full"
        )
        self.history_seg = ctk.CTkSegmentedButton(
            card,
            values=_HISTORY_LABELS,
            command=lambda _v: self.persist(),
        )
        self.history_seg.set(cur_hist)
        self.history_seg.grid(row=7, column=1, sticky="w", padx=(0, 14), pady=8)
        self._history_values = _HISTORY_VALUES

        # --- Advanced (collapsible) ----------------------------------------
        self.adv_btn = ctk.CTkButton(
            body,
            text="▸  Advanced",
            anchor="w",
            height=32,
            fg_color="transparent",
            hover_color=SURFACE_ALT,
            text_color="#b0b0b0",
            command=self._toggle_advanced,
        )
        self.adv_btn.grid(row=2, column=0, sticky="ew", padx=IPADX, pady=(8, 0))

        self.adv = ctk.CTkFrame(body, corner_radius=12, fg_color=SURFACE)
        self.adv.grid(row=3, column=0, sticky="ew", padx=IPADX, pady=(4, 6))
        self.adv.grid_columnconfigure(1, weight=1)
        self.adv.grid_remove()  # hidden until expanded

        self.silence_var = ctk.DoubleVar(value=float(self.settings["silence_s"]))
        self.beam_var = ctk.IntVar(value=int(self.settings["beam_size"]))
        self.preroll_var = ctk.DoubleVar(value=float(self.settings["preroll_s"]))
        self.rms_var = ctk.DoubleVar(value=float(self.settings["mic_rms"]))

        self.sil_value = self._adv_slider(
            self.adv,
            0,
            "Pause split",
            self.silence_var,
            0.4,
            2.0,
            16,
            lambda v: f"{v:.1f}s",
            "continuous: silence gap that ends a sentence",
        )
        self.beam_value = self._adv_slider(
            self.adv,
            2,
            "Beam size",
            self.beam_var,
            1,
            5,
            4,
            lambda v: f"{int(v)}",
            "higher = more accurate but slower",
        )
        self.preroll_value = self._adv_slider(
            self.adv,
            4,
            "Pre-roll",
            self.preroll_var,
            0.2,
            1.5,
            13,
            lambda v: f"{v:.1f}s",
            "audio kept before keypress (first-word onset)",
        )
        self.rms_value = self._adv_slider(
            self.adv,
            6,
            "Mic gate",
            self.rms_var,
            0.004,
            0.030,
            26,
            lambda v: f"{v:.3f}",
            "continuous: louder gate = ignores quiet noise",
        )

        # n8n JWT secret — passphrase for command-mode webhook auth (ADR-0004)
        ctk.CTkLabel(
            self.adv,
            text="n8n secret",
            font=ctk.CTkFont(size=13),
            text_color="#b0b0b0",
            anchor="w",
        ).grid(row=8, column=0, sticky="w", padx=(16, 10), pady=(12, 0))
        n8n_row = ctk.CTkFrame(self.adv, fg_color="transparent")
        n8n_row.grid(row=8, column=1, sticky="ew", padx=(0, 14), pady=(12, 0))
        n8n_row.grid_columnconfigure(0, weight=1)
        try:
            n8n_secret = credentials.resolve("n8n").get("secret", "")
        except credentials.CredentialError:
            n8n_secret = ""
        self.n8n_secret_var = ctk.StringVar(value=n8n_secret)
        self.n8n_secret_entry = ctk.CTkEntry(
            n8n_row, textvariable=self.n8n_secret_var, show="*"
        )
        self.n8n_secret_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            n8n_row, text="Save", width=64, command=self.save_n8n_secret
        ).grid(row=0, column=1)
        ctk.CTkLabel(
            self.adv,
            text="command mode: JWT passphrase for the n8n webhook (ADR-0004)",
            text_color="#666",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=9, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 2))

        # --- Footer ---------------------------------------------------------
        ctk.CTkLabel(
            body,
            text="Keep this window open (minimize it). Hotkey works globally.",
            text_color="#5f5f5f",
            font=ctk.CTkFont(size=11),
        ).grid(row=4, column=0, pady=(8, 12))

    def _adv_slider(self, parent, row, name, var, lo, hi, steps, fmt, hint):
        ctk.CTkLabel(
            parent,
            text=name,
            font=ctk.CTkFont(size=13),
            text_color="#b0b0b0",
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(16, 10), pady=(12, 0))
        sl_row = ctk.CTkFrame(parent, fg_color="transparent")
        sl_row.grid(row=row, column=1, sticky="ew", padx=(0, 14), pady=(12, 0))
        sl_row.grid_columnconfigure(0, weight=1)
        val_lbl = ctk.CTkLabel(
            sl_row, text=fmt(var.get()), width=46, font=ctk.CTkFont(size=13)
        )
        val_lbl.grid(row=0, column=1)
        sl = ctk.CTkSlider(
            sl_row,
            from_=lo,
            to=hi,
            number_of_steps=steps,
            variable=var,
            command=lambda _v, l=val_lbl, f=fmt, vv=var: self._on_adv_change(l, f, vv),
        )
        sl.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ctk.CTkLabel(
            parent, text=hint, text_color="#666", font=ctk.CTkFont(size=11), anchor="w"
        ).grid(row=row + 1, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 2))
        return val_lbl

    def _toggle_advanced(self) -> None:
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv.grid()
            self.adv_btn.configure(text="▾  Advanced")
        else:
            self.adv.grid_remove()
            self.adv_btn.configure(text="▸  Advanced")

    def _on_adv_change(self, val_lbl, fmt, var) -> None:
        val_lbl.configure(text=fmt(var.get()))
        self.persist()
        self.engine.set_preroll(float(self.preroll_var.get()))

    # --------------------------------------------------------- snippets window
    def open_snippets_window(self) -> None:
        if self._snip_win is not None and self._snip_win.winfo_exists():
            self._snip_win.focus()
            return
        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title("incant — snippets")
        win.geometry("620x400")
        win.minsize(480, 260)
        try:
            win.after(250, lambda: win.iconbitmap(str(ICON_ICO)))
        except Exception:  # noqa: BLE001
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            win,
            text="Say the phrase exactly to paste the expansion instead.",
            text_color="#7a7a7a",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 4))
        scroll = ctk.CTkScrollableFrame(win, fg_color=SURFACE)
        scroll.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        scroll.grid_columnconfigure(0, weight=1)
        self._snip_win = win
        self._snip_scroll = scroll
        self._snip_rows: list[tuple] = []
        for phrase, expansion in self.snippets.items():
            self._add_snippet_row(phrase, expansion)
        ctk.CTkButton(
            win,
            text="+ Add snippet",
            fg_color=SURFACE_ALT,
            hover_color=HOVER,
            command=lambda: self._add_snippet_row("", ""),
        ).grid(row=2, column=0, pady=(4, 12))

        def _on_close():
            self._snip_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _add_snippet_row(self, phrase: str, expansion: str) -> None:
        scroll = self._snip_scroll
        row_idx = len(self._snip_rows)
        row_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        row_frame.grid(row=row_idx, column=0, sticky="ew", padx=4, pady=3)
        row_frame.grid_columnconfigure(2, weight=1)
        phrase_var = ctk.StringVar(value=phrase)
        expansion_var = ctk.StringVar(value=expansion)
        ctk.CTkEntry(
            row_frame,
            textvariable=phrase_var,
            placeholder_text="trigger phrase",
            width=160,
        ).grid(row=0, column=0, padx=(0, 6))
        ctk.CTkLabel(row_frame, text="→", width=20).grid(row=0, column=1, padx=2)
        ctk.CTkEntry(
            row_frame,
            textvariable=expansion_var,
            placeholder_text="expansion text",
        ).grid(row=0, column=2, sticky="ew", padx=(4, 6))

        def delete_row():
            self._snip_rows = [(p, e, f) for p, e, f in self._snip_rows if f is not row_frame]
            row_frame.destroy()
            self._save_snippets()

        ctk.CTkButton(
            row_frame, text="✕", width=32, fg_color="#a33", hover_color="#822",
            command=delete_row,
        ).grid(row=0, column=3)
        phrase_var.trace_add("write", lambda *_: self._save_snippets())
        expansion_var.trace_add("write", lambda *_: self._save_snippets())
        self._snip_rows.append((phrase_var, expansion_var, row_frame))

    def _save_snippets(self) -> None:
        self.snippets = {}
        for phrase_var, expansion_var, _ in self._snip_rows:
            p = phrase_var.get().strip()
            e = expansion_var.get()
            if p:
                self.snippets[p] = e
        self.settings["snippets"] = self.snippets
        save_settings(self.settings)

    # ------------------------------------------------------------ log window
    def open_log_window(self) -> None:
        if self._log_win is not None and self._log_win.winfo_exists():
            self._log_win.focus()
            return
        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title("incant — activity")
        win.geometry("560x420")
        try:
            win.after(250, lambda: win.iconbitmap(str(ICON_ICO)))
        except Exception:  # noqa: BLE001
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(0, weight=1)
        box = ctk.CTkTextbox(
            win, font=ctk.CTkFont(family="Consolas", size=12), wrap="word"
        )
        box.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        box.insert(
            "end", "\n".join(self._log_lines) + ("\n" if self._log_lines else "")
        )
        box.see("end")
        box.configure(state="disabled")
        self._log_win, self._log_box = win, box

        def _closed():
            self._log_win = None
            self._log_box = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _closed)

    # ---------------------------------------------------------- history window
    def open_history_window(self) -> None:
        if self._hist_win is not None and self._hist_win.winfo_exists():
            self._hist_win.focus()
            return

        all_entries = history.load_all()

        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title("incant — history")
        win.geometry("660x520")
        win.minsize(480, 320)
        try:
            win.after(250, lambda: win.iconbitmap(str(ICON_ICO)))
        except Exception:  # noqa: BLE001
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(0, weight=1)
        self._hist_win = win

        container = ctk.CTkFrame(win, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)

        active_screen = [None]

        def _clear():
            if active_screen[0] is not None:
                active_screen[0].destroy()
                active_screen[0] = None

        def _show_search(restore_query: str = "") -> None:
            _clear()
            frame = ctk.CTkFrame(container, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(1, weight=1)
            active_screen[0] = frame

            search_bar = ctk.CTkFrame(frame, fg_color="transparent")
            search_bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
            search_bar.grid_columnconfigure(0, weight=1)

            q_var = ctk.StringVar(value=restore_query)
            q_entry = ctk.CTkEntry(
                search_bar, textvariable=q_var,
                placeholder_text="Search history…",
                font=ctk.CTkFont(size=14),
            )
            q_entry.grid(row=0, column=0, sticky="ew")
            q_entry.focus_set()

            results = ctk.CTkScrollableFrame(frame, fg_color="transparent")
            results.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
            results.grid_columnconfigure(0, weight=1)

            def _render(q: str = "") -> None:
                for w in results.winfo_children():
                    w.destroy()
                matches = history.search(all_entries, q)
                if not matches:
                    msg = "No history yet." if not all_entries else "No results."
                    ctk.CTkLabel(
                        results, text=msg, text_color="#555",
                        font=ctk.CTkFont(size=13),
                    ).pack(pady=24)
                    return
                for entry in reversed(matches):
                    _result_row(results, entry, q)

            def _result_row(parent: ctk.CTkScrollableFrame, entry: dict, q: str) -> None:
                ts = entry.get("ts", 0)
                dt = datetime.datetime.fromtimestamp(ts)
                time_str = dt.strftime("%b %d  ·  %I:%M %p")
                session = entry.get("session", "?")
                text = entry.get("output", entry.get("raw", ""))
                preview = text[:100] + ("…" if len(text) > 100 else "")

                row = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
                row.pack(fill="x", padx=12, pady=3)
                row.grid_columnconfigure(0, weight=1)

                meta = ctk.CTkLabel(
                    row, text=f"{time_str}  ·  {session}",
                    text_color="#555", font=ctk.CTkFont(size=11), anchor="w",
                )
                meta.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))

                body_lbl = ctk.CTkLabel(
                    row, text=preview,
                    text_color="#c8c8c8", font=ctk.CTkFont(size=13),
                    anchor="w", wraplength=580, justify="left",
                )
                body_lbl.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))

                def _click(e: dict = entry) -> None:
                    _show_context(e, restore_query=q_var.get())

                for widget in (row, meta, body_lbl):
                    widget.bind("<Button-1>", lambda _: _click())
                    widget.bind("<Enter>", lambda _, r=row: r.configure(fg_color=SURFACE_ALT))
                    widget.bind("<Leave>", lambda _, r=row: r.configure(fg_color=SURFACE))

            q_var.trace_add("write", lambda *_: _render(q_var.get()))
            _render(restore_query)

        def _show_context(clicked: dict, restore_query: str = "") -> None:
            _clear()
            frame = ctk.CTkFrame(container, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(1, weight=1)
            active_screen[0] = frame

            session = clicked.get("session", "")
            ctx = history.session_entries(all_entries, session)

            hdr = ctk.CTkFrame(frame, fg_color="transparent")
            hdr.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
            hdr.grid_columnconfigure(1, weight=1)

            ctk.CTkButton(
                hdr, text="← Back", width=80, height=28,
                fg_color=SURFACE_ALT, hover_color=HOVER,
                command=lambda: _show_search(restore_query),
            ).grid(row=0, column=0, sticky="w")

            ts0 = clicked.get("ts", 0)
            date_str = datetime.datetime.fromtimestamp(ts0).strftime("%b %d %Y")
            ctk.CTkLabel(
                hdr, text=f"Session {session}  ·  {date_str}",
                text_color="#7a7a7a", font=ctk.CTkFont(size=12),
            ).grid(row=0, column=1, sticky="w", padx=(12, 0))

            scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent")
            scroll.grid(row=1, column=0, sticky="nsew")
            scroll.grid_columnconfigure(0, weight=1)

            clicked_ts = clicked.get("ts", 0)
            highlight_row = [None]

            for entry in ctx:
                ts = entry.get("ts", 0)
                dt = datetime.datetime.fromtimestamp(ts)
                time_str = dt.strftime("%I:%M:%S %p")
                text = entry.get("output", entry.get("raw", ""))
                active = abs(ts - clicked_ts) < 0.001

                bg = "#1a3324" if active else SURFACE
                row = ctk.CTkFrame(scroll, fg_color=bg, corner_radius=6)
                row.pack(fill="x", padx=12, pady=2)

                ctk.CTkLabel(
                    row,
                    text=("▶  " if active else "    ") + time_str + "   " + text,
                    text_color="#e8e8e8" if active else "#a0a0a0",
                    font=ctk.CTkFont(size=13, weight="bold" if active else "normal"),
                    anchor="w", wraplength=590, justify="left",
                ).pack(fill="x", padx=10, pady=6)

                if active:
                    highlight_row[0] = row

            def _scroll_to_highlight() -> None:
                r = highlight_row[0]
                if r is None:
                    return
                try:
                    r.update_idletasks()
                    y = r.winfo_y()
                    total = scroll._parent_frame.winfo_reqheight()
                    view_h = scroll._parent_canvas.winfo_height()
                    if total > 0:
                        frac = max(0.0, min(1.0, (y - view_h // 2) / total))
                        scroll._parent_canvas.yview_moveto(frac)
                except Exception:  # noqa: BLE001
                    pass

            win.after(120, _scroll_to_highlight)

        def _on_close() -> None:
            self._hist_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        _show_search()

    # -------------------------------------------------------------- helpers
    def log_line(self, msg: str) -> None:
        self.ui_queue.put(("log", msg))

    def set_status(self, msg: str) -> None:
        self.ui_queue.put(("status", msg))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, val = self.ui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(val)
                    self.dot.configure(text_color=_status_color(val))
                elif kind == "enable_set":
                    self.set_btn.configure(state="normal", text="Set")
                elif kind == "enable_cmd_set":
                    self.cmd_set_btn.configure(state="normal", text="Set")
                elif kind == "enable_rev_set":
                    self.rev_set_btn.configure(state="normal", text="Set")
                elif kind == "review":
                    self._show_review_overlay()
                elif kind == "log":
                    self._log_lines.append(val)
                    del self._log_lines[:-500]  # cap history
                    if (
                        self._log_box is not None
                        and self._log_win
                        and self._log_win.winfo_exists()
                    ):
                        self._log_box.configure(state="normal")
                        self._log_box.insert("end", val + "\n")
                        self._log_box.see("end")
                        self._log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self._drain_approvals()
        self.root.after(80, self._drain_queue)

    def _drain_approvals(self) -> None:
        """Check for pending approval requests from n8n and show a dialog."""
        req = pop_approval()
        while req is not None:
            self._show_approval_dialog(req)
            req = pop_approval()

    def _show_approval_dialog(self, req: ApprovalRequest) -> None:
        """Modal popup: show the action details and let the user Approve / Reject."""
        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title(req.title)
        win.geometry("480x360")
        win.minsize(420, 300)
        win.transient(self.root)
        win.grab_set()
        win.focus()

        ctk.CTkLabel(
            win, text=req.title, font=ctk.CTkFont(size=16, weight="bold")
        ).pack(fill="x", padx=20, pady=(16, 4))

        textbox = ctk.CTkTextbox(win, wrap="word", font=ctk.CTkFont(size=13))
        textbox.pack(fill="both", expand=True, padx=20, pady=8)
        textbox.insert("1.0", req.message)
        textbox.configure(state="disabled")

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(0, 16))

        ctk.CTkButton(
            btn_row,
            text="Reject",
            fg_color="#a33",
            hover_color="#822",
            command=lambda: self._resolve_approval(win, req.id, False),
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row,
            text="Approve",
            fg_color="#2a7",
            hover_color="#195",
            command=lambda: self._resolve_approval(win, req.id, True),
        ).pack(side="right")

    def _resolve_approval(
        self, win: ctk.CTkToplevel, approval_id: str, approved: bool
    ) -> None:
        win.destroy()
        label = "approved" if approved else "rejected"
        self.log_line(f"[approval] {approval_id[:8]}… {label}")
        respond_approval(approval_id, approved)

    # --------------------------------------------------------------- overlay
    def _build_overlay(self) -> None:
        """Borderless always-on-top pill showing a live waveform while
        transcription is on. Click-through so it never steals focus."""
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=OVERLAY_KEY)
        win.attributes("-transparentcolor", OVERLAY_KEY)

        canvas = tk.Canvas(
            win,
            width=OVERLAY_W,
            height=OVERLAY_H,
            bg=OVERLAY_KEY,
            highlightthickness=0,
            bd=0,
        )
        canvas.pack()

        win.update_idletasks()
        try:
            left, top, right, bottom = _monitor_work_area(self.root.winfo_id())
        except Exception:  # noqa: BLE001
            left, top = 0, 0
            right, bottom = win.winfo_screenwidth(), win.winfo_screenheight()
        x = left + (right - left - OVERLAY_W) // 2
        y = bottom - OVERLAY_H - OVERLAY_MARGIN_BOTTOM
        win.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{x}+{y}")

        _rounded_rect(
            canvas, 1, 1, OVERLAY_W - 1, OVERLAY_H - 1, OVERLAY_H // 2,
            fill=OVERLAY_BG, outline="",
        )

        cy = OVERLAY_H / 2
        usable = OVERLAY_W - 2 * OVERLAY_PAD
        bars = []
        for i in range(OVERLAY_BARS):
            bx = OVERLAY_PAD + i * usable / (OVERLAY_BARS - 1)
            bar = canvas.create_line(
                bx, cy - 2, bx, cy + 2, fill="white", width=OVERLAY_BAR_WIDTH, capstyle=tk.ROUND
            )
            bars.append((bar, bx))

        self._overlay_win = win
        self._overlay_canvas = canvas
        self._overlay_bars = bars
        # Per-bar amplitude weights so all bars pulse with the same audio
        # level but at different relative heights (static EQ look, no scroll).
        # Tapered by a dome shape so bars near the ends sit lower than the
        # middle ones.
        self._overlay_weights = [
            random.uniform(0.5, 1.4)
            * (0.5 + 0.5 * math.sin(math.pi * i / (OVERLAY_BARS - 1)))
            for i in range(OVERLAY_BARS)
        ]
        self._overlay_visible = False
        self._make_clickthrough(win)

    def _make_clickthrough(self, win: tk.Toplevel) -> None:
        """Make the overlay ignore mouse/keyboard so it never steals focus."""
        try:
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            styles = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                styles | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            )
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[overlay] click-through unavailable: {e}")

    def _overlay_should_show(self) -> bool:
        return (
            self.continuous_on
            or self.word_streamer is not None
            or self.engine.capturing
        )

    def _overlay_tick(self) -> None:
        show = self._overlay_should_show()
        if show and not self._overlay_visible:
            self._overlay_win.deiconify()
            self._overlay_visible = True
        elif not show and self._overlay_visible:
            self._overlay_win.withdraw()
            self._overlay_visible = False

        if show:
            recent = self.engine.recent_levels(3)
            level = sum(recent) / len(recent) if recent else 0.0
            cy = OVERLAY_H / 2
            min_h, max_h = 4.0, OVERLAY_H - 2 * OVERLAY_BAR_WIDTH
            for (bar, bx), weight in zip(self._overlay_bars, self._overlay_weights):
                frac = min(1.0, math.sqrt(max(0.0, level)) * OVERLAY_GAIN * weight)
                h = min_h + (max_h - min_h) * frac
                self._overlay_canvas.coords(bar, bx, cy - h / 2, bx, cy + h / 2)

        self.root.after(50, self._overlay_tick)

    def persist(self) -> None:
        lang = self.lang_menu.get()
        self.settings.update(
            hotkey=self.hotkey_var.get().strip(),
            command_hotkey=self.command_hotkey_var.get().strip(),
            review_hotkey=self.review_hotkey_var.get().strip(),
            history_mode=self._history_values.get(self.history_seg.get(), "full"),
            model=MODELS[self.model_menu.get()],
            language="" if lang == "auto" else lang,
            output_mode=OUTPUT_MODES[self.output_seg.get()],
            silence_s=round(float(self.silence_var.get()), 1),
            beam_size=int(self.beam_var.get()),
            preroll_s=round(float(self.preroll_var.get()), 1),
            mic_rms=round(float(self.rms_var.get()), 3),
            webhook_url=self.webhook_var.get().strip(),
        )
        save_settings(self.settings)

    def save_n8n_secret(self) -> None:
        secret = self.n8n_secret_var.get().strip()
        credentials.save("n8n", {"secret": secret})
        self.log_line("[settings] n8n secret saved")

    # ---------------------------------------------------------------- hotkey
    def bind_hotkey(self, hk: str) -> None:
        if self.hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self.hotkey_handle)
            except (KeyError, ValueError):
                pass
        try:
            self.hotkey_handle = keyboard.add_hotkey(hk, self.toggle, suppress=False)
            self.log_line(f"[hotkey] bound to {hk}")
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[hotkey] invalid '{hk}': {e}")

    def capture_hotkey(self) -> None:
        self.set_btn.configure(state="disabled", text="press…")
        self.set_status("press your hotkey combo…")

        def grab() -> None:
            hk = keyboard.read_hotkey(suppress=False)
            self.hotkey_var.set(hk)
            self.bind_hotkey(hk)
            self.persist()
            self.set_status("ready — press your hotkey to talk")
            self.ui_queue.put(("enable_set", None))

        threading.Thread(target=grab, daemon=True).start()

    # -------------------------------------------------------- command hotkey
    def bind_command_hotkey(self, hk: str) -> None:
        if self.command_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self.command_hotkey_handle)
            except (KeyError, ValueError):
                pass
        try:
            self.command_hotkey_handle = keyboard.add_hotkey(
                hk, self.toggle_command, suppress=False
            )
            self.log_line(f"[hotkey] command bound to {hk}")
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[hotkey] invalid command '{hk}': {e}")

    def capture_command_hotkey(self) -> None:
        self.cmd_set_btn.configure(state="disabled", text="press…")
        self.set_status("press your command hotkey combo…")

        def grab() -> None:
            hk = keyboard.read_hotkey(suppress=False)
            self.command_hotkey_var.set(hk)
            self.bind_command_hotkey(hk)
            self.persist()
            self.set_status("ready — press your hotkey to talk")
            self.ui_queue.put(("enable_cmd_set", None))

        threading.Thread(target=grab, daemon=True).start()

    # ---------------------------------------------------------- review hotkey
    def bind_review_hotkey(self, hk: str) -> None:
        if self._review_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._review_hotkey_handle)
            except (KeyError, ValueError):
                pass
        try:
            self._review_hotkey_handle = keyboard.add_hotkey(
                hk, lambda: self.ui_queue.put(("review", None)), suppress=False
            )
            self.log_line(f"[hotkey] review bound to {hk}")
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[hotkey] invalid review '{hk}': {e}")

    def capture_review_hotkey(self) -> None:
        self.rev_set_btn.configure(state="disabled", text="press…")
        self.set_status("press your review hotkey combo…")

        def grab() -> None:
            hk = keyboard.read_hotkey(suppress=False)
            self.review_hotkey_var.set(hk)
            self.bind_review_hotkey(hk)
            self.persist()
            self.set_status("ready — press your hotkey to talk")
            self.ui_queue.put(("enable_rev_set", None))

        threading.Thread(target=grab, daemon=True).start()

    def _show_review_overlay(self) -> None:
        if not self._last_typed:
            self.log_line("[correct] nothing to review yet")
            return
        if self._review_win is not None and self._review_win.winfo_exists():
            self._review_win.focus()
            return

        original = self._last_typed          # includes trailing space
        display = original.rstrip()           # shown/edited without it

        try:
            prev_hwnd = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            prev_hwnd = None

        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title("incant — correct")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self._review_win = win

        try:
            left, top, right, bottom = _monitor_work_area(self.root.winfo_id())
        except Exception:
            left, top, right, bottom = 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = 520, 110
        x = left + (right - left - w) // 2
        y = bottom - h - 90
        win.geometry(f"{w}x{h}+{x}+{y}")

        ctk.CTkLabel(
            win, text="Correct last transcript  (Enter = commit · Esc = cancel · 8s auto-dismiss)",
            text_color="#7a7a7a", font=ctk.CTkFont(size=11), anchor="w",
        ).pack(anchor="w", padx=16, pady=(10, 2))

        var = ctk.StringVar(value=display)
        entry = ctk.CTkEntry(win, textvariable=var, font=ctk.CTkFont(size=14))
        entry.pack(fill="x", padx=16, pady=(0, 12))
        entry.select_range(0, "end")
        entry.focus_set()

        dismissed = [False]

        def _commit(event=None):
            if dismissed[0]:
                return
            dismissed[0] = True
            corrected = var.get().strip()
            win.destroy()
            self._review_win = None
            if corrected == display or not corrected:
                return

            n_back = len(original)

            def _retype():
                try:
                    if prev_hwnd:
                        ctypes.windll.user32.SetForegroundWindow(prev_hwnd)
                except Exception:
                    pass
                for _ in range(n_back):
                    keyboard.press_and_release("backspace")
                keyboard.write(corrected + " ", delay=0)
                new_map = corrections.record(display, corrected)
                self._corrections = new_map
                self._last_typed = corrected + " "
                self.log_line(f"[correct] '{display}' → '{corrected}'")

            self.root.after(150, _retype)

        def _cancel(event=None):
            if dismissed[0]:
                return
            dismissed[0] = True
            win.destroy()
            self._review_win = None

        def _auto_cancel():
            if not dismissed[0] and win.winfo_exists():
                _cancel()

        entry.bind("<Return>", _commit)
        entry.bind("<Escape>", _cancel)
        win.protocol("WM_DELETE_WINDOW", _cancel)
        win.after(8000, _auto_cancel)

    def _maybe_log(self, raw: str, output: str) -> None:
        mode = self.settings.get("history_mode", "full")
        if mode == "off" or not raw:
            return
        if mode == "corrections" and self._corrections:
            # Only log phrases that contain a previously-corrected word
            if not any(
                re.search(r"\b" + re.escape(k) + r"\b", raw, re.IGNORECASE)
                for k in self._corrections
            ):
                return
        elif mode == "corrections":
            return  # no corrections yet; nothing to track
        history.log_phrase(self._session_id, raw, output)

    # ----------------------------------------------------------------- model
    def reload_model(self) -> None:
        size = MODELS[self.model_menu.get()]
        self.persist()
        self.set_status(f"loading {size}…")
        self.log_line(f"[load] loading {size} (first use downloads weights)…")

        def work() -> None:
            with self.model_lock:
                try:
                    from faster_whisper.utils import download_model

                    try:
                        download_model(size, local_files_only=True)
                    except Exception:  # noqa: BLE001
                        self.set_status(f"downloading {size} model (first run, several GB)…")
                        self.log_line(f"[load] {size} not cached — downloading from Hugging Face…")

                    m = stt.load_model(size)
                    list(m.transcribe(np.zeros(stt.SAMPLE_RATE, dtype=np.float32))[0])
                    self.model = m
                    self.set_status("ready — press your hotkey to talk")
                    self.log_line(f"[load] {size} ready.")
                except Exception as e:  # noqa: BLE001
                    self.set_status("model failed to load")
                    self.log_line(f"[load] ERROR: {e}")

        threading.Thread(target=work, daemon=True).start()

    # --------------------------------------------------------------- toggle
    def toggle(self) -> None:
        if self.model is None:
            self.log_line("[stt] model not ready yet")
            return
        mode = self.settings.get("output_mode")
        if mode == "continuous":
            self._toggle_continuous()
        elif mode == "word":
            self._toggle_word()
        else:
            self._toggle_capture()

    def _beam(self) -> int:
        return int(self.settings.get("beam_size", 5))

    # capture ---------------------------------------------------------------
    def _toggle_capture(self) -> None:
        if self.busy.is_set():
            self.log_line("[stt] still transcribing…")
            return
        if not self.engine.capturing:
            self.engine.begin_capture()
            self.set_status("● recording… (hotkey again to stop)")
        else:
            audio = self.engine.end_capture()
            self.busy.set()
            self.set_status("transcribing…")
            threading.Thread(
                target=self._do_transcribe, args=(audio,), daemon=True
            ).start()

    def _do_transcribe(self, audio: np.ndarray) -> None:
        try:
            lang = self.settings.get("language") or None
            with self.model_lock:
                raw, detected = stt.transcribe_audio(
                    self.model, audio, lang, beam_size=self._beam(),
                    hotwords=corrections.hotwords(self._corrections),
                )
            text = corrections.apply(raw, self._corrections)
            self._maybe_log(raw, text)
            if text:
                expansion = stt.apply_snippet(text, self.snippets)
                if expansion is not None:
                    typed = expansion + " "
                    self.log_line(f"[{detected}] {text} → [snippet]")
                else:
                    typed = text + " "
                    self.log_line(f"[{detected}] {text}")
                self._last_typed = typed
                keyboard.write(typed, delay=0)
            else:
                self.log_line("[stt] (nothing heard)")
            self.set_status("ready — press your hotkey to talk")
        finally:
            self.busy.clear()

    # continuous ------------------------------------------------------------
    def _toggle_continuous(self) -> None:
        if not self.continuous_on:
            gap = float(self.settings.get("silence_s", 1.0))
            rms = float(self.settings.get("mic_rms", 0.012))
            self.segmenter = stt.PhraseSegmenter(
                on_phrase=self.phrase_queue.put, min_silence_s=gap, silence_rms=rms
            )
            self.stitcher = stt.TextStitcher()
            pre = self.engine.preroll_audio()
            if pre.size:
                self.segmenter.feed(pre)
            threading.Thread(target=self._phrase_loop, daemon=True).start()
            self.engine.set_frame_listener(self.segmenter.feed)
            self.continuous_on = True
            self.set_status("● listening… (types as you pause; hotkey to stop)")
            self.log_line(f"[stt] continuous on (pause {gap:.1f}s)")
        else:
            self.engine.set_frame_listener(None)
            if self.segmenter is not None:
                self.segmenter.finish()
            self.segmenter = None
            self.phrase_queue.put(None)
            self.continuous_on = False
            self.set_status("ready — press your hotkey to talk")
            self.log_line("[stt] continuous off")

    def _phrase_loop(self) -> None:
        while True:
            audio = self.phrase_queue.get()
            if audio is None:
                return
            try:
                lang = self.settings.get("language") or None
                prompt = self.stitcher.prompt if self.stitcher else None
                with self.model_lock:
                    raw, _ = stt.transcribe_audio(
                        self.model,
                        audio,
                        lang,
                        initial_prompt=prompt,
                        beam_size=self._beam(),
                        hotwords=corrections.hotwords(self._corrections),
                    )
                text = corrections.apply(raw, self._corrections)
                self._maybe_log(raw, text)
                expansion = stt.apply_snippet(text, self.snippets)
                if expansion is not None:
                    prefix = " " if self.stitcher and self.stitcher.prompt else ""
                    if self.stitcher:
                        self.stitcher.next(text)  # update context with original words
                    out = prefix + expansion
                else:
                    out = self.stitcher.next(text) if self.stitcher else text
                if out:
                    self.log_line(f"› {text}" + (" → [snippet]" if expansion is not None else ""))
                    self._last_typed = out
                    keyboard.write(out, delay=0)
            except Exception as e:  # noqa: BLE001
                self.log_line(f"[stt] phrase error: {e}")

    # word by word ----------------------------------------------------------
    def _toggle_word(self) -> None:
        if self.word_streamer is None:
            self._last_typed = ""

            def tx(audio, prompt):
                lang = self.settings.get("language") or None
                with self.model_lock:
                    return stt.transcribe_words(
                        self.model,
                        audio,
                        lang,
                        initial_prompt=prompt,
                        beam_size=self._beam(),
                        hotwords=corrections.hotwords(self._corrections),
                    )

            def out(text):
                corrected = corrections.apply(text, self._corrections)
                self._maybe_log(text, corrected)
                keyboard.write(corrected, delay=0)
                self._last_typed += corrected
                self.log_line(f"› {corrected.strip()}")

            self.word_streamer = stt.WordStreamer(tx, out)
            pre = self.engine.preroll_audio()
            if pre.size:
                self.word_streamer.feed(pre)
            self.engine.set_frame_listener(self.word_streamer.feed)
            self.word_streamer.start()
            self.set_status("● listening… (types word-by-word; hotkey to stop)")
            self.log_line("[stt] word-by-word on")
        else:
            self.engine.set_frame_listener(None)
            self.word_streamer.stop()
            self.word_streamer = None
            self.set_status("ready — press your hotkey to talk")
            self.log_line("[stt] word-by-word off")

    # --------------------------------------------------------- command mode
    def toggle_command(self) -> None:
        """Push-to-talk for command mode: first press records, second runs."""
        if self.model is None:
            self.log_line("[cmd] model not ready yet")
            return
        if not self.command_on:
            if (
                self.engine.capturing
                or self.busy.is_set()
                or self.continuous_on
                or self.word_streamer is not None
            ):
                self.log_line("[cmd] busy — finish dictation first")
                return
            self.engine.begin_capture()
            self.command_on = True
            self.set_status("● command… (hotkey again to run)")
        else:
            audio = self.engine.end_capture()
            self.command_on = False
            self.set_status("routing…")
            threading.Thread(
                target=self._run_command, args=(audio,), daemon=True
            ).start()

    def _run_command(self, audio: np.ndarray) -> None:
        try:
            lang = self.settings.get("language") or None
            with self.model_lock:
                text, _ = stt.transcribe_audio(
                    self.model, audio, lang, beam_size=self._beam(),
                    hotwords=corrections.hotwords(self._corrections),
                )
            if not text:
                self.log_line("[cmd] (nothing heard)")
                return
            self.log_line(f"[cmd] heard: {text}")
            webhook_url = self.settings.get("webhook_url", "").strip()
            if not webhook_url:
                self.log_line("[cmd] no webhook configured — cleaning + typing")
                self._clean_and_type(text)
                return
            outcome = run_command(text, webhook_url=webhook_url)
            if outcome.error:
                self.log_line(f"[cmd] error: {outcome.error}")
            else:
                self.log_line(f"[cmd] sent OK  reply: {outcome.reply or '(none)'}")
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[cmd] error: {e}")
        finally:
            self.set_status("ready — press your hotkey to talk")

    def _clean_and_type(self, text: str) -> None:
        """No-webhook fallback: AI-clean the transcript and type it — never raw."""
        try:
            model = make_model(
                self.settings.get(
                    "router_model", {"backend": "ollama", "model": "llama3.2"}
                )
            )
            cleaned = model.complete(
                "Fix the grammar, capitalization and punctuation of this dictation. "
                "Return ONLY the corrected text, nothing else.",
                text,
            ).strip()
            keyboard.write((cleaned or text) + " ", delay=0)
        except ModelError as e:
            self.log_line(f"[cmd] AI clean unavailable ({e}); typing raw")
            keyboard.write(text + " ", delay=0)

    # ------------------------------------------------------------- lifecycle
    def on_close(self) -> None:
        try:
            self.engine.set_frame_listener(None)
            if self.continuous_on:
                self.phrase_queue.put(None)
            if self.word_streamer is not None:
                self.word_streamer.stop()
            self.engine.stop_stream()
            keyboard.unhook_all_hotkeys()
            stop_notifier()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()


def main() -> None:
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
