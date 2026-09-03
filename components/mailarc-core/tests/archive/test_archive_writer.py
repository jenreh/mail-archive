"""The mapping from a parsed message to the graph, without a graph.

`FakeSession` implements the handful of `runic.ogm.Session` members the writer
could reach for and records every call, the way `tests/graph/test_client.py`
drives the client with a bare `GraphDriver`. `commit` is there so the test that
says the writer never calls it has something to observe. Two behaviours are
modelled rather than stubbed, because the claims below are claims about them:
`flush` is what makes an added node findable by `get`, and `relate` is
deduplicated because a real `MERGE` is.

The idempotence contract is proven twice on purpose. Here, so a checkout with
no FalkorDB still catches a regression, and in `test_archive_writer_local.py`
against a real server, where the node and edge counts come from the graph
itself rather than from a class in this file.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from runic.ogm import Vector

from mailarc_core.archive import writer
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import (
    Account,
    Address,
    ArchiveSource,
    Attachment,
    Label,
    Message,
    Thread,
)
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.mail.model import (
    EmailAddress,
    LabelInfo,
    LabelKind,
    MailProvider,
    ParsedAttachment,
    ParsedMessage,
)

SENT_AT = datetime(2026, 3, 4, 9, 15, tzinfo=UTC)


class FakeSession:
    """A `runic.ogm.Session` stand-in that writes nothing and remembers all."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[type, str], Any] = {}
        self.added: list[Any] = []
        self.relate_calls: list[tuple[str, str, str, object | None]] = []
        self.events: list[str] = []
        self._pending: list[Any] = []

    def get(self, cls: type, pk: str) -> object | None:
        return self.nodes.get((cls, pk))

    def add(self, entity: Any) -> None:
        self.added.append(entity)
        self._pending.append(entity)
        self.events.append(f"add:{type(entity).__name__}")

    def flush(self) -> None:
        for entity in self._pending:
            self.nodes[(type(entity), entity.id)] = entity
        self._pending.clear()
        self.events.append("flush")

    def commit(self) -> None:
        self.events.append("commit")

    def relate(self, source, field, target, edge=None) -> None:
        self.relate_calls.append((field.relationship, source.id, target.id, edge))
        self.events.append(f"relate:{field.relationship}")

    @property
    def edges(self) -> set[tuple[str, str, str]]:
        """What the graph would hold: a repeated MERGE is not a second edge."""
        return {(rel, src, tgt) for rel, src, tgt, _ in self.relate_calls}

    def node(self, cls: type, pk: str) -> Any:
        """The stored node, or a readable failure if the writer never made it."""
        found = self.nodes.get((cls, pk))
        assert found is not None, f"no {cls.__name__} {pk!r} in {list(self.nodes)}"
        return found

    def edge(self, relationship: str) -> Any:
        """The single edge payload written for this relationship type."""
        payloads = [e for rel, _, _, e in self.relate_calls if rel == relationship]
        assert len(payloads) == 1, f"{relationship}: {len(payloads)} edges"
        return payloads[0]


def mail(**overrides: Any) -> ParsedMessage:
    """A parsed message with every analysis-bearing field already filled in."""
    fields: dict[str, Any] = {
        "canonical_id": "m1@example.com",
        "rfc_message_id": "m1@example.com",
        "subject": "Angebot Q3",
        "subject_norm": "angebot q3",
        "sent_at": SENT_AT,
        "sender": EmailAddress(address="anna@example.com", display_name="Anna"),
        "to": (EmailAddress(address="bob@example.com"),),
        "body_text": "the whole body",
        "body_clean": "the whole body",
        "simhash": 42,
        "participant_key": "pk-1",
        "refs": ("PROJ-123",),
        "size_bytes": 512,
        "eml_sha256": "deadbeef",
    }
    return ParsedMessage(**{**fields, **overrides})


