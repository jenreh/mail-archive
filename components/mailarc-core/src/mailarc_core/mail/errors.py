"""What went wrong, expressed as what the caller should do about it.

Three kinds, because an import loop only ever has three answers: stop and ask
the user for new credentials, wait and try the same call again, or skip this
one message and keep going. Anything a provider adapter raises has to be one
of them — an adapter that lets an ``httpx`` error escape has not decided.

**No ``except: pass``.** Every skipped message leaves a row behind. A
:class:`MailPermanentError` is a decision to drop *one* message, and that
decision gets recorded, never swallowed.
"""


class MailError(Exception):
    """Base of everything the mail side raises."""


class MailAuthError(MailError):
    """Credentials are missing, expired or rejected.

    Terminal for the job: no amount of retrying fixes a revoked token. The
    account goes to ``auth_error`` and the UI offers a re-consent.
    """


class MailTransientError(MailError):
    """A rate limit, a 5xx or a dropped connection — the same call may work.

    ``retry_after`` carries the provider's own ``Retry-After`` in seconds when
    it sent one. It is a floor for the engine's backoff, not a replacement:
    the engine still adds jitter so a thousand queued jobs do not return at
    the same instant.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MailPermanentError(MailError):
    """This message will never parse or never come back — skip it, record it.

    Broken MIME and a 404 from the provider are the same thing from here:
    retrying is pointless, but the import as a whole is fine.
    """
