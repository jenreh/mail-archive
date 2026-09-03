"""The projection from graph rows to summaries, and the blob lookup, without a
graph.

`FakeSession` answers the calls :class:`MessageRepository` and
:class:`ThreadRepository` make — the listing, the count, the labels and the
three conversation reads — with what the test put in, the way
`test_archive_writer.py` drives the writer. What is proved here is the
projection: which node field lands in which summary field, what a missing
sender or body becomes, which of two threads a message is grouped into, and
that a missing blob is a ``None`` and not a traceback.
`test_archive_reader_local.py` proves the reads themselves against a real
FalkorDB.

The statement-shape assertions are not decoration. Two of them guard the
misplacement `mailarc_core.archive.repository._filtered` documents at length —
that a predicate naming a traversed variable is emitted after the last pattern
clause, and lands on an ``OPTIONAL MATCH`` if one is there.
"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import (
    Address,
    BlobKind,
    Conversation,
    Label,
    Message,
    MessageLabel,
    MessageSummary,
    Recipient,
    Thread,
)
from mailarc_core.archive.reader import (
    CONVERSATION_LIMIT,
    PREVIEW_LENGTH,
    ArchiveReader,
    GraphSessionFactory,
    preview_of,
)
from mailarc_core.mail.model import LabelKind

SENT_AT = datetime(2026, 8, 19, 14, 28, tzinfo=UTC)


class FakeSession:
    """A `runic.ogm.Session` stand-in that hands back canned rows.

    Both readers record the statement they were given, so a test can read the
    Cypher the repository built and check that the limit, the offset and the
    root reached it. Four statements, each told apart by its own shape: the
    listing answers with ``rows``, the label lookup — by the edge it walks —
    with ``labels``, the conversation lookup with ``threads``, and the two
    projected reads with ``totals`` (it aggregates) or ``members`` (it does
    not).

    What it records is the compiled Cypher with the compiler's backticks
    dropped. runic quotes every identifier it emits — ``m.`id``` — so that a
    model may declare a field named after a Cypher keyword; that is escaping,
    not shape, and the assertions here are about shape.
    """

    def __init__(
        self,
        rows: list[tuple[Message, Address | Label | None]],
        *,
        labels: list[tuple[Message, Address | Label | None]] | None = None,
        threads: list[tuple[Message, Thread]] | None = None,
        totals: dict[str, int] | None = None,
        members: list[str] | None = None,
        recipients: list[tuple[Message, Address]] | None = None,
    ) -> None:
        self.rows = rows
        self.labels = labels or []
        self.threads = threads or []
        self.recipients = recipients or []
        self.totals = totals or {}
        self.members = members or []
        self.statements: list[str] = []

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    @staticmethod
    def _cypher(statement) -> str:
        cypher, _ = statement.build()
        return cypher.replace("`", "")

    def all_with_edges(self, statement) -> list[tuple[Message, Address | Label | None]]:
        cypher = self._cypher(statement)
        self.statements.append(cypher)
        if "LABELED" in cypher:
            return self.labels
        if "IN_THREAD" in cypher:
            return cast(Any, self.threads)
        if "[:SENT_TO]" in cypher:
            return cast(Any, self.recipients)
        return self.rows

    def all_rows(self, statement) -> list[dict[str, Any]]:
        """The two projected reads, told apart by whether they aggregate."""
        cypher = self._cypher(statement)
        self.statements.append(cypher)
        if "count(" in cypher:
            return [
                {"thread_id": thread, "total": total}
                for thread, total in self.totals.items()
            ]
        return [{"id": one} for one in self.members]

    def count(self, statement) -> int:
        """What ``MessageRepository.count`` runs: a filtered ``select``."""
        self.statements.append(self._cypher(statement))
        return len(self.rows)


def message(**overrides: Any) -> Message:
    fields: dict[str, Any] = {
        "id": "m1@example.com",
        "subject": "SwiftScan 19.08.2026 14.28.pdf",
        "subject_norm": "swiftscan 19.08.2026 14.28.pdf",
        "sent_at": SENT_AT,
        "body_text": "Erstellt mit SwiftScan,\n\nder weltweit   führenden Scanner-App.",
        "body_clean": None,
        "has_attachments": True,
        "eml_sha256": "ab" * 32,
    }
    return Message(**{**fields, **overrides})


def sender(**overrides: Any) -> Address:
    fields: dict[str, Any] = {
        "id": "jens@example.com",
        "display_names": ["Jens Rehpöhler", "J. R."],
    }
    return Address(**{**fields, **overrides})


def label(name: str, kind: LabelKind = LabelKind.USER) -> Label:
    return Label(id=f"7:{name}", name=name, kind=kind)


@pytest.fixture
def blobs(tmp_path) -> BlobStore:
    return BlobStore(ArchiveConfig(store_dir=tmp_path / "blobs"))


def reader(session: FakeSession, blobs: BlobStore) -> ArchiveReader:
    return ArchiveReader(cast(GraphSessionFactory, lambda: session), blobs)


class TestTheListing:
    def test_every_field_a_row_shows_reaches_the_summary(self, blobs) -> None:
        session = FakeSession([(message(), sender())])

        [summary] = reader(session, blobs).list_messages()

        assert summary == MessageSummary(
            id="m1@example.com",
            sender_name="Jens Rehpöhler",
            sender_address="jens@example.com",
            subject="SwiftScan 19.08.2026 14.28.pdf",
            preview="Erstellt mit SwiftScan, der weltweit führenden Scanner-App.",
            sent_at=SENT_AT,
            has_attachments=True,
            eml_sha256="ab" * 32,
            subject_norm="swiftscan 19.08.2026 14.28.pdf",
        )

    def test_a_message_without_a_sender_still_lists(self, blobs) -> None:
        session = FakeSession([(message(), None)])

        [summary] = reader(session, blobs).list_messages()

        assert summary.sender_name == ""
        assert summary.sender_address == ""

    def test_a_sender_without_a_display_name_shows_its_address_only(
        self, blobs
    ) -> None:
        session = FakeSession([(message(), sender(display_names=[]))])

        [summary] = reader(session, blobs).list_messages()

        assert summary.sender_name == ""
        assert summary.sender_address == "jens@example.com"

    def test_the_clean_body_is_preferred_for_the_preview(self, blobs) -> None:
        session = FakeSession([(message(body_clean="just the reply"), sender())])

        [summary] = reader(session, blobs).list_messages()

        assert summary.preview == "just the reply"

    def test_the_labels_on_a_message_reach_its_summary(self, blobs) -> None:
        session = FakeSession(
            [(message(), sender())],
            labels=[
                (message(), label("INBOX", LabelKind.SYSTEM)),
                (message(), label("Projekte/Scanner")),
            ],
        )

        [summary] = reader(session, blobs).list_messages()

        assert summary.labels == (
            MessageLabel(name="Projekte/Scanner", kind=LabelKind.USER),
            MessageLabel(name="INBOX", kind=LabelKind.SYSTEM),
        )

    def test_labels_are_ordered_user_folder_system_then_by_name(self, blobs) -> None:
        """What a human made comes first; the provider's own housekeeping last."""
        session = FakeSession(
            [(message(), sender())],
            labels=[
                (message(), label("UNREAD", LabelKind.SYSTEM)),
                (message(), label("Archiv", LabelKind.FOLDER)),
                (message(), label("zebra")),
                (message(), label("INBOX", LabelKind.SYSTEM)),
                (message(), label("Alpha")),
            ],
        )

        [summary] = reader(session, blobs).list_messages()

        assert [one.name for one in summary.labels] == [
            "Alpha",
            "zebra",
            "Archiv",
            "INBOX",
            "UNREAD",
        ]

    def test_labels_land_on_the_message_that_wears_them(self, blobs) -> None:
        other = message(id="m2@example.com")
        session = FakeSession(
            [(message(), sender()), (other, sender())],
            labels=[(other, label("Rechnungen"))],
        )

        first, second = reader(session, blobs).list_messages()

        assert first.labels == ()
        assert second.labels == (MessageLabel(name="Rechnungen"),)

    def test_a_label_without_a_name_is_skipped(self, blobs) -> None:
        session = FakeSession(
            [(message(), sender())],
            labels=[(message(), Label(id="7:", name=None)), (message(), label("a"))],
        )

        [summary] = reader(session, blobs).list_messages()

        assert [one.name for one in summary.labels] == ["a"]

    def test_the_labels_of_a_page_come_from_one_statement(self, blobs) -> None:
        """The page's ids go into one ``IN``; fifty rows are not fifty reads."""
        session = FakeSession([(message(), sender()), (message(id="m2"), None)])

        reader(session, blobs).list_messages()

        listing, labels = session.statements
        assert "MATCH (m)-[:LABELED]->(l:Label)" in labels
        assert "WHERE m.id IN $p0" in labels

    def test_an_empty_page_asks_for_no_labels(self, blobs) -> None:
        session = FakeSession([])

        reader(session, blobs).list_messages()

        assert len(session.statements) == 1

    def test_nothing_is_none_where_a_row_prints_a_string(self, blobs) -> None:
        session = FakeSession(
            [(message(subject=None, body_text=None, eml_sha256=None), None)]
        )

        [summary] = reader(session, blobs).list_messages()

        assert summary.subject == ""
        assert summary.preview == ""
        assert summary.eml_sha256 is None

    def test_limit_and_offset_reach_the_statement(self, blobs) -> None:
        session = FakeSession([])

        reader(session, blobs).list_messages(limit=25, offset=50)

        [cypher] = session.statements
        assert "WHERE m.id IS NOT NULL" in cypher
        assert "ORDER BY m.sent_at DESC" in cypher
        assert "SKIP 50" in cypher
        assert "LIMIT 25" in cypher
        assert "OPTIONAL MATCH (m)-[:SENT_FROM]->(s:Address)" in cypher

    def test_the_count_is_the_graphs_answer_over_the_same_nodes(self, blobs) -> None:
        """Counted with the listing's own filter, or "2 of 3" would never fill."""
        session = FakeSession([(message(), None), (message(id="m2"), None)])

        assert reader(session, blobs).count_messages() == 2
        [cypher] = session.statements
        assert "WHERE m.id IS NOT NULL" in cypher


