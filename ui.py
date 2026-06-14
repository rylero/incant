"""
incant — local-first voice transcription with automation.

Control panel: set the hotkey, pick a model and typing mode, and (under
Advanced) tune performance. Press the hotkey anywhere to dictate into the
focused app. The Activity log opens in its own window.

Run:  uv run ui
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import numpy as np
import keyboard
import customtkinter as ctk
from PIL import Image

import stt  # sets up CUDA DLLs on import

# Automation layer (command mode): route a transcript to a registered n8n
# workflow and fire its webhook. See automation/ and docs/adr/.
from automation.command import run_command
from automation.models import make_model, ModelError
from automation.notifier import (
    ApprovalRequest,
    start as start_notifier,
    stop as stop_notifier,
    pop_approval,
    respond_approval,
)

SETTINGS_PATH = Path(__file__).with_name("settings.json")
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

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


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
        self._log_box: ctk.CTkTextbox | None = None
        self.adv_open = False

        root.title("incant")
        root.geometry("540x620")
        root.minsize(420, 380)
        try:
            root.iconbitmap(str(ICON_ICO))
        except Exception:  # noqa: BLE001
            pass

        self._build_ui()
        self.bind_hotkey(self.settings["hotkey"])
        self.bind_command_hotkey(self.settings["command_hotkey"])
        try:
            self.engine.start_stream()
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[audio] could not open mic: {e}")
        self.reload_model()
        self.root.after(80, self._drain_queue)
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
            text="Activity",
            width=84,
            height=30,
            fg_color="#2b2b2b",
            hover_color="#3a3a3a",
            command=self.open_log_window,
        ).pack(side="left")

        # --- Scrollable body (so Advanced scrolls on small windows) ---------
        body = ctk.CTkScrollableFrame(root, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=(PADX - 12), pady=0)
        body.grid_columnconfigure(0, weight=1)
        IPADX = 10  # inner padding inside the scroll area

        # --- Status pill ----------------------------------------------------
        pill = ctk.CTkFrame(body, corner_radius=10)
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
        card = ctk.CTkFrame(body, corner_radius=12)
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

        # --- Advanced (collapsible) ----------------------------------------
        self.adv_btn = ctk.CTkButton(
            body,
            text="▸  Advanced",
            anchor="w",
            height=32,
            fg_color="transparent",
            hover_color="#2b2b2b",
            text_color="#b0b0b0",
            command=self._toggle_advanced,
        )
        self.adv_btn.grid(row=2, column=0, sticky="ew", padx=IPADX, pady=(8, 0))

        self.adv = ctk.CTkFrame(body, corner_radius=12)
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

    # ------------------------------------------------------------ log window
    def open_log_window(self) -> None:
        if self._log_win is not None and self._log_win.winfo_exists():
            self._log_win.focus()
            return
        win = ctk.CTkToplevel(self.root)
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
        win = ctk.CTkToplevel(self.root)
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

    def persist(self) -> None:
        lang = self.lang_menu.get()
        self.settings.update(
            hotkey=self.hotkey_var.get().strip(),
            command_hotkey=self.command_hotkey_var.get().strip(),
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

    # ----------------------------------------------------------------- model
    def reload_model(self) -> None:
        size = MODELS[self.model_menu.get()]
        self.persist()
        self.set_status(f"loading {size}…")
        self.log_line(f"[load] loading {size} (first use downloads weights)…")

        def work() -> None:
            with self.model_lock:
                try:
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
                text, detected = stt.transcribe_audio(
                    self.model, audio, lang, beam_size=self._beam()
                )
            if text:
                self.log_line(f"[{detected}] {text}")
                keyboard.write(text + " ", delay=0)
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
                    text, _ = stt.transcribe_audio(
                        self.model,
                        audio,
                        lang,
                        initial_prompt=prompt,
                        beam_size=self._beam(),
                    )
                out = self.stitcher.next(text) if self.stitcher else text
                if out:
                    self.log_line(f"› {text}")
                    keyboard.write(out, delay=0)
            except Exception as e:  # noqa: BLE001
                self.log_line(f"[stt] phrase error: {e}")

    # word by word ----------------------------------------------------------
    def _toggle_word(self) -> None:
        if self.word_streamer is None:

            def tx(audio, prompt):
                lang = self.settings.get("language") or None
                with self.model_lock:
                    return stt.transcribe_words(
                        self.model,
                        audio,
                        lang,
                        initial_prompt=prompt,
                        beam_size=self._beam(),
                    )

            def out(text):
                keyboard.write(text, delay=0)
                self.log_line(f"› {text.strip()}")

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
                    self.model, audio, lang, beam_size=self._beam()
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
