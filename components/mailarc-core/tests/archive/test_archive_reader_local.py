"""The listing, proved against a real FalkorDB.

`test_archive_reader.py` proves the projection with canned rows; this file is
what makes the listing a claim about a graph — the rows come out of the same
server :class:`MessageArchiver` wrote them into, through the same runic
traversal the page will run. The server fixtures live in ``conftest.py``.
"""

from datetime import UTC, datetime
from functools import partial

import pytest

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource, BlobKind
from mailarc_core.archive.reader import ArchiveReader
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local


def eml(
    number: int, day: int, *, sender: str = "Anna Bauer <anna@example.com>"
) -> bytes:
    return (
        f"From: {sender}\r\n"
        "To: Bob Baker <bob@example.com>\r\n"
        f"Subject: Angebot {number}\r\n"
        f"Date: Wed, {day:02d} Mar 2026 09:15:00 +0000\r\n"
        f"Message-ID: <m{number}@example.com>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"Hallo Bob,\r\n\r\nanbei das Angebot Nummer {number}.\r\n"
    ).encode()


def source(number: int, *labels: LabelInfo) -> ArchiveSource:
    return ArchiveSource(
        account_id="7",
        account_address="anna@example.com",
        provider=MailProvider.GMAIL,
        provider_message_id=f"g-{number}",
        provider_thread_id="t-1",
        labels=labels,
    )


def label(name: str, kind: LabelKind = LabelKind.USER) -> LabelInfo:
    return LabelInfo(provider_label_id=f"Label_{name}", name=name, kind=kind)


@pytest.fixture
def blobs(tmp_path) -> BlobStore:
    return BlobStore(ArchiveConfig(store_dir=tmp_path / "blobs"))


@pytest.fixture
def reader(config: GraphConfig, blobs: BlobStore) -> ArchiveReader:
    return ArchiveReader(partial(client.session, config), blobs)


def archive(
    config: GraphConfig,
    blobs: BlobStore,
    *messages: bytes,
    labels: dict[int, tuple[LabelInfo, ...]] | None = None,
) -> None:
    """Write the way the import does: bytes to the store, the parse to the graph."""
    archiver = MessageArchiver(ArchiveConfig())
    with client.session(config) as graph:
        for number, raw in enumerate(messages, start=1):
            blobs.put(raw, BlobKind.MESSAGE)
            wearing = (labels or {}).get(number, ())
            archiver.archive(graph, parse_message(raw), source(number, *wearing))


def test_an_empty_graph_lists_nothing(reader) -> None:
    assert reader.list_messages() == []
    assert reader.count_messages() == 0


def test_the_newest_message_comes_first_with_its_sender(config, blobs, reader) -> None:
    archive(config, blobs, eml(1, day=4), eml(2, day=6), eml(3, day=5))

    rows = reader.list_messages()

    assert [row.subject for row in rows] == ["Angebot 2", "Angebot 3", "Angebot 1"]
    assert rows[0].sender_name == "Anna Bauer"
    assert rows[0].sender_address == "anna@example.com"
    assert rows[0].sent_at == datetime(2026, 3, 6, 9, 15, tzinfo=UTC)
    assert rows[0].preview == "Hallo Bob, anbei das Angebot Nummer 2."
    assert rows[0].has_attachments is False
    assert reader.count_messages() == 3


def test_each_row_carries_the_labels_its_message_wears(config, blobs, reader) -> None:
    archive(
        config,
        blobs,
        eml(1, day=4),
        eml(2, day=6),
        eml(3, day=5),
        labels={
            2: (label("INBOX", LabelKind.SYSTEM), label("Kunden/Bauer")),
            3: (label("Kunden/Bauer"),),
        },
    )

    newest, middle, oldest = reader.list_messages()

    assert [(one.name, one.kind) for one in newest.labels] == [
        ("Kunden/Bauer", LabelKind.USER),
        ("INBOX", LabelKind.SYSTEM),
    ]
    assert [one.name for one in middle.labels] == ["Kunden/Bauer"]
    assert oldest.labels == ()


