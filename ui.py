"""
Tiny GUI for the speech-to-text tool.

- Set the start/stop hotkey (click "Set" and press your combo).
- Pick a model: small (fast) ... large-v3 (most accurate).
- Press the hotkey anywhere: toggle to record, toggle again to type the text.

Run:  uv run ui
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import numpy as np
import keyboard
import tkinter as tk
from tkinter import ttk

import stt  # noqa: E402  (stt sets up CUDA DLLs on import)

SETTINGS_PATH = Path(__file__).with_name("settings.json")

# label -> (model id, blurb)
MODELS = {
    "small  — fastest, ok accuracy": "small",
    "medium — balanced": "medium",
    "large-v3 — most accurate (default)": "large-v3",
}
OUTPUT_MODES = {
    "Insert all at once (capture)": "capture",
    "Continuous (type as you pause)": "continuous",
}
DEFAULTS = {
    "hotkey": "ctrl+alt+space",
    "model": "large-v3",
    "language": "",
    "output_mode": "capture",
}


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


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = load_settings()
        self.model = None
        self.model_lock = threading.Lock()
        self.engine = stt.AudioEngine()  # always-on mic + pre-roll
        self.busy = threading.Event()
        self.hotkey_handle = None
        self.ui_queue: queue.Queue = queue.Queue()
        # continuous-mode state
        self.continuous_on = False
        self.segmenter: stt.PhraseSegmenter | None = None
        self.stitcher: stt.TextStitcher | None = None
        self.phrase_queue: queue.Queue = queue.Queue()
        self.phrase_worker: threading.Thread | None = None

        root.title("Speech → Text")
        root.geometry("520x470")
        root.minsize(460, 410)

        pad = dict(padx=10, pady=6)
        frm = ttk.Frame(root)
        frm.pack(fill="both", expand=True)

        # --- Hotkey row -----------------------------------------------------
        ttk.Label(frm, text="Hotkey (start/stop):").grid(row=0, column=0, sticky="w", **pad)
        self.hotkey_var = tk.StringVar(value=self.settings["hotkey"])
        self.hotkey_entry = ttk.Entry(frm, textvariable=self.hotkey_var, width=22)
        self.hotkey_entry.grid(row=0, column=1, sticky="we", **pad)
        self.set_btn = ttk.Button(frm, text="Set…", command=self.capture_hotkey)
        self.set_btn.grid(row=0, column=2, **pad)

        # --- Model row ------------------------------------------------------
        ttk.Label(frm, text="Model:").grid(row=1, column=0, sticky="w", **pad)
        cur_label = next(
            (k for k, v in MODELS.items() if v == self.settings["model"]),
            list(MODELS)[-1],
        )
        self.model_var = tk.StringVar(value=cur_label)
        self.model_combo = ttk.Combobox(
            frm, textvariable=self.model_var, values=list(MODELS),
            state="readonly", width=34,
        )
        self.model_combo.grid(row=1, column=1, columnspan=2, sticky="we", **pad)
        self.model_combo.bind("<<ComboboxSelected>>", lambda e: self.reload_model())

        # --- Language row ---------------------------------------------------
        ttk.Label(frm, text="Language:").grid(row=2, column=0, sticky="w", **pad)
        self.lang_var = tk.StringVar(value=self.settings.get("language", ""))
        self.lang_combo = ttk.Combobox(
            frm, textvariable=self.lang_var,
            values=["", "en", "es", "fr", "de", "it", "pt", "zh", "ja"],
            width=10,
        )
        self.lang_combo.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(frm, text="(blank = auto-detect)").grid(row=2, column=2, sticky="w")
        self.lang_combo.bind("<<ComboboxSelected>>", lambda e: self.persist())
        self.lang_combo.bind("<FocusOut>", lambda e: self.persist())

        # --- Output mode row ------------------------------------------------
        ttk.Label(frm, text="Typing:").grid(row=3, column=0, sticky="w", **pad)
        cur_out = next(
            (k for k, v in OUTPUT_MODES.items() if v == self.settings.get("output_mode")),
            list(OUTPUT_MODES)[0],
        )
        self.output_var = tk.StringVar(value=cur_out)
        self.output_combo = ttk.Combobox(
            frm, textvariable=self.output_var, values=list(OUTPUT_MODES),
            state="readonly", width=34,
        )
        self.output_combo.grid(row=3, column=1, columnspan=2, sticky="we", **pad)
        self.output_combo.bind("<<ComboboxSelected>>", lambda e: self.persist())

        # --- Status ---------------------------------------------------------
        self.status_var = tk.StringVar(value="loading model…")
        self.status = ttk.Label(frm, textvariable=self.status_var, font=("Segoe UI", 11, "bold"))
        self.status.grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(12, 4))

        # --- Log ------------------------------------------------------------
        self.log = tk.Text(frm, height=10, wrap="word", state="disabled",
                           font=("Consolas", 9))
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=10, pady=6)

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(5, weight=1)

        self.bind_hotkey(self.settings["hotkey"])
        try:
            self.engine.start_stream()  # start mic now so pre-roll is always filling
        except Exception as e:  # noqa: BLE001
            self.log_line(f"[audio] could not open mic: {e}")
        self.reload_model()  # initial load
        self.root.after(100, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- helpers ------------------------------------------------------------
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
                elif kind == "enable_set":
                    self.set_btn.configure(state="normal")
                elif kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", val + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def persist(self) -> None:
        self.settings["hotkey"] = self.hotkey_var.get().strip()
        self.settings["model"] = MODELS[self.model_var.get()]
        self.settings["language"] = self.lang_var.get().strip()
        self.settings["output_mode"] = OUTPUT_MODES[self.output_var.get()]
        save_settings(self.settings)

    # -- hotkey -------------------------------------------------------------
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
        self.set_btn.configure(state="disabled")
        self.set_status("press your hotkey combo…")

        def grab() -> None:
            hk = keyboard.read_hotkey(suppress=False)
            self.hotkey_var.set(hk)
            self.bind_hotkey(hk)
            self.persist()
            self.ui_queue.put(("status", "ready"))
            self.ui_queue.put(("enable_set", None))

        threading.Thread(target=grab, daemon=True).start()

    # -- model --------------------------------------------------------------
    def reload_model(self) -> None:
        size = MODELS[self.model_var.get()]
        self.persist()
        self.set_status(f"loading {size}…")
        self.log_line(f"[load] loading {size} (first time downloads weights)…")

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

    # -- record / transcribe ------------------------------------------------
    def toggle(self) -> None:
        if self.model is None:
            self.log_line("[stt] model not ready yet")
            return
        if self.settings.get("output_mode") == "continuous":
            self._toggle_continuous()
        else:
            self._toggle_capture()

    # --- capture mode (record all, transcribe once) ------------------------
    def _toggle_capture(self) -> None:
        if self.busy.is_set():
            self.log_line("[stt] still transcribing…")
            return
        if not self.engine.capturing:
            self.engine.begin_capture()  # seeded with pre-roll -> no clipped first word
            self.set_status("● recording… (hotkey again to stop)")
        else:
            audio = self.engine.end_capture()
            self.busy.set()
            self.set_status("transcribing…")
            threading.Thread(target=self._do_transcribe, args=(audio,), daemon=True).start()

    # --- continuous mode (type phrase-by-phrase on pauses) -----------------
    def _toggle_continuous(self) -> None:
        if not self.continuous_on:
            # 1.0s gap so mid-sentence thinking pauses don't split sentences
            self.segmenter = stt.PhraseSegmenter(
                on_phrase=self.phrase_queue.put, min_silence_s=1.0
            )
            self.stitcher = stt.TextStitcher()
            # prime with pre-roll so the very first phrase keeps its onset
            for f in (self.engine.preroll_audio(),):
                if f.size:
                    self.segmenter.feed(f)
            self.phrase_worker = threading.Thread(target=self._phrase_loop, daemon=True)
            self.phrase_worker.start()
            self.engine.set_frame_listener(self.segmenter.feed)
            self.continuous_on = True
            self.set_status("● listening… (types as you pause; hotkey to stop)")
            self.log_line("[stt] continuous mode on")
        else:
            self.engine.set_frame_listener(None)
            if self.segmenter is not None:
                self.segmenter.finish()  # flush any in-progress phrase
            self.segmenter = None
            self.phrase_queue.put(None)  # worker exits after draining
            self.continuous_on = False
            self.set_status("ready — press your hotkey to talk")
            self.log_line("[stt] continuous mode off")

    def _phrase_loop(self) -> None:
        """Transcribe queued phrase audio in order and type each as it lands."""
        while True:
            audio = self.phrase_queue.get()
            if audio is None:
                return
            try:
                lang = self.lang_var.get().strip() or None
                prompt = self.stitcher.prompt  # preceding text for continuity
                with self.model_lock:
                    text, detected = stt.transcribe_audio(
                        self.model, audio, lang, initial_prompt=prompt
                    )
                out = self.stitcher.next(text)
                if out:
                    self.log_line(f"[{detected}] {text}")
                    keyboard.write(out, delay=0)
            except Exception as e:  # noqa: BLE001
                self.log_line(f"[stt] phrase error: {e}")

    def _do_transcribe(self, audio: np.ndarray) -> None:
        try:
            lang = self.lang_var.get().strip() or None
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

    # -- lifecycle ----------------------------------------------------------
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
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
