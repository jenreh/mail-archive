"""The graph queries the archive needs on top of runic's collection reads.

The counterpart of :mod:`mailarc_core.database.repositories`, for the graph:
``runic.ogm.Repository`` already brings ``find_all``, ``find_all_by_ids``,
``count`` and ``exists``, and none of that is repeated here. What is here is
the one listing a review of the archive cannot express with a primary key.

Everything goes through the query builder, never through a Cypher string, so
the statement is checked against the mapped models at build time and the
backend stays :attr:`~mailarc_core.graph.config.GraphConfig.backend`'s choice.
"""

import logging

from runic.ogm import FieldDescriptor, Repository, Session, select

from mailarc_core.archive.model import Address, Label, Message

logger = logging.getLogger(__name__)


def _field(name: str) -> FieldDescriptor:
    """The descriptor the query builder wants, looked up by name.

    ``Message.sender`` *is* the descriptor at runtime but is annotated with
    the value it yields on an instance, so passing it straight to ``traverse``
    or ``order_by`` does not type-check. The same detour the writer takes, and
    for the same reason: a renamed field fails at import, not at query time.
    """
    descriptor = getattr(Message, name)
    if not isinstance(descriptor, FieldDescriptor):
        raise TypeError(f"Message.{name} is not a mapped field")
    return descriptor


ID = _field("id")
SENDER = _field("sender")
LABELS = _field("labels")
SENT_AT = _field("sent_at")


class MessageRepository(Repository[Message]):
    """Messages, the way a listing wants them: newest first, sender attached.

    Both reads keep to nodes that carry a canonical id. A ``Message`` without
    one is not something the writer produces, but a graph that has been
    around — a smoke test, an older schema — can hold such a node, and a
    listing that trips over it would take the whole page down with it.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Message)

    def count(self) -> int:
        """How many archived messages there are — the listing's total."""
        return self._session.count(select(Message).where(ID.is_not_null()))

    def find_recent(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[tuple[Message, Address | None]]:
        """The newest ``limit`` messages after ``offset``, each with its sender.

        One traversal rather than a lazy relation per row: a listing of fifty
        messages is the textbook N+1. The traversal is optional, so a message
        whose ``From`` could not be parsed still lists — with ``None`` beside
        it. Ordered by ``sent_at`` descending; a message without a date sorts
        wherever the backend puts nulls.
        """
        statement = (
            select(Message)
            .alias("m")
            .where(ID.is_not_null())
            .traverse(SENDER, optional=True)
            .alias("s")
            .order_by(SENT_AT, desc=True)
            .skip(offset)
            .limit(limit)
            .return_nodes("m", "s")
        )
        rows = self._session.all_with_edges(statement)
        logger.debug("Listed %d messages from offset %d", len(rows), offset)
        return [(message, sender) for message, sender in rows]

    def find_labels(self, ids: list[str]) -> dict[str, list[Label]]:
        """The labels on each of these messages, keyed by message id.

        A second statement rather than a second traversal on the listing: a
        message wears several labels, and a row per ``(message, label)`` pair
        would make the listing's ``LIMIT`` count labels instead of messages.
        One ``IN`` over the page's ids keeps it at one read per page. A
        message without labels is simply absent from the answer.
        """
        if not ids:
            return {}
        statement = (
            select(Message)
            .alias("m")
            .where(ID.in_(ids))
            .traverse(LABELS, optional=False)
            .alias("l")
            .return_nodes("m", "l")
        )
        found: dict[str, list[Label]] = {}
        for message, label in self._session.all_with_edges(statement):
            found.setdefault(message.id, []).append(label)
        logger.debug("Read labels for %d of %d messages", len(found), len(ids))
        return found


class AddressRepository(Repository[Address]):
    """Addresses, for the one thing a human writes back: remote-content trust."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, Address)

    def is_remote_trusted(self, address: str) -> bool:
        """Whether this sender's remote content may load without asking.

        An address the archive has never seen answers ``False`` — there is
        nothing to hang a decision on, and the safe answer is the default one.
        """
        node = self._session.get(Address, _address_key(address))
        return node is not None and node.remote_trusted

    def trust_remote(self, address: str) -> bool:
        """Record the decision on the node; ``False`` if the node is missing.

        The session's dirty tracking carries the change out; the caller's
        session boundary commits it.
        """
        node = self._session.get(Address, _address_key(address))
        if node is None:
            logger.warning("Cannot trust %s — the archive has no such address", address)
            return False
        node.remote_trusted = True
        logger.info("Remote content allowed for %s from now on", node.id)
        return True


def _address_key(address: str) -> str:
    """The node key form — lowercased, trimmed, as ``EmailAddress`` builds it."""
    return address.strip().lower()
