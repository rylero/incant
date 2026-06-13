"""Unit tests for AudioEngine pre-roll / capture logic (no mic needed)."""

import numpy as np

import stt

SR = stt.SAMPLE_RATE
BS = 1600  # 100 ms blocks


def feed_blocks(eng: stt.AudioEngine, n_blocks: int, value: float):
    for i in range(n_blocks):
        eng._ingest(np.full(BS, value, dtype="float32"))


def test_capture_includes_preroll():
    eng = stt.AudioEngine(blocksize=BS, preroll_s=0.5)  # ring holds 5 blocks
    feed_blocks(eng, 5, 0.1)        # pre-roll audio BEFORE capture starts
    eng.begin_capture()
    feed_blocks(eng, 10, 0.2)       # spoken audio
    audio = eng.end_capture()
    # capture = 5 preroll blocks + 10 spoken blocks
    assert audio.size == 15 * BS
    # first samples are the pre-roll value, proving the onset was retained
    assert np.allclose(audio[:BS], 0.1)
    assert np.allclose(audio[-BS:], 0.2)


def test_preroll_ring_is_bounded():
    eng = stt.AudioEngine(blocksize=BS, preroll_s=0.5)  # 5 blocks max
    feed_blocks(eng, 50, 0.3)       # way more than the ring holds
    eng.begin_capture()
    eng.end_capture()
    pre = eng.preroll_audio()
    assert pre.size == 5 * BS       # bounded to preroll length


def test_not_capturing_by_default():
    eng = stt.AudioEngine(blocksize=BS)
    assert eng.capturing is False
    feed_blocks(eng, 3, 0.2)
    assert eng.end_capture().size == 0  # nothing captured when idle


def test_frame_listener_receives_frames():
    eng = stt.AudioEngine(blocksize=BS)
    got = []
    eng.set_frame_listener(got.append)
    feed_blocks(eng, 3, 0.2)
    assert len(got) == 3
    eng.set_frame_listener(None)
    feed_blocks(eng, 2, 0.2)
    assert len(got) == 3            # detached -> no more frames


def test_second_capture_is_independent():
    eng = stt.AudioEngine(blocksize=BS, preroll_s=0.0)  # ring >=1 block
    eng.begin_capture()
    feed_blocks(eng, 4, 0.2)
    first = eng.end_capture()
    eng.begin_capture()
    feed_blocks(eng, 2, 0.2)
    second = eng.end_capture()
    assert first.size > second.size  # old capture didn't leak into the new one