def test_limit_and_offset_page_through_the_listing(config, blobs, reader) -> None:
    archive(config, blobs, eml(1, day=4), eml(2, day=6), eml(3, day=5))

    first = reader.list_messages(limit=2)
    rest = reader.list_messages(limit=2, offset=2)

    assert [row.subject for row in first] == ["Angebot 2", "Angebot 3"]
    assert [row.subject for row in rest] == ["Angebot 1"]


def test_the_digest_on_the_row_opens_the_original(config, blobs, reader) -> None:
    raw = eml(1, day=4)
    archive(config, blobs, raw)

    [row] = reader.list_messages()

    assert row.eml_sha256 is not None
    assert reader.raw_message(row.eml_sha256) == raw


def test_a_message_node_without_an_id_is_not_listed_or_counted(
    config, blobs, reader
) -> None:
    """Nothing the writer makes, but a graph that has been around can hold one
    — the project's own Hello-World smoke test left two — and a listing that
    decodes it trips over the missing key."""
    archive(config, blobs, eml(1, day=4))
    with client.session(config) as graph:
        # Raw Cypher on purpose: the OGM refuses to build a node without its
        # key, which is exactly why such a node can only come from outside.
        graph.execute("CREATE (:Message {subject: 'Hello'})")
        assert graph.execute("MATCH (m:Message) RETURN count(m)").rows[0][0] == 2

    assert [row.subject for row in reader.list_messages()] == ["Angebot 1"]
    assert reader.count_messages() == 1


def test_trusting_a_sender_sticks_and_a_reimport_keeps_it(
    config, blobs, reader
) -> None:
    """The decision lives on the Address node: it survives a new session, and
    archiving the same sender again must not blank it — the writer leaves
    existing nodes alone."""
    archive(config, blobs, eml(1, day=4))

    assert reader.remote_content_trusted("anna@example.com") is False
    assert reader.trust_remote_content("anna@example.com") is True
    assert reader.remote_content_trusted("anna@example.com") is True
    # The key is the normalised form, however the caller writes it.
    assert reader.remote_content_trusted(" Anna@Example.COM ") is True

    archive(config, blobs, eml(2, day=5))

    assert reader.remote_content_trusted("anna@example.com") is True


def test_an_address_the_archive_never_saw_cannot_be_trusted(reader) -> None:
    assert reader.trust_remote_content("stranger@example.com") is False
    assert reader.remote_content_trusted("stranger@example.com") is False
    assert reader.trust_remote_content("") is False


def unthreaded(number: int) -> ArchiveSource:
    """A copy from a provider that hands out no thread ids — an IMAP mailbox."""
    return ArchiveSource(
        account_id="7",
        account_address="anna@example.com",
        provider=MailProvider.IMAP,
        provider_message_id=f"i-{number}",
        provider_thread_id=None,
    )


def reply(number: int, day: int, *, to: int) -> bytes:
    """An answer, carrying the headers a References-threaded client writes."""
    return (
        "From: Bob Baker <bob@example.com>\r\n"
        "To: Anna Bauer <anna@example.com>\r\n"
        f"Subject: Re: Angebot {to}\r\n"
        f"Date: Wed, {day:02d} Mar 2026 09:15:00 +0000\r\n"
        f"Message-ID: <m{number}@example.com>\r\n"
        f"In-Reply-To: <m{to}@example.com>\r\n"
        f"References: <m{to}@example.com>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Passt, danke.\r\n"
    ).encode()