def origin(**overrides: Any) -> ArchiveSource:
    """Where a copy came from — one Gmail account, one message, one thread."""
    fields: dict[str, Any] = {
        "account_id": "7",
        "account_address": "anna@example.com",
        "provider": MailProvider.GMAIL,
        "provider_message_id": "g-1",
        "provider_thread_id": "t-1",
    }
    return ArchiveSource(**{**fields, **overrides})


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def archiver() -> MessageArchiver:
    return MessageArchiver(ArchiveConfig())


class TestTheMessageNode:
    def test_every_analysis_bearing_field_reaches_the_node(
        self, archiver, session
    ) -> None:
        result = archiver.archive(session, mail(), origin())

        node = session.node(Message, "m1@example.com")
        assert result.canonical_id == "m1@example.com"
        assert result.created is True
        assert node.subject_norm == "angebot q3"
        assert node.participant_key == "pk-1"
        assert node.simhash == 42
        assert node.refs == ["PROJ-123"]
        assert node.body_clean == "the whole body"
        assert node.sent_at == SENT_AT

    def test_the_body_is_cut_to_the_configured_limit(self, session) -> None:
        """The graph indexes an excerpt; the whole message is in the blob store."""
        archiver = MessageArchiver(ArchiveConfig(body_text_limit=5))

        archiver.archive(session, mail(body_text="0123456789"), origin())

        assert session.node(Message, "m1@example.com").body_text == "01234"

    def test_the_body_is_not_cut_when_it_fits(self, archiver, session) -> None:
        archiver.archive(session, mail(), origin())

        assert session.node(Message, "m1@example.com").body_text == "the whole body"

    def test_a_simhash_with_the_top_bit_set_is_stored_signed(
        self, archiver, session
    ) -> None:
        """Half of all messages produce a value no graph integer can hold."""
        archiver.archive(session, mail(simhash=2**64 - 1), origin())

        assert session.node(Message, "m1@example.com").simhash == -1

    def test_the_embedding_is_left_for_the_semantic_phase(
        self, archiver, session
    ) -> None:
        archiver.archive(session, mail(), origin())

        node = session.node(Message, "m1@example.com")
        assert node.embedding is None
        assert node.embedding_model is None


class TestAddresses:
    def test_a_message_with_no_readable_sender_still_archives(
        self, archiver, session
    ) -> None:
        """Mail in the wild is odd; a missing `From` is not a reason to stop."""
        archiver.archive(session, mail(sender=None), origin())

        assert session.node(Message, "m1@example.com") is not None
        assert not any(rel == "SENT_FROM" for rel, _, _ in session.edges)

    def test_each_recipient_role_gets_its_own_edge_type(
        self, archiver, session
    ) -> None:
        """RFC 5322 closes the set, so no edge property has to be filtered on."""
        archiver.archive(
            session,
            mail(
                to=(EmailAddress(address="bob@example.com"),),
                cc=(EmailAddress(address="carl@example.com"),),
                bcc=(EmailAddress(address="dora@example.com"),),
            ),
            origin(),
        )

        assert ("SENT_FROM", "m1@example.com", "anna@example.com") in session.edges
        assert ("SENT_TO", "m1@example.com", "bob@example.com") in session.edges
        assert ("COPIED_TO", "m1@example.com", "carl@example.com") in session.edges
        assert (
            "BLIND_COPIED_TO",
            "m1@example.com",
            "dora@example.com",
        ) in session.edges

    def test_the_address_node_carries_its_parts(self, archiver, session) -> None:
        archiver.archive(session, mail(), origin())

        node = session.node(Address, "anna@example.com")
        assert node.local_part == "anna"
        assert node.domain == "example.com"
        assert node.display_names == ["Anna"]

    def test_one_address_in_two_headers_is_one_node(self, archiver, session) -> None:
        """`get` cannot see a node added moments ago, so the writer remembers it."""
        archiver.archive(
            session,
            mail(
                to=(EmailAddress(address="bob@example.com"),),
                cc=(EmailAddress(address="bob@example.com"),),
            ),
            origin(),
        )

        bobs = [
            n
            for n in session.added
            if isinstance(n, Address) and n.id.startswith("bob")
        ]
        assert len(bobs) == 1
        assert ("SENT_TO", "m1@example.com", "bob@example.com") in session.edges
        assert ("COPIED_TO", "m1@example.com", "bob@example.com") in session.edges

    def test_a_name_seen_later_is_collected_on_the_existing_node(
        self, archiver, session
    ) -> None:
        """The same address signs itself differently; the node keeps both."""
        archiver.archive(session, mail(), origin())
        archiver.archive(
            session,
            mail(
                canonical_id="m2@example.com",
                rfc_message_id="m2@example.com",
                sender=EmailAddress(address="anna@example.com", display_name="A. Bau"),
            ),
            origin(provider_message_id="g-2"),
        )

        assert session.node(Address, "anna@example.com").display_names == [
            "Anna",
            "A. Bau",
        ]

    def test_a_name_already_recorded_is_not_recorded_twice(
        self, archiver, session
    ) -> None:
        archiver.archive(session, mail(), origin())
        archiver.archive(session, mail(), origin())

        assert session.node(Address, "anna@example.com").display_names == ["Anna"]