class TestTheRawMessage:
    def test_the_stored_bytes_come_back_by_digest(self, blobs) -> None:
        digest = blobs.put(b"From: a@example.com\r\n\r\nhi", BlobKind.MESSAGE)

        raw = reader(FakeSession([]), blobs).raw_message(digest)

        assert raw == b"From: a@example.com\r\n\r\nhi"

    def test_a_missing_blob_is_none_not_an_error(self, blobs) -> None:
        assert reader(FakeSession([]), blobs).raw_message("0" * 64) is None


class TestThePreview:
    def test_whitespace_is_folded_to_single_spaces(self) -> None:
        assert preview_of("a\n\n  b\tc  ") == "a b c"

    def test_a_long_body_is_cut_with_an_ellipsis(self) -> None:
        body = "word " * 100

        cut = preview_of(body, 20)

        assert cut == "word word word word…"
        assert len(cut) <= 21

    def test_a_body_at_the_limit_is_left_whole(self) -> None:
        body = "x" * PREVIEW_LENGTH

        assert preview_of(body) == body

    def test_no_body_is_an_empty_preview(self) -> None:
        assert preview_of(None) == ""
        assert preview_of("   ") == ""


def thread(key: str, subject: str | None = None) -> Thread:
    return Thread(id=key, subject=subject)


