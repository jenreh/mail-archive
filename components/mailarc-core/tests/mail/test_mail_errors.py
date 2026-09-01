"""The taxonomy, checked as what it is: a routing decision, not four names.

The engine catches these by class, so the inheritance is load-bearing — a
`MailAuthError` that also matched the transient branch would be retried until
the job gave up instead of asking the user to sign in again, and a
`MailCursorExpired` that matched the permanent branch would be filed as a
skipped message instead of starting a full sync.
"""

import pytest

from mailarc_core.mail.errors import (
    MailAuthError,
    MailCursorExpired,
    MailError,
    MailPermanentError,
    MailTransientError,
)


@pytest.mark.parametrize(
    "error",
    [MailAuthError, MailTransientError, MailPermanentError, MailCursorExpired],
)
def test_every_failure_is_catchable_as_one_mail_error(error) -> None:
    with pytest.raises(MailError):
        raise error("boom")


@pytest.mark.parametrize(
    ("raised", "other"),
    [
        (MailAuthError, MailTransientError),
        (MailTransientError, MailPermanentError),
        (MailPermanentError, MailAuthError),
        (MailCursorExpired, MailPermanentError),
        (MailPermanentError, MailCursorExpired),
    ],
)
def test_the_four_kinds_do_not_overlap(raised, other) -> None:
    """Each one means a different reaction, so none may catch another."""
    assert not issubclass(raised, other)


def test_an_expired_cursor_is_not_a_message_that_can_be_skipped() -> None:
    """The pairing that would break quietly rather than loudly.

    Gmail says both with a 404. As a subclass, the engine's per-message
    `except MailPermanentError` would swallow an expired cursor into a
    `mail_failed_messages` row for a message id that never existed, and the
    delta would never fall back to a full walk.
    """
    with pytest.raises(MailError) as escaped:
        raise MailCursorExpired("startHistoryId is too old")

    assert not isinstance(escaped.value, MailPermanentError)


def test_a_transient_error_can_carry_the_providers_retry_after() -> None:
    error = MailTransientError("429 Too Many Requests", retry_after=30.0)

    assert error.retry_after == 30.0
    assert "429" in str(error)


def test_a_transient_error_without_a_retry_after_says_so() -> None:
    """`None` means "back off however you like", not "retry immediately"."""
    assert MailTransientError("connection reset").retry_after is None
