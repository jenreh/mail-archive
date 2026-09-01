"""The schema declarations, checked as declarations.

These fields are read back by `runic.migrate`, which turns them into the real
indexes, and by the mapper, which uses the converters to encode a value on its
way out. Both failures are silent: a dropped index only shows up as a slow
query on a full archive, and a missing `DatetimeConverter` only shows up as a
`sent_at` the graph cannot compare. Neither would fail a writer test.

`TestConverters` in particular guards a live trap — see the note on
`Message.replies_to`. Annotating a self-referencing relation with a forward
reference does not just break that field, it aborts runic's whole annotation
pass and strips the converters off every other field on the node.
"""

from datetime import datetime

from runic.ogm import Edge, Node, Vector

from mailarc_core.archive.model import (
    Account,
    Address,
    ArchivedFrom,
    ArchiveSource,
    Attachment,
    HasAttachment,
    Label,
    Message,
    Thread,
    to_signed_64,
    to_unsigned_64,
)
from mailarc_core.mail.model import MailProvider

NODES = (Message, Address, Thread, Label, Attachment, Account)


def _field(node: type[Node | Edge], name: str):
    return next(fi.field for fi in node._fields if fi.name == name)


def _relation(name: str) -> str:
    return _field(Message, name).relationship


class TestNodes:
    def test_every_node_is_keyed_by_a_single_primary_key(self) -> None:
        for node in NODES:
            keys = [fi.name for fi in node._fields if fi.field.primary_key]
            assert keys == ["id"], node.__name__

    def test_the_labels_are_the_ones_a_query_will_be_written_against(self) -> None:
        assert [node._primary_label for node in NODES] == [
            "Message",
            "Address",
            "Thread",
            "Label",
            "Attachment",
            "Account",
        ]


class TestIndexes:
    def test_the_two_searchable_fields_are_fulltext(self) -> None:
        assert _field(Message, "subject").index_type == "FULLTEXT"
        assert _field(Message, "body_text").index_type == "FULLTEXT"

    def test_every_field_an_analysis_filters_on_is_indexed(self) -> None:
        for name in ("sent_at", "subject_norm", "simhash", "participant_key"):
            assert _field(Message, name).index is True, name
        assert _field(Address, "domain").index is True

    def test_the_rfc_message_id_is_unique(self) -> None:
        """Two nodes carrying one Message-ID would mean the id is not canonical."""
        assert _field(Message, "rfc_message_id").unique is True

    def test_the_embedding_is_declared_now_and_written_later(self) -> None:
        embedding = _field(Message, "embedding")

        assert embedding.index_type == "VECTOR"
        assert Message(id="m1").embedding is None
        assert Message(id="m1").embedding_model is None


class TestConverters:
    """The pass that assigns these is all-or-nothing, so check it is intact."""

    def test_a_datetime_field_knows_how_to_encode_itself(self) -> None:
        assert _field(Message, "sent_at").converter is not None
        assert _field(ArchivedFrom, "archived_at").converter is not None

    def test_the_vector_field_knows_how_to_encode_itself(self) -> None:
        assert _field(Message, "embedding").converter is not None

    def test_an_enum_field_knows_how_to_encode_itself(self) -> None:
        assert _field(Label, "kind").converter is not None
        assert _field(Account, "provider").converter is not None

    def test_the_declared_types_survive_a_round_trip(self) -> None:
        """Not just present — the converters have to be the right ones."""
        sent_at = _field(Message, "sent_at").converter
        provider = _field(Account, "provider").converter
        embedding = _field(Message, "embedding").converter

        moment = datetime.fromisoformat("2026-03-04T09:15:00+00:00")

        assert sent_at.from_graph(sent_at.to_graph(moment)) == moment
        assert provider.from_graph(provider.to_graph(MailProvider.GMAIL)) == (
            MailProvider.GMAIL
        )
        assert list(embedding.from_graph(embedding.to_graph(Vector([1.0, 2.0])))) == [
            1.0,
            2.0,
        ]


class TestEdges:
    def test_the_recipient_roles_have_one_edge_type_each(self) -> None:
        """RFC 5322 closes the set, so A1 walks a type instead of filtering."""
        assert _relation("sender") == "SENT_FROM"
        assert _relation("recipients") == "SENT_TO"
        assert _relation("copied_to") == "COPIED_TO"
        assert _relation("blind_copied_to") == "BLIND_COPIED_TO"

    def test_the_remaining_edges_are_named_as_the_model_says(self) -> None:
        assert _relation("thread") == "IN_THREAD"
        assert _relation("replies_to") == "REPLIES_TO"
        assert _relation("labels") == "LABELED"
        assert _relation("attachments") == "HAS_ATTACHMENT"
        assert _relation("archived_from") == "ARCHIVED_FROM"

    def test_every_edge_points_away_from_the_message(self) -> None:
        for name in (
            "sender",
            "recipients",
            "copied_to",
            "blind_copied_to",
            "thread",
            "replies_to",
            "labels",
            "attachments",
            "archived_from",
        ):
            assert _field(Message, name).direction == "OUTGOING", name

    def test_the_two_edges_with_properties_carry_them(self) -> None:
        assert HasAttachment._edge_type == "HAS_ATTACHMENT"
        assert [fi.name for fi in HasAttachment._fields] == [
            "filename",
            "content_id",
            "inline",
        ]
        assert ArchivedFrom._edge_type == "ARCHIVED_FROM"
        assert [fi.name for fi in ArchivedFrom._fields] == [
            "provider_message_id",
            "provider_thread_id",
            "folder",
            "uid",
            "archived_at",
        ]


class TestSignedSimhash:
    """A SimHash uses all 64 bits; a graph integer has 63 and a sign."""

    def test_a_value_below_the_sign_bit_is_left_alone(self) -> None:
        assert to_signed_64(0) == 0
        assert to_signed_64(2**63 - 1) == 2**63 - 1

    def test_a_value_at_or_above_the_sign_bit_becomes_negative(self) -> None:
        assert to_signed_64(2**63) == -(2**63)
        assert to_signed_64(2**64 - 1) == -1

    def test_the_round_trip_keeps_every_bit(self) -> None:
        for value in (0, 1, 2**63 - 1, 2**63, 2**64 - 1, 0xDEADBEEFCAFEBABE):
            assert to_unsigned_64(to_signed_64(value)) == value

    def test_the_signed_form_still_fits_a_64_bit_integer(self) -> None:
        """The whole point: what goes to the graph has to be storable."""
        for value in (2**63, 2**64 - 1):
            assert -(2**63) <= to_signed_64(value) <= 2**63 - 1


class TestArchiveSource:
    def test_it_is_frozen(self) -> None:
        source = ArchiveSource(
            account_id="7",
            account_address="me@example.com",
            provider=MailProvider.GMAIL,
            provider_message_id="g-1",
        )

        assert source.archived_at is None
        assert source.labels == ()
        assert source.model_config["frozen"] is True
