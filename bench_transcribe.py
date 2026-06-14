"""Benchmark faster-whisper large-v3 transcription latency for various clip lengths.

In continuous mode, the perceived delay after you stop talking is roughly:

    silence_s (pause-detection wait) + this transcribe time

This measures the second term so we know how much headroom "smart mode"
pause-shortening actually has.
"""
import time

import numpy as np

import stt

model = stt.load_model()

print("[warmup]")
stt.transcribe_audio(model, np.zeros(stt.SAMPLE_RATE, dtype=np.float32), language=None)

durations = [1, 2, 3, 5, 8, 12]
rng = np.random.default_rng(0)

print(f"{'dur(s)':>6} {'vad=on':>10} {'vad=off':>10}")
for d in durations:
    audio = (rng.standard_normal(int(stt.SAMPLE_RATE * d)).astype(np.float32) * 0.05)

    t0 = time.perf_counter()
    stt.transcribe_audio(model, audio, language=None)
    t_on = time.perf_counter() - t0

    t0 = time.perf_counter()
    segments, _info = model.transcribe(audio, language=None, beam_size=5, vad_filter=False)
    "".join(s.text for s in segments)
    t_off = time.perf_counter() - t0

    print(f"{d:>6} {t_on:>10.3f} {t_off:>10.3f}")
