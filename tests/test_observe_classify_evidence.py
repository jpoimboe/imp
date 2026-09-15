"""The anchored classifier fires only on recorded evidence that the model did not
write the text: zero billed output tokens, or a stop_sequence close imp never
configures. Absent evidence is never read as zero."""
from __future__ import annotations

import pytest

from imp.providers import errors as perr
from imp.providers.claude import _termination_from_result
from imp.providers.observe_classify import (
    _classify_agent_text,
    classifies_as_provider_error,
)
from tests.cc_fakes import result

AUTH_CHROME = "Failed to authenticate. API Error: 401 OAuth access token has been revoked."
LIMIT_CHROME = "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"
QUOTA_CHROME = "You have reached your usage limit"
# zopo54: a design essay whose opening markdown paragraph quotes a quota pattern,
# so the first-line anchor alone does nothing.
ESSAY = (
    'A true quota error (`"You have reached your usage limit"`) maps to an immediate '
    "pause, so the classifier must not fire on prose that merely discusses it.\n\n"
    "The fix gates the scan on recorded evidence of who wrote the message."
)


def test_agent_prose_quoting_a_pattern_does_not_classify() -> None:
    assert _classify_agent_text(
        ESSAY, output_tokens=1763, stop_reason="end_turn") is None


def test_the_same_text_with_zero_output_tokens_classifies() -> None:
    assert isinstance(
        _classify_agent_text(ESSAY, output_tokens=0, stop_reason="end_turn"),
        perr.QuotaError)


def test_archived_chrome_shapes_keep_their_categories() -> None:
    assert isinstance(
        _classify_agent_text(LIMIT_CHROME, output_tokens=0,
                             stop_reason="stop_sequence"),
        perr.RateLimitError)
    auth = _classify_agent_text(AUTH_CHROME, output_tokens=0,
                                stop_reason="stop_sequence")
    assert isinstance(auth, perr.AuthError)
    assert getattr(auth, "kind", "") == "http_401"
    assert isinstance(
        _classify_agent_text(QUOTA_CHROME, output_tokens=0,
                             stop_reason="stop_sequence"),
        perr.QuotaError)


def test_either_marker_alone_suffices() -> None:
    assert isinstance(
        _classify_agent_text(QUOTA_CHROME, output_tokens=42,
                             stop_reason="stop_sequence"),
        perr.QuotaError)
    assert isinstance(
        _classify_agent_text(QUOTA_CHROME, output_tokens=0, stop_reason="end_turn"),
        perr.QuotaError)


def test_absent_evidence_is_never_read_as_zero() -> None:
    assert _classify_agent_text(ESSAY) is None
    assert _classify_agent_text(AUTH_CHROME) is None


def test_the_tag_predicate_requires_both_evidence_keywords() -> None:
    assert classifies_as_provider_error(
        AUTH_CHROME, output_tokens=0, stop_reason="stop_sequence") is True
    assert classifies_as_provider_error(
        ESSAY, output_tokens=1763, stop_reason="end_turn") is False
    with pytest.raises(TypeError):
        classifies_as_provider_error(AUTH_CHROME, output_tokens=0)
    with pytest.raises(TypeError):
        classifies_as_provider_error(AUTH_CHROME, stop_reason="stop_sequence")


def test_the_unanchored_termination_path_ignores_the_gate() -> None:
    # An out-of-band failure signal already established the termination; the
    # pattern only picks the category, mid-document and with no evidence.
    err = _termination_from_result(
        result(is_error=True), f"Here is the report.\n{QUOTA_CHROME} today.", {})
    assert isinstance(err, perr.QuotaError)
