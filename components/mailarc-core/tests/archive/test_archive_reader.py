"""The projection from graph rows to summaries, and the blob lookup, without a
graph.

`FakeSession` answers the two calls :class:`MessageRepository` makes — the
listing and the count — with what the test put in, the way
`test_archive_writer.py` drives the writer. What is proved here is the
projection: which node field lands in which summary field, what a missing
sender or body becomes, and that a missing blob is a ``None`` and not a
traceback. `test_archive_reader_local.py` proves the listing itself against a
real FalkorDB.
"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import (
    Address,
    BlobKind,
    Label,
    Message,
    MessageLabel,
    MessageSummary,
)
from mailarc_core.archive.reader import (
    PREVIEW_LENGTH,
    ArchiveReader,
    GraphSessionFactory,
    preview_of,
)
from mailarc_core.mail.model import LabelKind

SENT_AT = datetime(2026, 8, 19, 14, 28, tzinfo=UTC)


class FakeSession:
    """A `runic.ogm.Session` stand-in that hands back canned rows.

    ``all_with_edges`` records the statement it was given, so a test can read
    the Cypher the repository built and check the limit and offset reached it.
    It answers two statements: the listing with ``rows``, the label lookup —
    told apart by the edge it walks — with ``labels``.

    What it records is the compiled Cypher with the compiler's backticks
    dropped. runic quotes every identifier it emits — ``m.`id``` — so that a
    model may declare a field named after a Cypher keyword; that is escaping,
    not shape, and the assertions here are about shape.
    """

    def __init__(
        self,
        rows: list[tuple[Message, Address | Label | None]],
        labels: list[tuple[Message, Address | Label | None]] | None = None,
    ) -> None:
        self.rows = rows
        self.labels = labels or []
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
        return self.rows

    def count(self, statement) -> int:
        """What ``MessageRepository.count`` runs: a filtered ``select``."""
        self.statements.append(self._cypher(statement))
        return len(self.rows)


def message(**overrides: Any) -> Message:
    fields: dict[str, Any] = {
        "id": "m1@example.com",
        "subject": "SwiftScan 19.08.2026 14.28.pdf",
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
