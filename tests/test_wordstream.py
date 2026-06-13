"""Tests for word-by-word streaming logic (pure parts) and WordStreamer commit."""

import numpy as np
import stt


def w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_stable_word_count_holds_trailing():
    words = [w(" the", 0.0, 0.3), w(" quick", 0.3, 0.7), w(" brown", 0.7, 1.5)]
    # audio is 1.6s, hold 0.4s -> cutoff 1.2s ; "brown" ends at 1.5 -> unstable
    assert stt.stable_word_count(words, audio_duration=1.6, hold_s=0.4) == 2


def test_stable_word_count_all_stable_when_old():
    words = [w(" a", 0.0, 0.3), w(" b", 0.3, 0.6)]
    assert stt.stable_word_count(words, audio_duration=2.0, hold_s=0.4) == 2


def test_stable_word_count_none_when_recent():
    words = [w(" hi", 0.0, 0.9)]
    assert stt.stable_word_count(words, audio_duration=1.0, hold_s=0.4) == 0


def test_wordstreamer_commits_only_stable_then_flushes():
    typed = []
    # fake transcriber: ignores audio, returns a fixed growing word list based
    # on how much audio it "sees" (duration). We simulate by closure state.
    script = [
        [w(" hello", 0.0, 0.4)],                       # only 1 word, recent -> held
        [w(" hello", 0.0, 0.4), w(" world", 0.4, 0.8)],
    ]
    calls = {"i": 0}

    def fake_tx(audio, prompt):
        i = min(calls["i"], len(script) - 1)
        calls["i"] += 1
        return script[i]

    ws = stt.WordStreamer(fake_tx, typed.append, interval_s=999, hold_s=0.4)
    # buffer ~1.0s so "hello"(ends .4) is stable, "world" not present yet
    ws.feed(np.ones(stt.SAMPLE_RATE, dtype="float32"))
    ws._tick(final=False)
    assert "".join(typed).strip() == "hello"
    # now more audio; final flush should emit the rest
    ws.feed(np.ones(stt.SAMPLE_RATE, dtype="float32"))
    ws._tick(final=True)
    assert "".join(typed).strip() == "hello world"


def test_wordstreamer_no_duplicate_emits():
    typed = []
    words = [w(" a", 0.0, 0.2), w(" b", 0.2, 0.4)]

    def fake_tx(audio, prompt):
        return words

    ws = stt.WordStreamer(fake_tx, typed.append, interval_s=999, hold_s=0.0)
    ws.feed(np.ones(stt.SAMPLE_RATE, dtype="float32"))
    ws._tick(final=False)
    ws._tick(final=False)  # same words again -> must not re-type
    assert "".join(typed) == " a b"