class TestThread:
    def test_the_thread_is_scoped_to_the_account(self, archiver, session) -> None:
        """Two providers hand out thread ids from their own namespaces."""
        archiver.archive(session, mail(), origin())

        assert session.node(Thread, "7:t-1").subject == "Angebot Q3"
        assert ("IN_THREAD", "m1@example.com", "7:t-1") in session.edges

    def test_a_provider_without_threads_falls_back_to_the_header_hint(
        self, archiver, session
    ) -> None:
        """What IMAP has instead of a thread id: the root of `References`."""
        archiver.archive(
            session,
            mail(thread_hint="root@example.com"),
            origin(provider_thread_id=None),
        )

        assert ("IN_THREAD", "m1@example.com", "7:root@example.com") in session.edges

    def test_a_message_naming_no_conversation_opens_one_of_its_own(
        self, archiver, session
    ) -> None:
        """Keyed on its own Message-ID, which is what its replies will name."""
        archiver.archive(session, mail(), origin(provider_thread_id=None))

        assert ("IN_THREAD", "m1@example.com", "7:m1@example.com") in session.edges

    def test_an_imap_root_and_its_reply_land_on_one_thread(
        self, archiver, session
    ) -> None:
        """The whole point: IMAP hands out no thread ids, so the headers do.

        The root has neither ``References`` nor ``In-Reply-To``, and used to
        get no thread while its own reply grouped without it.
        """
        archiver.archive(session, mail(), origin(provider_thread_id=None))
        archiver.archive(
            session,
            mail(
                canonical_id="m2@example.com",
                rfc_message_id="m2@example.com",
                in_reply_to="m1@example.com",
                thread_hint="m1@example.com",
            ),
            origin(provider_message_id="i-2", provider_thread_id=None),
        )

        assert ("IN_THREAD", "m1@example.com", "7:m1@example.com") in session.edges
        assert ("IN_THREAD", "m2@example.com", "7:m1@example.com") in session.edges

    def test_a_message_without_a_message_id_still_gets_no_thread(
        self, archiver, session
    ) -> None:
        """Its canonical id is a digest of the bytes; no reply can name one."""
        archiver.archive(
            session,
            mail(canonical_id="sha256:abc", rfc_message_id=None),
            origin(provider_thread_id=None),
        )

        assert not any(isinstance(node, Thread) for node in session.added)
        assert not any(rel == "IN_THREAD" for rel, _, _ in session.edges)