class TestGroupingByConversation:
    def test_the_members_of_one_provider_thread_share_a_conversation(
        self, config, blobs, reader
    ) -> None:
        archive(config, blobs, eml(1, day=4), eml(2, day=6), eml(3, day=5))
        ids = [row.id for row in reader.list_messages()]

        found = reader.conversations_of(ids)

        assert {one.id for one in found.values()} == {"7:t-1"}
        assert {one.total for one in found.values()} == {3}

    def test_a_message_outside_every_thread_is_absent(
        self, config, blobs, reader
    ) -> None:
        """A message with no Message-ID and no thread id names no conversation."""
        archiver = MessageArchiver(ArchiveConfig())
        raw = eml(1, day=4).replace(b"Message-ID: <m1@example.com>\r\n", b"")
        blobs.put(raw, BlobKind.MESSAGE)
        with client.session(config) as graph:
            archiver.archive(graph, parse_message(raw), unthreaded(1))
        [row] = reader.list_messages()

        assert reader.conversations_of([row.id]) == {}

    def test_an_imap_root_and_its_reply_are_one_conversation(
        self, config, blobs, reader
    ) -> None:
        """What the writer fix bought: no provider thread id anywhere in sight."""
        archiver = MessageArchiver(ArchiveConfig())
        with client.session(config) as graph:
            for number, raw in ((1, eml(1, day=4)), (2, reply(2, day=5, to=1))):
                blobs.put(raw, BlobKind.MESSAGE)
                archiver.archive(graph, parse_message(raw), unthreaded(number))
        ids = [row.id for row in reader.list_messages()]

        found = reader.conversations_of(ids)

        assert {one.id for one in found.values()} == {"7:m1@example.com"}
        assert {one.total for one in found.values()} == {2}

    def test_the_total_counts_the_archive_and_not_the_page(
        self, config, blobs, reader
    ) -> None:
        archive(config, blobs, eml(1, day=4), eml(2, day=6), eml(3, day=5))
        [newest] = reader.list_messages(limit=1)

        found = reader.conversations_of([newest.id])

        assert found[newest.id].total == 3


class TestGroupingByRecipient:
    def test_each_message_is_filed_under_the_address_it_went_to(
        self, config, blobs, reader
    ) -> None:
        archive(config, blobs, eml(1, day=4), eml(2, day=6))
        ids = [row.id for row in reader.list_messages()]

        found = reader.recipients_of(ids)

        assert set(found) == set(ids)
        assert {one.address for one in found.values()} == {"bob@example.com"}
        assert {one.name for one in found.values()} == {"Bob Baker"}

    def test_a_copied_address_does_not_count_as_a_receiver(
        self, config, blobs, reader
    ) -> None:
        raw = eml(1, day=4).replace(
            b"To: Bob Baker <bob@example.com>\r\n",
            b"To: Zoe Zed <zoe@example.com>\r\nCc: Al <al@example.com>\r\n",
        )
        archive(config, blobs, raw)
        [row] = reader.list_messages()

        found = reader.recipients_of([row.id])

        assert found[row.id].address == "zoe@example.com"

    def test_the_summary_carries_the_normalised_subject(
        self, config, blobs, reader
    ) -> None:
        archive(config, blobs, reply(2, day=5, to=1))
        [row] = reader.list_messages()

        assert row.subject == "Re: Angebot 1"
        assert row.subject_norm == "angebot 1"


class TestOneWholeConversation:
    def test_the_whole_thread_comes_back_from_one_member(
        self, config, blobs, reader
    ) -> None:
        archive(config, blobs, eml(1, day=4), eml(2, day=6), eml(3, day=5))

        members = reader.conversation_messages("7:t-1")

        assert [one.subject for one in members] == [
            "Angebot 2",
            "Angebot 3",
            "Angebot 1",
        ]

    def test_the_members_carry_their_labels(self, config, blobs, reader) -> None:
        archive(
            config,
            blobs,
            eml(1, day=4),
            eml(2, day=6),
            labels={2: (label("Kunden/Bauer"),)},
        )

        newest, _ = reader.conversation_messages("7:t-1")

        assert [one.name for one in newest.labels] == ["Kunden/Bauer"]

    def test_the_limit_cuts_the_conversation_newest_first(
        self, config, blobs, reader
    ) -> None:
        archive(config, blobs, eml(1, day=4), eml(2, day=6), eml(3, day=5))

        members = reader.conversation_messages("7:t-1", limit=2)

        assert [one.subject for one in members] == ["Angebot 2", "Angebot 3"]

    def test_a_conversation_the_graph_does_not_hold_is_empty(self, reader) -> None:
        assert reader.conversation_messages("7:nothing") == []
