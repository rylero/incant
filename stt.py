"""
Push-to-talk speech-to-text.

Toggle the hotkey, speak, toggle again -> Whisper transcribes locally and
types the text at your cursor.

Defaults: faster-whisper large-v3 on CUDA (RTX GPU), auto language.
Run:  uv run stt
"""

from __future__ import annotations

import os
import sys
import glob
import time
import threading

# ---------------------------------------------------------------------------
# Config (override via env vars)
# ---------------------------------------------------------------------------
HOTKEY = os.environ.get("STT_HOTKEY", "ctrl+alt+space")
MODEL_SIZE = os.environ.get("STT_MODEL", "large-v3")
LANGUAGE = os.environ.get("STT_LANG") or None   # None = auto-detect
SAMPLE_RATE = 16000                             # Whisper expects 16 kHz mono
TYPE_DELAY = float(os.environ.get("STT_TYPE_DELAY", "0"))  # seconds/char
TRAILING_SPACE = os.environ.get("STT_TRAILING_SPACE", "1") == "1"


# ---------------------------------------------------------------------------
# Make bundled NVIDIA CUDA/cuDNN DLLs discoverable on Windows.
# faster-whisper -> CTranslate2 needs cublas + cudnn on the DLL search path.
# The pip packages drop them in site-packages/nvidia/*/bin ; register those.
# ---------------------------------------------------------------------------
def _register_cuda_dlls() -> None:
    found: list[str] = []
    for site in sys.path:
        for binpath in glob.glob(os.path.join(site, "nvidia", "*", "bin")):
            if os.path.isdir(binpath):
                found.append(binpath)
    # CTranslate2 delay-loads cublas/cudnn by name; on Windows it searches PATH,
    # so add_dll_directory alone is not enough -- prepend to PATH too.
    if found:
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            for binpath in found:
                try:
                    os.add_dll_directory(binpath)
                except OSError:
                    pass


_register_cuda_dlls()

import numpy as np            # noqa: E402
import sounddevice as sd      # noqa: E402
import keyboard               # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402


# ---------------------------------------------------------------------------
# Load the model (GPU first, CPU fallback)
# ---------------------------------------------------------------------------
def load_model(model_size: str | None = None) -> WhisperModel:
    size = model_size or MODEL_SIZE
    attempts = [
        ("cuda", "float16"),
        ("cuda", "int8_float16"),
        ("cpu", "int8"),
    ]
    last_err = None
    for device, compute in attempts:
        try:
            print(f"[load] {size} on {device} ({compute}) ...", flush=True)
            t0 = time.time()
            m = WhisperModel(size, device=device, compute_type=compute)
            print(f"[load] ready in {time.time() - t0:.1f}s on {device}", flush=True)
            return m
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[load] {device}/{compute} failed: {e}", flush=True)
    raise SystemExit(f"Could not load model: {last_err}")


# ---------------------------------------------------------------------------
# Recorder: toggle on/off, capture mono 16 kHz float32
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self) -> None:
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self.active = False

    def _callback(self, indata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"[audio] {status}", flush=True)
        self._frames.append(indata.copy())

    def start(self) -> None:
        self._frames = []
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self.active = True

    def stop(self) -> np.ndarray:
        self.active = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._frames, axis=0).flatten()


def transcribe_audio(model: WhisperModel, audio: np.ndarray, language: str | None):
    """Run Whisper on a mono 16k float32 array. Returns (text, language)."""
    if audio.size < SAMPLE_RATE * 0.3:  # <0.3s -> nothing useful
        return "", None
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300),
    )
    text = "".join(seg.text for seg in segments).strip()
    return text, info.language