class TestReplies:
    def test_a_reply_is_linked_once_its_parent_is_archived(
        self, archiver, session
    ) -> None:
        archiver.archive(session, mail(), origin())

        archiver.archive(
            session,
            mail(
                canonical_id="m2@example.com",
                rfc_message_id="m2@example.com",
                in_reply_to="m1@example.com",
            ),
            origin(provider_message_id="g-2"),
        )

        assert ("REPLIES_TO", "m2@example.com", "m1@example.com") in session.edges

    def test_a_reply_whose_parent_is_missing_gets_no_edge_and_no_stub(
        self, archiver, session
    ) -> None:
        """A node holding only an id would poison every count in the graph."""
        archiver.archive(
            session,
            mail(
                canonical_id="m2@example.com",
                rfc_message_id="m2@example.com",
                in_reply_to="never-seen@example.com",
            ),
            origin(),
        )

        assert not any(rel == "REPLIES_TO" for rel, _, _ in session.edges)
        assert (Message, "never-seen@example.com") not in session.nodes


class TestLabels:
    def test_a_label_is_scoped_to_the_account_that_has_it(
        self, archiver, session
    ) -> None:
        archiver.archive(
            session,
            mail(),
            origin(
                labels=(
                    LabelInfo(provider_label_id="INBOX", name="INBOX"),
                    LabelInfo(
                        provider_label_id="Label_9",
                        name="Kunden",
                        kind=LabelKind.USER,
                    ),
                )
            ),
        )

        assert session.node(Label, "7:INBOX").name == "INBOX"
        assert session.node(Label, "7:Kunden").kind is LabelKind.USER
        assert ("LABELED", "m1@example.com", "7:Kunden") in session.edges


class TestAttachments:
    def test_the_filename_hangs_on_the_edge_not_on_the_node(
        self, archiver, session
    ) -> None:
        """Content-addressed: the sender renamed it, the bytes did not change."""
        archiver.archive(
            session,
            mail(
                has_attachments=True,
                attachments=(
                    ParsedAttachment(
                        filename="Angebot.pdf",
                        content_type="application/pdf",
                        size=1024,
                        sha256="sha-of-the-pdf",
                        content_id="cid-1",
                        inline=True,
                    ),
                ),
            ),
            origin(),
        )

        node = session.node(Attachment, "sha-of-the-pdf")
        assert node.content_type == "application/pdf"
        assert node.size == 1024
        assert not hasattr(node, "filename")

        edge = session.edge("HAS_ATTACHMENT")
        assert edge.filename == "Angebot.pdf"
        assert edge.content_id == "cid-1"
        assert edge.inline is True

    def test_the_same_file_on_two_messages_is_one_node(self, archiver, session) -> None:
        attachment = ParsedAttachment(
            filename="Angebot.pdf", size=1024, sha256="sha-of-the-pdf"
        )

        archiver.archive(
            session, mail(has_attachments=True, attachments=(attachment,)), origin()
        )
        archiver.archive(
            session,
            mail(
                canonical_id="m2@example.com",
                rfc_message_id="m2@example.com",
                has_attachments=True,
                attachments=(attachment.model_copy(update={"filename": "offer.pdf"}),),
            ),
            origin(provider_message_id="g-2"),
        )

        nodes = [n for n in session.added if isinstance(n, Attachment)]
        assert len(nodes) == 1
        assert len([rel for rel, _, _ in session.edges if rel == "HAS_ATTACHMENT"]) == 2

    def test_an_attachment_the_parser_could_not_digest_is_skipped(
        self, archiver, session
    ) -> None:
        """An empty sha256 is not a key; a node under `""` would collect junk."""
        archiver.archive(
            session,
            mail(
                has_attachments=True,
                attachments=(ParsedAttachment(filename="broken.bin"),),
            ),
            origin(),
        )

        assert not any(isinstance(node, Attachment) for node in session.added)


