"""What went wrong, expressed as what the caller should do about it.

Four kinds, because an import loop only ever has four answers: stop and ask
the user for new credentials, wait and try the same call again, skip this one
message and keep going, or throw the cursor away and walk the whole mailbox.
Anything a provider adapter raises has to be one of them — an adapter that
lets an ``httpx`` error escape has not decided.

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


class MailCursorExpired(MailError):
    """The stored cursor is too old to resume from — walk everything instead.

    A sibling of the three above and deliberately **not** a subclass of
    :class:`MailPermanentError`, although a provider usually says it with the
    same 404. The taxonomy is a routing table, and inheriting would let two
    existing handlers claim this one silently: the engine's per-message
    ``except MailPermanentError`` would file an expired cursor as a skipped
    message that never existed, and the job worker would fail the job *and*
    write a ``mail_failed_messages`` row for a ``provider_message_id`` nobody
    can look up. Neither of those is "start over"; only its own type gets it
    to the one handler that is.

    Gmail's documentation is the case that forced it: a ``startHistoryId``
    older than roughly a week returns a 404, and the remedy it names is a full
    sync. IMAP says the same thing as a changed ``UIDVALIDITY``.
    """