# ---------------------------------------------------------------------------
# Continuous mode: cut the live audio stream into phrases at silence gaps.
# Pure logic (no audio I/O) so it is easy to unit-test.
# ---------------------------------------------------------------------------
class PhraseSegmenter:
    """
    Feed mono float32 frames; whenever a run of speech is followed by enough
    trailing silence, the accumulated phrase audio is handed to on_phrase().
    Leading silence is dropped; a little trailing silence is kept for context.
    """

    def __init__(
        self,
        on_phrase,
        sample_rate: int = SAMPLE_RATE,
        silence_rms: float = 0.012,
        min_silence_s: float = 0.6,
        min_phrase_s: float = 0.3,
        max_phrase_s: float = 20.0,
    ) -> None:
        self.on_phrase = on_phrase
        self.sr = sample_rate
        self.silence_rms = silence_rms
        self.min_silence_s = min_silence_s
        self.min_phrase_s = min_phrase_s
        self.max_phrase_s = max_phrase_s
        self._buf: list[np.ndarray] = []
        self._silence_s = 0.0
        self._have_speech = False
        self._speech_s = 0.0  # actual speech (not trailing silence) in buffer

    def feed(self, frame: np.ndarray) -> None:
        frame = np.asarray(frame, dtype=np.float32).flatten()
        if frame.size == 0:
            return
        dur = frame.size / self.sr
        rms = float(np.sqrt(np.mean(frame * frame)))
        if rms >= self.silence_rms:
            self._have_speech = True
            self._silence_s = 0.0
            self._buf.append(frame)
            self._speech_s += dur
            if self._speech_s >= self.max_phrase_s:  # hard cut very long runs
                self._flush()
        elif self._have_speech:
            self._buf.append(frame)  # keep trailing silence
            self._silence_s += dur
            if self._silence_s >= self.min_silence_s:
                self._flush()
        # else: leading silence -> drop

    def _flush(self) -> None:
        if self._have_speech and self._buf and self._speech_s >= self.min_phrase_s:
            audio = np.concatenate(self._buf).flatten()
            self.on_phrase(audio)
        self._buf = []
        self._silence_s = 0.0
        self._speech_s = 0.0
        self._have_speech = False

    def finish(self) -> None:
        """Call when recording stops to flush any in-progress phrase."""
        self._flush()


class StreamingRecorder:
    """Captures the mic and feeds frames to a PhraseSegmenter in real time."""

    def __init__(self, segmenter: PhraseSegmenter) -> None:
        self.segmenter = segmenter
        self._stream: sd.InputStream | None = None
        self.active = False

    def _callback(self, indata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"[audio] {status}", flush=True)
        self.segmenter.feed(indata[:, 0].copy())

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self.active = True

    def stop(self) -> None:
        self.active = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.segmenter.finish()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    model = load_model()
    # Warm up CUDA kernels (first transcribe JITs PTX on new GPUs ~ several s)
    print("[load] warming up...", flush=True)
    list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))[0])
    print("[load] warm.", flush=True)
    rec = Recorder()
    lock = threading.Lock()
    busy = threading.Event()

    def transcribe_and_type(audio: np.ndarray) -> None:
        try:
            if audio.size < SAMPLE_RATE * 0.3:  # <0.3s -> nothing useful
                print("[stt] too short, skipped", flush=True)
                return
            t0 = time.time()
            segments, info = model.transcribe(
                audio,
                language=LANGUAGE,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=300),
            )
            text = "".join(seg.text for seg in segments).strip()
            dur = audio.size / SAMPLE_RATE
            print(
                f"[stt] {dur:.1f}s audio -> {time.time() - t0:.1f}s "
                f"[{info.language} {info.language_probability:.2f}]: {text!r}",
                flush=True,
            )
            if text:
                if TRAILING_SPACE:
                    text += " "
                keyboard.write(text, delay=TYPE_DELAY)
        finally:
            busy.clear()

    def toggle() -> None:
        with lock:
            if busy.is_set():
                print("[stt] still transcribing, ignoring toggle", flush=True)
                return
            if not rec.active:
                rec.start()
                print("[rec] ● recording... (press hotkey again to stop)", flush=True)
            else:
                audio = rec.stop()
                busy.set()
                print("[rec] ■ stopped, transcribing...", flush=True)
                threading.Thread(
                    target=transcribe_and_type, args=(audio,), daemon=True
                ).start()

    keyboard.add_hotkey(HOTKEY, toggle, suppress=False)

    print("=" * 56)
    print(f"  Hotkey:   {HOTKEY}   (toggle to start/stop)")
    print(f"  Model:    {MODEL_SIZE}")
    print(f"  Language: {LANGUAGE or 'auto'}")
    print(f"  Output:   typed at cursor")
    print("  Ctrl+C in this window to quit.")
    print("=" * 56, flush=True)

    try:
        keyboard.wait()  # block forever; hotkey runs in its own thread
    except KeyboardInterrupt:
        print("\n[exit] bye", flush=True)


if __name__ == "__main__":
    main()