class TestProvenance:
    def test_the_provider_ids_hang_on_the_archived_from_edge(
        self, archiver, session
    ) -> None:
        archiver.archive(session, mail(), origin(folder="INBOX/2026", uid="4711"))

        assert session.node(Account, "7").address == "anna@example.com"
        assert session.node(Account, "7").provider is MailProvider.GMAIL

        edge = session.edge("ARCHIVED_FROM")
        assert edge.provider_message_id == "g-1"
        assert edge.provider_thread_id == "t-1"
        assert edge.folder == "INBOX/2026"
        assert edge.uid == "4711"

    def test_archived_at_is_stamped_when_the_caller_does_not_say(
        self, archiver, session
    ) -> None:
        before = datetime.now(UTC)

        archiver.archive(session, mail(), origin())

        assert before <= session.edge("ARCHIVED_FROM").archived_at <= datetime.now(UTC)

    def test_a_caller_supplied_time_wins(self, archiver, session) -> None:
        archiver.archive(session, mail(), origin(archived_at=SENT_AT))

        assert session.edge("ARCHIVED_FROM").archived_at == SENT_AT

    def test_the_same_mail_through_two_accounts_is_one_node_with_two_edges(
        self, archiver, session
    ) -> None:
        archiver.archive(session, mail(), origin())

        result = archiver.archive(
            session,
            mail(),
            origin(
                account_id="8",
                account_address="anna@work.example",
                provider=MailProvider.IMAP,
                provider_message_id="imap-9",
            ),
        )

        assert result.created is False
        assert len([n for n in session.added if isinstance(n, Message)]) == 1
        assert ("ARCHIVED_FROM", "m1@example.com", "7") in session.edges
        assert ("ARCHIVED_FROM", "m1@example.com", "8") in session.edges


class TestIdempotence:
    def test_archiving_the_same_message_twice_adds_no_node_and_no_edge(
        self, archiver, session
    ) -> None:
        """The contract: a second import of one mailbox writes nothing new."""
        message = mail(
            cc=(EmailAddress(address="carl@example.com"),),
            has_attachments=True,
            attachments=(ParsedAttachment(filename="a.pdf", sha256="sha-a"),),
        )
        source = origin(labels=(LabelInfo(provider_label_id="INBOX", name="INBOX"),))

        archiver.archive(session, message, source)
        nodes_after_first = len(session.added)
        edges_after_first = set(session.edges)

        result = archiver.archive(session, message, source)

        assert result.created is False
        assert len(session.added) == nodes_after_first
        assert session.edges == edges_after_first

    def test_an_existing_message_node_is_never_rewritten(
        self, archiver, session
    ) -> None:
        """Later phases write to this node; a re-import must not blank them."""
        session.nodes[(Message, "m1@example.com")] = Message(
            id="m1@example.com",
            subject="as it was first parsed",
            embedding=Vector([0.5, 0.25]),
            embedding_model="bge-m3",
        )

        archiver.archive(session, mail(subject="a newer parse"), origin())

        node = session.node(Message, "m1@example.com")
        assert node.subject == "as it was first parsed"
        assert node.embedding_model == "bge-m3"
        assert list(node.embedding) == [0.5, 0.25]


class TestUnitOfWork:
    def test_every_node_is_flushed_before_the_first_edge_is_written(
        self, archiver, session
    ) -> None:
        """An edge is a MERGE over two MATCHes: relate too early writes nothing."""
        archiver.archive(session, mail(), origin())

        flushed = session.events.index("flush")
        first_edge = next(
            i for i, e in enumerate(session.events) if e.startswith("relate:")
        )
        last_node = max(i for i, e in enumerate(session.events) if e.startswith("add:"))
        assert last_node < flushed < first_edge

    def test_the_commit_is_left_to_the_caller(self, archiver, session) -> None:
        """So a batch of messages can land as one unit of work."""
        archiver.archive(session, mail(), origin())

        assert "commit" not in session.events


class TestRelationLookup:
    def test_the_edge_constants_name_the_edge_types_they_are_called_after(
        self,
    ) -> None:
        assert writer.SENT_FROM.relationship == "SENT_FROM"
        assert writer.ARCHIVED_FROM.relationship == "ARCHIVED_FROM"

    def test_a_field_that_is_not_a_relation_is_refused_at_import_time(self) -> None:
        """A renamed relation must break loudly, not write nothing quietly."""
        with pytest.raises(TypeError, match="not a relation"):
            writer._relation("subject")
