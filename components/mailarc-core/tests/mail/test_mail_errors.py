"""The taxonomy, checked as what it is: a routing decision, not three names.

The engine catches these by class, so the inheritance is load-bearing — a
`MailAuthError` that also matched the transient branch would be retried until
the job gave up instead of asking the user to sign in again.
"""

import pytest

from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)


@pytest.mark.parametrize(
    "error", [MailAuthError, MailTransientError, MailPermanentError]
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
    ],
)
def test_the_three_kinds_do_not_overlap(raised, other) -> None:
    """Each one means a different reaction, so none may catch another."""
    assert not issubclass(raised, other)


def test_a_transient_error_can_carry_the_providers_retry_after() -> None:
    error = MailTransientError("429 Too Many Requests", retry_after=30.0)

    assert error.retry_after == 30.0
    assert "429" in str(error)


def test_a_transient_error_without_a_retry_after_says_so() -> None:
    """`None` means "back off however you like", not "retry immediately"."""
    assert MailTransientError("connection reset").retry_after is None
