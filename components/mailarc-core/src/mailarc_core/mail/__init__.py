"""Mail as the rest of the system understands it: values, identity, parsing.

Nothing here talks to a network or a database. A provider adapter turns its
own JSON into the value objects below and the archive writer turns those into
graph nodes; between the two, this package is the only vocabulary anyone
shares. That is what keeps a Gmail field name from ever reaching the graph.

One module per concern, layered so nothing points back up:

``model``
    Value objects — a message, an address, a page of results, a cursor.
    No I/O, no imports from anything below.
``errors``
    The three failures an import can have and what each one means.
``identity``
    The canonical message id. One mail is one node, however often it arrives.
``parsing``
    RFC 5322 bytes to a ``ParsedMessage``, including the five fields the
    analyses read.
``config``
    ``MailConfig`` — the parser's knobs, and only those.
``ports``
    ``MailSourcePort`` — the single seam a second implementation goes behind.
``rendering``
    The same bytes as a ``RenderedMessage`` — what a mail client shows,
    HTML body included.
"""

from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_core.mail.identity import canonical_id, normalise_message_id
from mailarc_core.mail.model import (
    AccountIdentity,
    CredentialField,
    EmailAddress,
    LabelInfo,
    LabelKind,
    MailProvider,
    MessagePage,
    MessageRef,
    ParsedAttachment,
    ParsedMessage,
    RenderedMessage,
    ProviderDescriptor,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_core.mail.parsing import (
    clean_body,
    extract_refs,
    hamming_distance,
    normalise_subject,
    parse_message,
    participant_key,
    simhash,
)
from mailarc_core.mail.ports import MailSourceFactory, MailSourcePort
from mailarc_core.mail.rendering import (
    count_remote_references,
    inline_cid_images,
    render_message,
)

__all__ = [
    "AccountIdentity",
    "CredentialField",
    "EmailAddress",
    "LabelInfo",
    "LabelKind",
    "MailAuthError",
    "MailConfig",
    "MailError",
    "MailPermanentError",
    "MailProvider",
    "MailSourceFactory",
    "MailSourcePort",
    "MailTransientError",
    "MessagePage",
    "MessageRef",
    "ParsedAttachment",
    "ParsedMessage",
    "ProviderDescriptor",
    "RawMessage",
    "RenderedMessage",
    "SyncCursor",
    "SyncCursorKind",
    "canonical_id",
    "clean_body",
    "count_remote_references",
    "extract_refs",
    "hamming_distance",
    "inline_cid_images",
    "normalise_message_id",
    "normalise_subject",
    "parse_message",
    "participant_key",
    "render_message",
    "simhash",
]
