"""Unit tests for stt.apply_snippet."""

import stt

SNIPPETS = {
    "my email": "leroy.ryan09@gmail.com",
    "sign off": "Best regards,\nRyan",
    "home address": "123 Main Street",
}


def test_exact_match():
    assert stt.apply_snippet("my email", SNIPPETS) == "leroy.ryan09@gmail.com"


def test_case_insensitive():
    assert stt.apply_snippet("My Email", SNIPPETS) == "leroy.ryan09@gmail.com"
    assert stt.apply_snippet("MY EMAIL", SNIPPETS) == "leroy.ryan09@gmail.com"


def test_strips_leading_trailing_whitespace():
    assert stt.apply_snippet("  my email  ", SNIPPETS) == "leroy.ryan09@gmail.com"


def test_strips_trailing_punctuation():
    assert stt.apply_snippet("my email.", SNIPPETS) == "leroy.ryan09@gmail.com"
    assert stt.apply_snippet("sign off,", SNIPPETS) == "Best regards,\nRyan"
    assert stt.apply_snippet("home address!", SNIPPETS) == "123 Main Street"
    assert stt.apply_snippet("home address?", SNIPPETS) == "123 Main Street"
    assert stt.apply_snippet("home address;", SNIPPETS) == "123 Main Street"
    assert stt.apply_snippet("home address:", SNIPPETS) == "123 Main Street"


def test_no_match_returns_none():
    assert stt.apply_snippet("something else", SNIPPETS) is None


def test_empty_text_returns_none():
    assert stt.apply_snippet("", SNIPPETS) is None


def test_empty_snippets_returns_none():
    assert stt.apply_snippet("my email", {}) is None


def test_partial_match_does_not_trigger():
    assert stt.apply_snippet("my email address", SNIPPETS) is None
    assert stt.apply_snippet("email", SNIPPETS) is None


def test_multiline_expansion_returned_as_is():
    result = stt.apply_snippet("sign off", SNIPPETS)
    assert result == "Best regards,\nRyan"
