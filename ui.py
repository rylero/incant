"""
incant — local-first voice transcription with automation.

A modern control panel: set the hotkey, pick a model, choose typing mode, tune
the pause length. Press the hotkey anywhere to dictate into the focused app.

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

import stt  # sets up CUDA DLLs on import

SETTINGS_PATH = Path(__file__).with_name("settings.json")

MODELS = {
    "small · fastest": "small",
    "medium · balanced": "medium",
    "large-v3 · most accurate": "large-v3",
}
OUTPUT_MODES = {"Insert at end": "capture", "Continuous": "continuous"}
LANGS = ["auto", "en", "es", "fr", "de", "it", "pt", "zh", "ja"]
DEFAULTS = {
    "hotkey": "ctrl+alt+space",
    "model": "large-v3",
    "language": "",
    "output_mode": "capture",
    "silence_s": 1.0,
}

# status keyword -> dot color
STATUS_COLORS = {
    "load": "#e0a106",       # amber
    "warm": "#e0a106",
    "ready": "#2ecc71",      # green
    "record": "#e74c3c",     # red
    "listen": "#e74c3c",
    "transcrib": "#3498db",  # blue
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
        self.engine = stt.AudioEngine()
        self.busy = threading.Event()
        self.hotkey_handle = None
        self.ui_queue: queue.Queue = queue.Queue()
        self.continuous_on = False
        self.segmenter: stt.PhraseSegmenter | None = None
        self.stitcher: stt.TextStitcher | None = None
        self.phrase_queue: queue.Queue = queue.Queue()
        self.phrase_worker: threading.Thread | None = None

        root.title("incant")
        root.geometry("540x680")
        root.minsize(500, 620)

        self._build_ui()

        self.bind_hotkey(self.settings["hotkey"])
        try:
            self.engine.start_stream()
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[audio] could not open mic: {e}")
        self._sync_silence_state()
        self.reload_model()
        self.root.after(80, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(3, weight=1)

        PADX = 22

        # --- Header ---------------------------------------------------------
        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=PADX, pady=(20, 6))
        ctk.CTkLabel(
            header, text="incant", font=ctk.CTkFont(size=30, weight="bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            header, text="local voice → text", text_color="#7a7a7a",
            font=ctk.CTkFont(size=13),
        ).pack(anchor="w")

        # --- Status pill ----------------------------------------------------
        pill = ctk.CTkFrame(root, corner_radius=10)
        pill.grid(row=1, column=0, sticky="ew", padx=PADX, pady=(10, 6))
        self.dot = ctk.CTkLabel(pill, text="●", font=ctk.CTkFont(size=18),
                                text_color="#e0a106", width=22)
        self.dot.pack(side="left", padx=(14, 4), pady=10)
        self.status_var = ctk.StringVar(value="loading model…")
        ctk.CTkLabel(pill, textvariable=self.status_var,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
            side="left", pady=10)

        # --- Settings card --------------------------------------------------
        card = ctk.CTkFrame(root, corner_radius=12)
        card.grid(row=2, column=0, sticky="ew", padx=PADX, pady=6)
        card.grid_columnconfigure(1, weight=1)
        r = 0

        def label(text: str, row: int) -> None:
            ctk.CTkLabel(card, text=text, font=ctk.CTkFont(size=13),
                         text_color="#b0b0b0", anchor="w").grid(
                row=row, column=0, sticky="w", padx=(16, 10), pady=11)

        # Hotkey
        label("Hotkey", r)
        hk_row = ctk.CTkFrame(card, fg_color="transparent")
        hk_row.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=8)
        hk_row.grid_columnconfigure(0, weight=1)
        self.hotkey_var = ctk.StringVar(value=self.settings["hotkey"])
        self.hotkey_entry = ctk.CTkEntry(hk_row, textvariable=self.hotkey_var)
        self.hotkey_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.set_btn = ctk.CTkButton(hk_row, text="Set", width=64,
                                     command=self.capture_hotkey)
        self.set_btn.grid(row=0, column=1)
        r += 1

        # Model
        label("Model", r)
        cur_model = next(
            (k for k, v in MODELS.items() if v == self.settings["model"]),
            list(MODELS)[-1],
        )
        self.model_menu = ctk.CTkOptionMenu(
            card, values=list(MODELS), command=lambda _v: self.reload_model())
        self.model_menu.set(cur_model)
        self.model_menu.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=8)
        r += 1

        # Typing mode
        label("Typing", r)
        cur_out = next(
            (k for k, v in OUTPUT_MODES.items() if v == self.settings.get("output_mode")),
            list(OUTPUT_MODES)[0],
        )
        self.output_seg = ctk.CTkSegmentedButton(
            card, values=list(OUTPUT_MODES), command=lambda _v: self._on_mode_change())
        self.output_seg.set(cur_out)
        self.output_seg.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=8)
        r += 1

        # Language
        label("Language", r)
        self.lang_menu = ctk.CTkOptionMenu(
            card, values=LANGS, width=120, command=lambda _v: self.persist())
        self.lang_menu.set(self.settings.get("language") or "auto")
        self.lang_menu.grid(row=r, column=1, sticky="w", padx=(0, 14), pady=8)
        r += 1

        # Silence / pause length
        self.sil_label = ctk.CTkLabel(
            card, text="Pause split", font=ctk.CTkFont(size=13),
            text_color="#b0b0b0", anchor="w")
        self.sil_label.grid(row=r, column=0, sticky="w", padx=(16, 10), pady=11)
        sil_row = ctk.CTkFrame(card, fg_color="transparent")
        sil_row.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=8)
        sil_row.grid_columnconfigure(0, weight=1)
        self.silence_var = ctk.DoubleVar(value=float(self.settings.get("silence_s", 1.0)))
        self.sil_slider = ctk.CTkSlider(
            sil_row, from_=0.4, to=2.0, number_of_steps=16,
            variable=self.silence_var, command=self._on_silence_change)
        self.sil_slider.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.sil_value = ctk.CTkLabel(sil_row, text=f"{self.silence_var.get():.1f}s",
                                      width=40, font=ctk.CTkFont(size=13))
        self.sil_value.grid(row=0, column=1)
        r += 1

        # --- Activity log ---------------------------------------------------
        logwrap = ctk.CTkFrame(root, corner_radius=12)
        logwrap.grid(row=3, column=0, sticky="nsew", padx=PADX, pady=(6, 8))
        logwrap.grid_columnconfigure(0, weight=1)
        logwrap.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(logwrap, text="Activity", text_color="#7a7a7a",
                     font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        self.log = ctk.CTkTextbox(logwrap, font=ctk.CTkFont(family="Consolas", size=12),
                                  wrap="word", activate_scrollbars=True)
        self.log.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self.log.configure(state="disabled")

        ctk.CTkLabel(
            root, text="Keep this window open (minimize it). The hotkey works globally.",
            text_color="#5f5f5f", font=ctk.CTkFont(size=11)).grid(
            row=4, column=0, pady=(0, 12))

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
                elif kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", val + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(80, self._drain_queue)

    def persist(self) -> None:
        lang = self.lang_menu.get()
        self.settings.update(
            hotkey=self.hotkey_var.get().strip(),
            model=MODELS[self.model_menu.get()],
            language="" if lang == "auto" else lang,
            output_mode=OUTPUT_MODES[self.output_seg.get()],
            silence_s=round(float(self.silence_var.get()), 1),
        )
        save_settings(self.settings)

    # ----------------------------------------------------------- silence UI
    def _on_silence_change(self, _v=None) -> None:
        self.sil_value.configure(text=f"{self.silence_var.get():.1f}s")
        self.persist()

    def _on_mode_change(self) -> None:
        self.persist()
        self._sync_silence_state()

    def _sync_silence_state(self) -> None:
        """Pause-split slider only matters in continuous mode."""
        on = self.settings.get("output_mode") == "continuous"
        state = "normal" if on else "disabled"
        color = "#b0b0b0" if on else "#5a5a5a"
        self.sil_slider.configure(state=state)
        self.sil_label.configure(text_color=color)
        self.sil_value.configure(text_color=color)

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
        if self.settings.get("output_mode") == "continuous":
            self._toggle_continuous()
        else:
            self._toggle_capture()

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
            threading.Thread(target=self._do_transcribe, args=(audio,), daemon=True).start()

    def _toggle_continuous(self) -> None:
        if not self.continuous_on:
            gap = float(self.silence_var.get())
            self.segmenter = stt.PhraseSegmenter(
                on_phrase=self.phrase_queue.put, min_silence_s=gap)
            self.stitcher = stt.TextStitcher()
            pre = self.engine.preroll_audio()
            if pre.size:
                self.segmenter.feed(pre)
            self.phrase_worker = threading.Thread(target=self._phrase_loop, daemon=True)
            self.phrase_worker.start()
            self.engine.set_frame_listener(self.segmenter.feed)
            self.continuous_on = True
            self.set_status("● listening… (types as you pause; hotkey to stop)")
            self.log_line(f"[stt] continuous on (pause split {gap:.1f}s)")
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
                        self.model, audio, lang, initial_prompt=prompt)
                out = self.stitcher.next(text) if self.stitcher else text
                if out:
                    self.log_line(f"› {text}")
                    keyboard.write(out, delay=0)
            except Exception as e:  # noqa: BLE001
                self.log_line(f"[stt] phrase error: {e}")

    def _do_transcribe(self, audio: np.ndarray) -> None:
        try:
            lang = self.settings.get("language") or None
            with self.model_lock:
                text, detected = stt.transcribe_audio(self.model, audio, lang)
            if text:
                self.log_line(f"[{detected}] {text}")
                keyboard.write(text + " ", delay=0)
            else:
                self.log_line("[stt] (nothing heard)")
            self.set_status("ready — press your hotkey to talk")
        finally:
            self.busy.clear()

    # ------------------------------------------------------------- lifecycle
    def on_close(self) -> None:
        try:
            if self.continuous_on:
                self.engine.set_frame_listener(None)
                self.phrase_queue.put(None)
            self.engine.stop_stream()
            keyboard.unhook_all_hotkeys()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()


def main() -> None:
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
