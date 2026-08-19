"""Writing a parsed message into the graph — the second time for free.

The contract this module exists for: importing the same mailbox twice creates
no new nodes and no new edges. Three things carry it. The canonical id is the
same for both arrivals of one mail, so the ``Message`` lookup finds the first
one. Every other node is looked up before it is added. And ``Session.relate``
is a ``MERGE``, so an edge that is already there is written again as itself.

An existing ``Message`` is left exactly as it stands. Its properties are
derived from the bytes and a re-parse computes the same values anyway, while
the fields the semantic phase fills in later are not ours to blank out.

Synchronous on purpose: every runic driver blocks, so an async caller wraps a
call in ``asyncio.to_thread`` the way the graph status reader does. The unit of
work stays with the caller — this writer flushes, so the edges it writes can
find their nodes, and never commits, so a batch of messages can share one
commit.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from runic.ogm import FieldDescriptor, Session

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import (
    Account,
    Address,
    ArchivedFrom,
    ArchiveResult,
    ArchiveSource,
    Attachment,
    HasAttachment,
    Label,
    Message,
    Thread,
    to_signed_64,
)
from mailarc_core.mail.model import (
    EmailAddress,
    LabelInfo,
    ParsedAttachment,
    ParsedMessage,
)

logger = logging.getLogger(__name__)


def _relation(name: str) -> FieldDescriptor:
    """The relation descriptor ``Session.relate`` wants, named by its edge type.

    ``Message.labels`` already *is* the descriptor at runtime, but it is
    annotated with the type it yields on an instance, so reading it directly at
    a call site type-checks as a list of labels. Going through here says what
    the value is, and the check makes a renamed field an import-time failure
    rather than a silent one at write time.
    """
    descriptor = getattr(Message, name)
    if not isinstance(descriptor, FieldDescriptor) or not descriptor.relationship:
        raise TypeError(f"Message.{name} is not a relation")
    return descriptor


SENT_FROM = _relation("sender")
SENT_TO = _relation("recipients")
COPIED_TO = _relation("copied_to")
BLIND_COPIED_TO = _relation("blind_copied_to")
IN_THREAD = _relation("thread")
REPLIES_TO = _relation("replies_to")
LABELED = _relation("labels")
HAS_ATTACHMENT = _relation("attachments")
ARCHIVED_FROM = _relation("archived_from")


class MessageArchiver:
    """Turns a parsed message plus its provenance into graph ground truth."""

    def __init__(self, config: ArchiveConfig | None = None) -> None:
        self._config = config or ArchiveConfig()

    def archive(
        self, session: Session, message: ParsedMessage, source: ArchiveSource
    ) -> ArchiveResult:
        """Write one message and everything it points at. Safe to repeat.

        Nodes are resolved first and flushed together, because an edge is a
        ``MERGE`` over two ``MATCH`` clauses: relate a node that has not
        reached the graph yet and the edge is silently not written.
        """
        nodes = _NodeCache(session)
        node, created = nodes.resolve(
            Message, message.canonical_id, lambda: self._message_node(message)
        )
        addresses = _address_edges(nodes, message)
        thread = _thread_node(nodes, message, source)
        labels = [_label_node(nodes, source.account_id, one) for one in source.labels]
        attachments = [
            (_attachment_node(nodes, one), one)
            for one in message.attachments
            if one.sha256
        ]
        account = _account_node(nodes, source)
        parent = _parent_node(session, message)

        session.flush()

        for descriptor, targets in addresses:
            for target in targets:
                session.relate(node, descriptor, target)
        if thread is not None:
            session.relate(node, IN_THREAD, thread)
        if parent is not None:
            session.relate(node, REPLIES_TO, parent)
        for label in labels:
            session.relate(node, LABELED, label)
        for attachment_node, attachment in attachments:
            session.relate(
                node,
                HAS_ATTACHMENT,
                attachment_node,
                edge=_attachment_edge(attachment),
            )
        session.relate(node, ARCHIVED_FROM, account, edge=_provenance(source))

        logger.debug(
            "Archived %s from account %s (created=%s)",
            message.canonical_id,
            source.account_id,
            created,
        )
        return ArchiveResult(canonical_id=message.canonical_id, created=created)

    def _message_node(self, message: ParsedMessage) -> Message:
        """The ``Message`` node, with the body cut to what the graph indexes."""
        return Message(
            id=message.canonical_id,
            rfc_message_id=message.rfc_message_id,
            subject=message.subject,
            subject_norm=message.subject_norm,
            sent_at=message.sent_at,
            body_text=message.body_text[: self._config.body_text_limit],
            body_clean=message.body_clean,
            simhash=to_signed_64(message.simhash),
            participant_key=message.participant_key,
            refs=list(message.refs),
            size_bytes=message.size_bytes,
            has_attachments=message.has_attachments,
            eml_sha256=message.eml_sha256,
        )


class _NodeCache:
    """Get-or-create against one session, deduplicated within one message.

    ``Session.get`` sees an added entity only after the flush, and a message
    routinely carries the same address in ``To`` and in ``Cc``. Without this
    map the second lookup would miss and create a duplicate node.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._seen: dict[tuple[type, str], Any] = {}

    def resolve[T](
        self, cls: type[T], pk: str, build: Callable[[], T]
    ) -> tuple[T, bool]:
        """The node with this key, and whether this call is what created it."""
        key = (cls, pk)
        cached = self._seen.get(key)
        if cached is not None:
            return cached, False

        existing = self._session.get(cls, pk)
        if existing is not None:
            self._seen[key] = existing
            return existing, False

        node = build()
        self._session.add(node)
        self._seen[key] = node
        return node, True