class TestGroupingByConversation:
    """Which conversation each message of a page sits in, and how big it is."""

    def test_a_message_reaches_its_conversation_with_the_true_size(self, blobs) -> None:
        session = FakeSession(
            [],
            threads=[(message(), thread("7:t-1"))],
            totals={"7:t-1": 12},
        )

        found = reader(session, blobs).conversations_of(["m1@example.com"])

        assert found == {"m1@example.com": Conversation(id="7:t-1", total=12)}

    def test_a_message_in_no_thread_is_absent(self, blobs) -> None:
        """The way a message without labels is absent from ``find_labels``."""
        session = FakeSession(
            [],
            threads=[(message(id="m2@example.com"), thread("7:t-1"))],
            totals={"7:t-1": 2},
        )

        found = reader(session, blobs).conversations_of(
            ["m1@example.com", "m2@example.com"]
        )

        assert list(found) == ["m2@example.com"]

    def test_two_threads_on_one_message_resolve_to_the_smallest_id(self, blobs) -> None:
        """The same mail through two mailboxes carries two ``IN_THREAD`` edges."""
        session = FakeSession(
            [],
            threads=[
                (message(), thread("9:t-9")),
                (message(), thread("7:t-1")),
            ],
            totals={"7:t-1": 3},
        )

        found = reader(session, blobs).conversations_of(["m1@example.com"])

        assert found["m1@example.com"].id == "7:t-1"

    def test_the_pick_does_not_depend_on_the_order_the_rows_came_back(
        self, blobs
    ) -> None:
        """``collect`` promises no order, so neither may the answer depend on one."""
        session = FakeSession(
            [],
            threads=[
                (message(), thread("7:t-1")),
                (message(), thread("9:t-9")),
            ],
            totals={"7:t-1": 3},
        )

        found = reader(session, blobs).conversations_of(["m1@example.com"])

        assert found["m1@example.com"].id == "7:t-1"

    def test_a_conversation_nothing_counted_reads_zero(self, blobs) -> None:
        session = FakeSession([], threads=[(message(), thread("7:t-1"))], totals={})

        found = reader(session, blobs).conversations_of(["m1@example.com"])

        assert found["m1@example.com"].total == 0

    def test_an_empty_ask_opens_no_session(self, blobs) -> None:
        session = FakeSession([])

        assert reader(session, blobs).conversations_of([]) == {}
        assert session.statements == []

    def test_the_count_is_rooted_at_the_thread(self, blobs) -> None:
        """Rooted at ``t`` so the ``IN`` lands on the key before the expansion.

        Rooted at the message it would expand every message in the archive and
        filter afterwards — the misplacement ``_filtered`` documents.
        """
        session = FakeSession(
            [], threads=[(message(), thread("7:t-1"))], totals={"7:t-1": 2}
        )

        reader(session, blobs).conversations_of(["m1@example.com"])

        _, counting = session.statements
        assert counting.startswith("MATCH (t:Thread)")
        assert "WHERE t.id IN $p0" in counting
        assert "count(DISTINCT m.id) AS total" in counting

    def test_the_page_asks_once_per_page_and_once_per_conversation_set(
        self, blobs
    ) -> None:
        """Two statements for a page, however many rows or threads it holds."""
        session = FakeSession(
            [],
            threads=[
                (message(), thread("7:t-1")),
                (message(id="m2@example.com"), thread("7:t-2")),
            ],
            totals={"7:t-1": 2, "7:t-2": 5},
        )

        reader(session, blobs).conversations_of(["m1@example.com", "m2@example.com"])

        assert len(session.statements) == 2


