"""Unit tests for the phrase segmenter (no audio hardware needed)."""

import numpy as np
import pytest

import stt

SR = stt.SAMPLE_RATE


def speech(seconds: float, amp: float = 0.2) -> np.ndarray:
    n = int(SR * seconds)
    return (np.random.randn(n).astype("float32")) * amp


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype="float32")


def feed_in_chunks(seg: stt.PhraseSegmenter, audio: np.ndarray, chunk_s: float = 0.05):
    step = int(SR * chunk_s)
    for i in range(0, len(audio), step):
        seg.feed(audio[i : i + step])


def test_single_phrase_emitted_after_silence():
    phrases = []
    seg = stt.PhraseSegmenter(on_phrase=phrases.append, min_silence_s=0.6)
    feed_in_chunks(seg, np.concatenate([speech(1.0), silence(0.8)]))
    assert len(phrases) == 1
    assert phrases[0].size / SR >= 1.0


def test_two_phrases_split_on_gap():
    phrases = []
    seg = stt.PhraseSegmenter(on_phrase=phrases.append, min_silence_s=0.6)
    audio = np.concatenate(
        [speech(0.8), silence(0.8), speech(0.8), silence(0.8)]
    )
    feed_in_chunks(seg, audio)
    assert len(phrases) == 2


def test_leading_silence_dropped():
    phrases = []
    seg = stt.PhraseSegmenter(on_phrase=phrases.append, min_silence_s=0.6)
    feed_in_chunks(seg, np.concatenate([silence(1.5), speech(1.0), silence(0.8)]))
    assert len(phrases) == 1
    # phrase should be ~1s of speech + a little trailing silence, not 1.5s lead
    assert phrases[0].size / SR < 1.8


def test_short_blip_ignored():
    phrases = []
    seg = stt.PhraseSegmenter(
        on_phrase=phrases.append, min_silence_s=0.6, min_phrase_s=0.3
    )
    feed_in_chunks(seg, np.concatenate([speech(0.1), silence(0.8)]))
    assert phrases == []


def test_finish_flushes_in_progress_phrase():
    phrases = []
    seg = stt.PhraseSegmenter(on_phrase=phrases.append, min_silence_s=0.6)
    feed_in_chunks(seg, speech(1.0))  # no trailing silence
    assert phrases == []  # not flushed yet
    seg.finish()
    assert len(phrases) == 1


def test_max_phrase_hard_cut():
    phrases = []
    seg = stt.PhraseSegmenter(
        on_phrase=phrases.append, min_silence_s=5.0, max_phrase_s=2.0
    )
    feed_in_chunks(seg, speech(5.0))  # continuous speech, no pause
    assert len(phrases) >= 2  # cut into chunks by max_phrase_s


def test_empty_frame_noop():
    phrases = []
    seg = stt.PhraseSegmenter(on_phrase=phrases.append)
    seg.feed(np.zeros(0, dtype="float32"))
    seg.finish()
    assert phrases == []