def _address_edges(
    nodes: _NodeCache, message: ParsedMessage
) -> tuple[tuple[FieldDescriptor, list[Address]], ...]:
    """Every participant, grouped by the edge type that carries their role."""
    sender = _address_node(nodes, message.sender)
    return (
        (SENT_FROM, [sender] if sender is not None else []),
        (SENT_TO, _address_nodes(nodes, message.to)),
        (COPIED_TO, _address_nodes(nodes, message.cc)),
        (BLIND_COPIED_TO, _address_nodes(nodes, message.bcc)),
    )


def _address_nodes(
    nodes: _NodeCache, addresses: tuple[EmailAddress, ...]
) -> list[Address]:
    """Resolve a header's worth of addresses, keeping the order they came in."""
    resolved = (_address_node(nodes, one) for one in addresses)
    return [node for node in resolved if node is not None]


def _address_node(nodes: _NodeCache, address: EmailAddress | None) -> Address | None:
    """The node for one address, remembering a name we have not seen before."""
    if address is None:
        return None
    node, _ = nodes.resolve(
        Address,
        address.address,
        lambda: Address(
            id=address.address,
            local_part=address.local_part,
            domain=address.domain,
            display_names=[address.display_name] if address.display_name else [],
        ),
    )
    _remember_display_name(node, address.display_name)
    return node


def _remember_display_name(node: Address, display_name: str | None) -> None:
    """Add a signature we have not recorded for this address yet.

    Assigned rather than appended in place: runic tracks dirtiness through the
    descriptor, so a mutated list would never reach the graph. Re-importing the
    same mailbox adds nothing and leaves the node clean.
    """
    if not display_name or display_name in node.display_names:
        return
    node.display_names = [*node.display_names, display_name]


def _thread_node(
    nodes: _NodeCache, message: ParsedMessage, source: ArchiveSource
) -> Thread | None:
    """The conversation this message sits in, if anything names one.

    The provider's own thread id wins. ``thread_hint`` — the root taken out of
    the ``References`` header — is what IMAP has instead of one.
    """
    thread_id = source.provider_thread_id or message.thread_hint
    if not thread_id:
        return None
    key = f"{source.account_id}:{thread_id}"
    node, _ = nodes.resolve(
        Thread, key, lambda: Thread(id=key, subject=message.subject)
    )
    return node


def _parent_node(session: Session, message: ParsedMessage) -> Message | None:
    """The message this one replies to, but only if it is already archived.

    Deliberately no placeholder node: a ``Message`` holding nothing but an id
    would be indistinguishable from a real one and would poison every count and
    every analysis that walks the graph. A reply imported ahead of its parent
    therefore keeps no ``REPLIES_TO`` edge — the headers that would rebuild it
    stay on the message and in the blob store, so the loss is recoverable and a
    fabricated node would not be.
    """
    if not message.in_reply_to:
        return None
    return session.get(Message, message.in_reply_to)


def _label_node(nodes: _NodeCache, account_id: str, label: LabelInfo) -> Label:
    """The node for one provider label, scoped to the account that has it."""
    key = f"{account_id}:{label.name}"
    node, _ = nodes.resolve(
        Label, key, lambda: Label(id=key, name=label.name, kind=label.kind)
    )
    return node


def _attachment_node(nodes: _NodeCache, attachment: ParsedAttachment) -> Attachment:
    """The node for one attachment — keyed by content, never by filename."""
    node, _ = nodes.resolve(
        Attachment,
        attachment.sha256,
        lambda: Attachment(
            id=attachment.sha256,
            content_type=attachment.content_type,
            size=attachment.size,
        ),
    )
    return node


def _account_node(nodes: _NodeCache, source: ArchiveSource) -> Account:
    """The node for the mailbox this copy came out of."""
    node, _ = nodes.resolve(
        Account,
        source.account_id,
        lambda: Account(
            id=source.account_id,
            address=source.account_address,
            provider=source.provider,
        ),
    )
    return node


def _attachment_edge(attachment: ParsedAttachment) -> HasAttachment:
    """What this message called the file it carried."""
    return HasAttachment(
        filename=attachment.filename,
        content_id=attachment.content_id,
        inline=attachment.inline,
    )


def _provenance(source: ArchiveSource) -> ArchivedFrom:
    """Which account this copy came from, under which provider id."""
    return ArchivedFrom(
        provider_message_id=source.provider_message_id,
        provider_thread_id=source.provider_thread_id,
        folder=source.folder,
        uid=source.uid,
        archived_at=source.archived_at or datetime.now(UTC),
    )