class TestGroupingByRecipient:
    """Whom each message of a page was sent to — one address, the same one
    every time."""

    def test_a_message_reaches_its_recipient_with_a_name(self, blobs) -> None:
        session = FakeSession(
            [],
            recipients=[
                (message(), sender(id="bob@example.com", display_names=["Bob"]))
            ],
        )

        found = reader(session, blobs).recipients_of(["m1@example.com"])

        assert found == {
            "m1@example.com": Recipient(address="bob@example.com", name="Bob")
        }

    def test_an_address_without_a_display_name_is_filed_by_its_address(
        self, blobs
    ) -> None:
        session = FakeSession(
            [], recipients=[(message(), sender(id="bob@example.com", display_names=[]))]
        )

        found = reader(session, blobs).recipients_of(["m1@example.com"])

        assert found["m1@example.com"].name == ""

    def test_several_recipients_resolve_to_the_smallest_address(self, blobs) -> None:
        """The graph keeps no header order, so the pick is a rule, not a guess."""
        session = FakeSession(
            [],
            recipients=[
                (message(), sender(id="zoe@example.com")),
                (message(), sender(id="bob@example.com")),
                (message(), sender(id="carl@example.com")),
            ],
        )

        found = reader(session, blobs).recipients_of(["m1@example.com"])

        assert found["m1@example.com"].address == "bob@example.com"

    def test_a_message_sent_to_nobody_is_absent(self, blobs) -> None:
        session = FakeSession(
            [],
            recipients=[(message(id="m2@example.com"), sender(id="bob@example.com"))],
        )

        found = reader(session, blobs).recipients_of(
            ["m1@example.com", "m2@example.com"]
        )

        assert list(found) == ["m2@example.com"]

    def test_an_empty_ask_opens_no_session(self, blobs) -> None:
        session = FakeSession([])

        assert reader(session, blobs).recipients_of([]) == {}
        assert session.statements == []

    def test_the_page_asks_once_over_the_to_edge_alone(self, blobs) -> None:
        """One ``IN`` per page, and ``SENT_TO`` without the Cc alternation."""
        session = FakeSession([])

        reader(session, blobs).recipients_of(["m1@example.com", "m2@example.com"])

        [statement] = session.statements
        assert "WHERE m.id IN $p0" in statement
        assert "[:SENT_TO]->(r:Address)" in statement
        assert "COPIED_TO" not in statement


