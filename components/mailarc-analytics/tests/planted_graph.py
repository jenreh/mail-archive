"""The planted corpus as an archive — how it gets into a graph, and what is in it.

Separate from ``conftest.py`` on purpose. A test module cannot reliably
``import conftest``: the name is pytest's convention rather than a package path,
so in a repository-wide run the first ``conftest.py`` imported claims it and a
second component's helpers are simply not there. Anything a test needs to call
by name therefore lives in a module with a name of its own.

Separate from ``corpus.py`` for the other reason: that module plants RFC 5322
bytes and must stay importable by the pure tests without a graph, a server or a
writer anywhere near it. This is the half that needs all three.
"""

from collections.abc import Sequence

from corpus import ACCOUNT_ID, OWN, PlantedMessage
from runic.ogm import Session

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider
from mailarc_core.mail.parsing import parse_message

LABELLED = "p1"
"""The one message that wears a provider label.

One is enough and none is not: a rebuild has to leave ``Label`` nodes alone,
and a count of zero would be satisfied by a rebuild that deleted them.
"""


def source_for(planted: PlantedMessage) -> ArchiveSource:
    """Where this message would have come from, had a provider handed it over.

    One account for the whole corpus, so ``Account.address`` is
    :data:`~corpus.OWN` and the direction split has something to ask. The
    provider's thread id is only set where the corpus planted a real
    conversation; everywhere else the writer falls back to the ``References``
    header, which is what IMAP has instead of threads.
    """
    return ArchiveSource(
        account_id=ACCOUNT_ID,
        account_address=OWN,
        provider=MailProvider.GMAIL,
        provider_message_id=f"g-{planted.key}",
        provider_thread_id=planted.thread,
        folder="INBOX",
        labels=(
            (
                LabelInfo(
                    provider_label_id="Label_1", name="Kunden", kind=LabelKind.USER
                ),
            )
            if planted.key == LABELLED
            else ()
        ),
    )


def archive(config: GraphConfig, messages: Sequence[PlantedMessage]) -> None:
    """Write these messages into the graph, one session for the lot."""
    archiver = MessageArchiver(ArchiveConfig())
    with client.session(config) as graph:
        for planted in messages:
            archiver.archive(graph, parse_message(planted.raw), source_for(planted))


def archive_refingerprinted(
    config: GraphConfig,
    messages: Sequence[PlantedMessage],
    fingerprints: Sequence[int],
) -> None:
    """Archive these messages with somebody else's SimHash on them.

    The one thing the corpus cannot plant. A family whose stored fingerprints
    *straddle* the sign is what actually breaks when the reader hands a signed
    value on — a family that is entirely negative comes out right anyway,
    because ``(negative ^ negative)`` is positive and every band key is
    reconverted downstream. Thirteen hundred markers through the real body were
    tried and every one of them set bit 63, so the straddle has to be made
    rather than found.

    Everything else stays real: the bytes are parsed by the real parser and the
    node is written by the real :class:`~mailarc_core.archive.writer.MessageArchiver`,
    so :func:`~mailarc_core.archive.model.to_signed_64` still decides what the
    graph holds.
    """
    archiver = MessageArchiver(ArchiveConfig())
    with client.session(config) as graph:
        for planted, fingerprint in zip(messages, fingerprints, strict=True):
            parsed = parse_message(planted.raw).model_copy(
                update={"simhash": fingerprint}
            )
            archiver.archive(graph, parsed, source_for(planted))


def ground_truth(session: Session) -> dict[str, int]:
    """Every ground-truth label and edge type, counted.

    The baseline a rebuild is measured against. Written out label by label
    rather than as one ``MATCH (n)`` so that a derived node appearing where it
    should not is a new key rather than a bigger number.
    """
    labels = ("Message", "Address", "Thread", "Label", "Attachment", "Account")
    edges = (
        "SENT_FROM", "SENT_TO", "COPIED_TO", "BLIND_COPIED_TO",
        "IN_THREAD", "REPLIES_TO", "LABELED", "HAS_ATTACHMENT", "ARCHIVED_FROM",
    )  # fmt: skip
    counted = {
        name: int(session.execute(f"MATCH (n:{name}) RETURN count(n)").rows[0][0])
        for name in labels
    }
    counted |= {
        name: int(
            session.execute(f"MATCH ()-[r:{name}]->() RETURN count(r)").rows[0][0]
        )
        for name in edges
    }
    return counted
