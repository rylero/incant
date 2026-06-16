"""
Push-to-talk speech-to-text.

Toggle the hotkey, speak, toggle again -> Whisper transcribes locally and
types the text at your cursor.

Defaults: faster-whisper large-v3 on CUDA (RTX GPU), auto language.
Run:  uv run stt
"""

from __future__ import annotations

import os
import re
import sys
import glob
import time
import threading
from collections import deque

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
# AudioEngine: ONE always-on mic stream + a rolling pre-roll buffer.
#
# Why always-on: opening an InputStream on demand drops the first ~100-300ms
# while the device ramps, which clips the first word. Keeping the stream open
# and prepending a short pre-roll of audio captured *before* the keypress means
# the onset of the first word is always present, and VAD has real leading
# context so it trims silence instead of your speech.
#
# Audio ingestion is split into _ingest(frame) (pure, unit-testable) and the
# sounddevice _callback that calls it.
# ---------------------------------------------------------------------------
class AudioEngine:
    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        blocksize: int = 1600,      # 100 ms frames at 16 kHz
        preroll_s: float = 0.5,
    ) -> None:
        self.sr = sample_rate
        self.blocksize = blocksize
        ring_len = max(1, round(preroll_s * sample_rate / blocksize))
        self._ring: deque[np.ndarray] = deque(maxlen=ring_len)
        self._levels: deque[float] = deque(maxlen=50)  # recent per-frame RMS, for UI waveform
        self._cap: list[np.ndarray] = []
        self._capturing = False
        self._on_frame = None  # optional live consumer (continuous mode)
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    # --- core (testable) ---------------------------------------------------
    def _ingest(self, frame: np.ndarray) -> None:
        frame = np.asarray(frame, dtype=np.float32).flatten()
        rms = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
        with self._lock:
            self._ring.append(frame)
            self._levels.append(rms)
            if self._capturing:
                self._cap.append(frame)
            listener = self._on_frame
        if listener is not None:
            listener(frame)

    def recent_levels(self, n: int) -> list[float]:
        """Last n per-frame RMS values (left-padded with 0.0), for a UI waveform."""
        with self._lock:
            levels = list(self._levels)
        if len(levels) < n:
            levels = [0.0] * (n - len(levels)) + levels
        return levels[-n:]

    def begin_capture(self) -> None:
        """Start collecting; seed with the pre-roll ring so the onset is kept."""
        with self._lock:
            self._cap = list(self._ring)
            self._capturing = True

    def end_capture(self) -> np.ndarray:
        with self._lock:
            self._capturing = False
            frames, self._cap = self._cap, []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames).flatten()

    def preroll_audio(self) -> np.ndarray:
        """Snapshot of the pre-roll ring (used to prime continuous mode)."""
        with self._lock:
            frames = list(self._ring)
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames).flatten()

    def set_frame_listener(self, fn) -> None:
        with self._lock:
            self._on_frame = fn

    def set_preroll(self, seconds: float) -> None:
        """Resize the pre-roll ring (kept audio before a keypress)."""
        with self._lock:
            new_len = max(1, round(seconds * self.sr / self.blocksize))
            self._ring = deque(list(self._ring)[-new_len:], maxlen=new_len)

    @property
    def capturing(self) -> bool:
        return self._capturing

    # --- device ------------------------------------------------------------
    def _callback(self, indata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"[audio] {status}", flush=True)
        self._ingest(indata[:, 0].copy())

    def start_stream(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            samplerate=self.sr,
            channels=1,
            dtype="float32",
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def stop_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


# Whisper writes "..." or the unicode ellipsis to mark trailing-off / hesitant
# speech. In dictation that is almost always noise, so strip it.
_ELLIPSIS = re.compile(r"\s*(?:\.{2,}|…)\s*")


def clean_text(text: str) -> str:
    """Remove hesitation ellipses and tidy whitespace/stray leading punctuation."""
    text = _ELLIPSIS.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"^[,;:\-\s]+", "", text)  # drop a stray leading comma/dash
    return text.strip()


def transcribe_audio(
    model: WhisperModel,
    audio: np.ndarray,
    language: str | None,
    initial_prompt: str | None = None,
    beam_size: int = 5,
    hotwords: str | None = None,
):
    """Run Whisper on a mono 16k float32 array. Returns (clean_text, language)."""
    if audio.size < SAMPLE_RATE * 0.3:  # <0.3s -> nothing useful
        return "", None
    # Pad ~0.2s of silence at the front: Whisper occasionally drops the first
    # token when speech starts at sample 0, and silero VAD keeps the onset when
    # it has a little lead-in. speech_pad_ms widens kept speech regions so a
    # soft first word is not trimmed.
    audio = np.concatenate([np.zeros(int(SAMPLE_RATE * 0.2), np.float32), audio])
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=600),
    )
    text = "".join(seg.text for seg in segments).strip()
    return clean_text(text), info.language


def transcribe_words(
    model: WhisperModel,
    audio: np.ndarray,
    language: str | None,
    initial_prompt: str | None = None,
    beam_size: int = 5,
    hotwords: str | None = None,
) -> list[dict]:
    """Transcribe with word timestamps. Returns [{word, start, end}, ...]."""
    if audio.size < SAMPLE_RATE * 0.3:
        return []
    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=600),
    )
    words: list[dict] = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({"word": w.word, "start": w.start, "end": w.end})
    return words