class TestOneWholeConversation:
    def test_the_members_come_back_as_summaries_with_their_labels(self, blobs) -> None:
        session = FakeSession(
            [(message(), sender())],
            labels=[(message(), label("Rechnungen"))],
            members=["m1@example.com"],
        )

        [summary] = reader(session, blobs).conversation_messages("7:t-1")

        assert summary.id == "m1@example.com"
        assert summary.labels == (MessageLabel(name="Rechnungen"),)

    def test_the_members_are_read_newest_first_with_an_id_tiebreak(self, blobs) -> None:
        session = FakeSession([], members=[])

        reader(session, blobs).conversation_messages("7:t-1")

        [cypher] = session.statements
        assert "WHERE t.id = $p0" in cypher
        assert "ORDER BY m.sent_at DESC, m.id" in cypher

    def test_the_member_read_has_no_optional_match_before_its_where(
        self, blobs
    ) -> None:
        """The ``_filtered`` landmine, guarded rather than rediscovered.

        A ``WHERE`` naming a traversed variable is emitted after the last
        pattern clause, and one landing on an ``OPTIONAL MATCH`` nullifies the
        optional binding instead of dropping the row. So this statement
        traverses nothing optional: hydration is ``find_by_ids``'s job.
        """
        session = FakeSession([], members=[])

        reader(session, blobs).conversation_messages("7:t-1")

        [cypher] = session.statements
        assert "OPTIONAL MATCH" not in cypher

    def test_the_limit_reaches_the_statement(self, blobs) -> None:
        session = FakeSession([], members=[])

        reader(session, blobs).conversation_messages("7:t-1", limit=25)

        [cypher] = session.statements
        assert "LIMIT 25" in cypher

    def test_the_default_limit_is_the_readers_cap(self, blobs) -> None:
        session = FakeSession([], members=[])

        reader(session, blobs).conversation_messages("7:t-1")

        [cypher] = session.statements
        assert f"LIMIT {CONVERSATION_LIMIT}" in cypher

    def test_a_conversation_with_no_members_asks_for_nothing_more(self, blobs) -> None:
        session = FakeSession([(message(), sender())], members=[])

        assert reader(session, blobs).conversation_messages("7:t-1") == []
        assert len(session.statements) == 1

    def test_no_conversation_named_opens_no_session(self, blobs) -> None:
        session = FakeSession([])

        assert reader(session, blobs).conversation_messages("") == []
        assert session.statements == []
