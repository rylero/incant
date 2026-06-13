"""Tests for ellipsis cleaning and phrase stitching."""

import stt


def test_clean_strips_ellipsis_unicode_and_dots():
    assert stt.clean_text("just thinking … here") == "just thinking here"
    assert stt.clean_text("well... maybe") == "well maybe"
    assert stt.clean_text("end of thought...") == "end of thought"


def test_clean_collapses_space_and_leading_punct():
    assert stt.clean_text("  ,  hello   world ") == "hello world"
    assert stt.clean_text("- and then") == "and then"


def test_clean_keeps_normal_sentence_punctuation():
    assert stt.clean_text("Hello there. How are you?") == "Hello there. How are you?"


def test_stitcher_joins_with_single_space():
    s = stt.TextStitcher()
    assert s.next("hello there") == "hello there"      # first: no leading space
    assert s.next("how are you") == " how are you"     # later: leading space


def test_stitcher_strips_ellipsis_in_phrases():
    s = stt.TextStitcher()
    assert s.next("thinking...") == "thinking"
    assert s.next("...and continuing") == " and continuing"


def test_stitcher_prompt_tracks_tail():
    s = stt.TextStitcher(context_chars=10)
    assert s.prompt is None                # nothing emitted yet
    s.next("the quick brown fox")
    assert s.prompt == "brown fox"         # last 10 chars, stripped


def test_stitcher_empty_phrase_emits_nothing():
    s = stt.TextStitcher()
    s.next("hello")
    assert s.next("   ...  ") == ""        # nothing meaningful -> no output
    assert s.prompt == "hello"             # context unchanged