class TextStitcher:
    """
    Joins phrase-by-phrase transcripts (continuous mode) into clean text.

    - Separates phrases with a single space.
    - Tracks the tail of emitted text to feed back as Whisper's initial_prompt
      so the next phrase is transcribed with preceding context (better
      continuity and capitalization than transcribing each phrase cold).
    """

    def __init__(self, context_chars: int = 160) -> None:
        self._emitted = ""
        self._context_chars = context_chars

    @property
    def prompt(self) -> str | None:
        """Preceding context to bias the next phrase (None if nothing yet)."""
        tail = self._emitted[-self._context_chars :].strip()
        return tail or None

    def next(self, phrase: str) -> str:
        """Return the text to type for this phrase (incl. leading space)."""
        phrase = clean_text(phrase)
        if not phrase:
            return ""
        prefix = " " if self._emitted else ""
        self._emitted += prefix + phrase
        return prefix + phrase


# ---------------------------------------------------------------------------
# Word-by-word mode: re-transcribe the growing utterance on a cadence and emit
# only words that have become stable (commit-only, never rewrite -> no
# backspacing). The last word(s) are held back until enough trailing audio
# confirms them.
# ---------------------------------------------------------------------------
def stable_word_count(words: list[dict], audio_duration: float, hold_s: float) -> int:
    """How many leading words end before the trailing 'hold' zone (are stable)."""
    cutoff = audio_duration - hold_s
    n = 0
    for w in words:
        if w["end"] <= cutoff:
            n += 1
        else:
            break
    return n


class WordStreamer:
    """
    Feed live frames; types words as they stabilise. Runs its own worker thread
    that periodically transcribes the pending audio. transcribe_words_fn must
    have signature (audio, initial_prompt) -> [{word,start,end}, ...].
    """

    def __init__(
        self,
        transcribe_words_fn,
        on_text,
        sample_rate: int = SAMPLE_RATE,
        interval_s: float = 0.5,
        hold_s: float = 0.4,
        retrim_s: float = 4.0,
        context_chars: int = 200,
    ) -> None:
        self._tx = transcribe_words_fn
        self._on_text = on_text
        self.sr = sample_rate
        self.interval_s = interval_s
        self.hold_s = hold_s
        self.retrim_s = retrim_s
        self._context_chars = context_chars
        self._buf: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._committed = 0   # words already typed from the current (untrimmed) buffer
        self._prompt = ""     # committed text history for context

    def feed(self, frame: np.ndarray) -> None:
        with self._lock:
            self._buf.append(np.asarray(frame, dtype=np.float32).flatten())

    def _snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._buf:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._buf).flatten()

    def _trim_to(self, sample_idx: int) -> None:
        with self._lock:
            audio = np.concatenate(self._buf).flatten() if self._buf else np.zeros(0, np.float32)
            audio = audio[sample_idx:]
            self._buf = [audio] if audio.size else []

    def _emit(self, new_words: list[dict]) -> None:
        text = "".join(w["word"] for w in new_words)
        if text:
            self._on_text(text)
            self._prompt = (self._prompt + text)[-self._context_chars :]

    def _tick(self, final: bool) -> None:
        audio = self._snapshot()
        dur = audio.size / self.sr
        if dur < 0.3:
            return
        words = self._tx(audio, self._prompt or None)
        if not words:
            return
        n = len(words) if final else stable_word_count(words, dur, self.hold_s)
        if n > self._committed:
            self._emit(words[self._committed:n])
            self._committed = n
        # bound cost: once the buffer is long, drop the committed prefix audio
        if not final and dur > self.retrim_s and 0 < self._committed <= len(words):
            cut_end = words[self._committed - 1]["end"]
            self._trim_to(int(cut_end * self.sr))
            self._committed = 0

    def _loop(self) -> None:
        while self._running:
            time.sleep(self.interval_s)
            try:
                self._tick(final=False)
            except Exception as e:  # noqa: BLE001
                print(f"[word] tick error: {e}", flush=True)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._tick(final=True)  # flush remaining words


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
        min_silence_s: float = 0.8,
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    model = load_model()
    # Warm up CUDA kernels (first transcribe JITs PTX on new GPUs ~ several s)
    print("[load] warming up...", flush=True)
    list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))[0])
    print("[load] warm.", flush=True)

    engine = AudioEngine()
    engine.start_stream()  # always-on mic + pre-roll
    lock = threading.Lock()
    busy = threading.Event()

    def transcribe_and_type(audio: np.ndarray) -> None:
        try:
            t0 = time.time()
            text, lang = transcribe_audio(model, audio, LANGUAGE)
            dur = audio.size / SAMPLE_RATE
            print(f"[stt] {dur:.1f}s -> {time.time() - t0:.1f}s [{lang}]: {text!r}",
                  flush=True)
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
            if not engine.capturing:
                engine.begin_capture()
                print("[rec] ● recording... (press hotkey again to stop)", flush=True)
            else:
                audio = engine.end_capture()
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
