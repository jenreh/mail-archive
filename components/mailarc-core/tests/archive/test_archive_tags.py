"""What a tag is called, what a listing hands back, and what the guards refuse.

Three claims, none of which needs a graph.

The **slug** is the tag's identity. Two people naming the same project the same
way have to land on one node, and a name that survives a rename has to keep the
edges already hanging off it — both of which are decided by :func:`tag_id`
alone.

The **shape guards** are borrowed wholesale from
:mod:`mailarc_core.archive.purge`, and for the same reason: ``untag`` deletes a
relationship between two nodes it must not touch, so the one thing that can go
wrong with it is a ``DETACH`` that takes the ``Message`` down with the edge.
That is not a mistake a unit test of the repository would catch — it would pass
every assertion and destroy mail — so the statement is matched character by
character at import time and this file proves the match bites.

The third is the **membership read**. ``tag_messages`` reads what is already
tagged and sends only the rest, so an earlier decision keeps its ``source`` and
its ``at``. ``FakeSession`` answers the two statements out of a dict, which
makes the rows that actually go over the wire observable;
``test_archive_tags_local.py`` proves the same contract against a real graph.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from runic.ogm import QueryBuilder, alias, count, param, row, select, unwind

from mailarc_core.archive import tags
from mailarc_core.archive.model import (
    Message,
    Tag,
    Tagged,
    TagOrigin,
    TagSource,
    TagSummary,
)
from mailarc_core.archive.tags import TAG_PREFIX, TagRepository, tag_id


class FakeSession:
    """Answers the tag statements from a dict, and records what was sent.

    Told apart by the statement object, never by parsing its Cypher: the
    statements are module constants, so identity is exact and a renamed
    parameter cannot make a test pass on the wrong one.
    """

    def __init__(
        self,
        tagged: tuple[str, ...] = (),
        rows: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.tagged = tagged
        self.rows = rows
        self.sent: list[dict[str, Any]] = []

    def get(self, cls: type, pk: str) -> None:
        """No tag is ever there — the "not found" half of every verb."""
        del cls, pk

    def all_rows(
        self, statement: QueryBuilder[Any], params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        bound = params or {}
        if statement is tags.MEMBERSHIP:
            asked = set(bound["ids"])
            return [{"id": one} for one in self.tagged if one in asked]
        if statement is tags.TAG_MESSAGES:
            rows = list(bound["rows"])
            self.sent.append({"rows": rows})
            return [{"written": len(rows)}]
        if statement is tags.TAGS_OF:
            return list(self.rows)
        raise AssertionError(f"unexpected statement {statement!r}")


class TestTheSlug:
    def test_it_is_prefixed_so_a_tag_id_is_recognisable_on_sight(self) -> None:
        assert tag_id("Nordlicht").startswith(TAG_PREFIX)

    def test_it_lowercases_and_joins_words_with_one_dash(self) -> None:
        assert tag_id("NORD-42 Angebot") == "tag:nord-42-angebot"

    def test_punctuation_and_padding_collapse(self) -> None:
        assert tag_id("  //Q3//  ") == "tag:q3"

    def test_german_letters_transliterate_rather_than_vanish(self) -> None:
        """``ascii``-folding alone turns ``Straße`` into ``strae``, which is a
        name nobody typed. The four letters that have an established two-letter
        form get it before the fold."""
        assert tag_id("Kündigung Straße") == "tag:kuendigung-strasse"

    def test_two_names_that_differ_only_in_shape_are_one_tag(self) -> None:
        """The point of the slug: "NORD 42" and "nord-42" are the same project
        and must not become two nodes with half the mail each."""
        assert tag_id("NORD 42") == tag_id("nord-42")

    def test_a_name_with_no_latin_letters_still_gets_a_usable_id(self) -> None:
        """A digest rather than a bare prefix. Without the fallback every such
        name would slug to ``tag:`` and the second one would collide with the
        first — silently, because that is a valid key."""
        one, two = tag_id("案件"), tag_id("プロジェクト")

        assert one != TAG_PREFIX
        assert one != two
        assert one == tag_id("案件")

    def test_a_blank_name_is_refused_rather_than_keyed(self) -> None:
        with pytest.raises(ValueError, match="name"):
            tag_id("   ")


class TestTheSummary:
    def test_it_is_frozen(self) -> None:
        summary = TagSummary(id="tag:x", name="X")

        with pytest.raises(ValidationError):
            summary.name = "Y"  # ty: ignore[invalid-assignment]

    def test_a_tag_nobody_counted_reads_zero_rather_than_unknown(self) -> None:
        """Only the listing counts; every other read leaves it at zero, and the
        docstring says so rather than the number pretending to be a count."""
        assert TagSummary(id="tag:x").message_count == 0

    def test_it_defaults_to_the_origin_a_human_would_have_chosen(self) -> None:
        assert TagSummary(id="tag:x").origin is TagOrigin.MANUAL

    def test_it_parses_the_iso_string_the_graph_hands_back(self) -> None:
        """A projected ``t.created_at`` is a string — the mapper's converters
        run on entities, not on projected rows."""
        summary = TagSummary(id="tag:x", created_at="2026-03-04T09:15:00+00:00")

        assert summary.created_at == datetime(2026, 3, 4, 9, 15, tzinfo=UTC)


class TestTheShapeGuards:
    def test_the_delete_is_the_shape_the_guard_pins(self) -> None:
        """The guard passed at import; this is what it passed *on*."""
        assert tags._normalised(tags.DELETE_TAG.build()[0]) == (  # noqa: SLF001
            "MATCH (t:Tag) WHERE t.id = $tag DETACH DELETE t RETURN count(t) AS removed"
        )

    def test_the_untag_deletes_the_edge_and_never_detaches(self) -> None:
        cypher = tags._normalised(tags.UNTAG.build()[0])  # noqa: SLF001

        assert "DETACH" not in cypher
        assert "DELETE r" in cypher

    def test_the_untag_predicate_stands_before_the_delete(self) -> None:
        """runic emits a predicate naming a *traversed* variable after the whole
        pipeline, which on a delete means behind the ``DELETE`` it was meant to
        narrow. The ``WITH r, m`` stage is what pulls it back in front — without
        it this statement empties the tag instead of removing five messages
        from it."""
        cypher = tags._normalised(tags.UNTAG.build()[0])  # noqa: SLF001

        assert cypher.index("WHERE m.id IN $ids") < cypher.index("DELETE r")

    def test_a_detach_on_a_message_is_refused_by_the_tag_delete_guard(self) -> None:
        message = alias(Message, "m")
        detaching = (
            select(message)
            .where(message.id == param("tag"))
            .delete(detach=True)
            .returning(count("m").as_("removed"))
        )

        with pytest.raises(ValueError, match="unknown shape"):
            tags._verified(detaching, tags._TAG_DELETE)  # noqa: SLF001

    def test_a_detaching_untag_is_refused(self) -> None:
        """The edit that would pass every repository test and destroy mail:
        ``delete(detach=True)`` on the traversal takes both endpoints, so every
        message the tag named would be gone."""
        tag, message = alias(Tag, "t"), alias(Message, "m")
        detaching = (
            select(tag)
            .where(tag.id == param("tag"))
            .traverse(Tag.messages, to=message, edge=alias(Tagged, "r"))
            .with_(message, where=message.id.in_(param("ids")))
            .delete(detach=True)
            .returning(count("r").as_("removed"))
        )

        with pytest.raises(ValueError, match="unknown shape"):
            tags._verified(detaching, tags._UNTAG_SHAPE)  # noqa: SLF001

    @pytest.mark.parametrize("name", sorted(tags.READS | tags.WRITES))
    def test_nothing_but_the_two_deletes_can_destroy_anything(self, name: str) -> None:
        statement = (tags.READS | tags.WRITES)[name]

        assert "DELETE" not in tags._normalised(statement.build()[0])  # noqa: SLF001

    def test_the_colour_is_cleared_with_an_explicit_null(self) -> None:
        """runic's dirty tracking encodes only properties that have a value, so
        ``node.color = None`` plus a flush emits a ``SET`` that never mentions
        the colour and the old one stands."""
        assert tags._normalised(tags.CLEAR_COLOR.build()[0]) == (  # noqa: SLF001
            "MATCH (t:Tag) WHERE t.id = $tag SET t.color = NULL "
            "RETURN count(t) AS cleared"
        )

    def test_the_write_merges_a_relationship_and_never_a_label(self) -> None:
        """``MERGE (m:Message {id: row.id})`` would invent an empty message
        wherever a caller named one that is not there — which is how tagging
        starts writing ground truth without anybody deciding to."""
        cypher = tags._normalised(tags.TAG_MESSAGES.build()[0])  # noqa: SLF001

        assert "MERGE (m)-[r:TAGGED]->(t)" in cypher
        assert "MERGE (m:" not in cypher
        assert "MERGE (t:" not in cypher


class TestTheGuardsRefuseAnEditedStatement:
    def test_a_read_that_grew_a_delete_fails_the_import(self) -> None:
        tag = alias(Tag, "t")
        destructive = {
            "MEMBERS": select(tag)
            .where(tag.id == param("tag"))
            .delete(detach=True)
            .returning(count("t").as_("removed"))
        }

        with pytest.raises(ValueError, match="deletes"):
            tags._harmless(destructive)  # noqa: SLF001

    def test_an_upsert_that_merges_a_label_fails_the_import(self) -> None:
        """``MERGE (m:Message {id: row.id})`` invents an empty message wherever
        a row names one that is not there — a derived layer writing ground truth
        without anybody deciding to."""
        merging = {
            "TAG_MESSAGES": unwind(param("rows")).merge(
                Message, key={Message.id: row("id")}, alias="m"
            )
        }

        with pytest.raises(ValueError, match="merges a node"):
            tags._harmless(merging)  # noqa: SLF001


class TestWhatAReadTolerates:
    def test_an_unreadable_origin_is_read_as_manual(self) -> None:
        """A graph that has been around can hold a value a newer build wrote,
        and a listing that raised on one node would take the whole page down —
        the same argument the message listing makes about id-less nodes."""
        assert tags._origin_of("from-a-future-release") is TagOrigin.MANUAL  # noqa: SLF001
        assert tags._origin_of(None) is TagOrigin.MANUAL  # noqa: SLF001

    def test_a_write_that_answered_nothing_counts_zero(self) -> None:
        assert tags._first_count([], "removed") == 0  # noqa: SLF001

    def test_asking_about_no_messages_costs_no_round_trip(self) -> None:
        session = FakeSession()

        assert TagRepository(session).tags_of([]) == {}  # ty: ignore[invalid-argument-type]

    def test_a_row_with_no_message_is_dropped_rather_than_keyed_empty(self) -> None:
        """A ``TAGGED`` edge on a message with no canonical id is not something
        the writer produces, but a graph that has been around can hold one, and
        an empty key would collect every such row under one heading."""
        session = FakeSession(
            rows=(
                {"message_id": "", "tag_id": "tag:x"},
                {"message_id": "m1", "tag_id": "tag:x"},
            )
        )

        found = TagRepository(session).tags_of(["m1"])  # ty: ignore[invalid-argument-type]

        assert set(found) == {"m1"}

    def test_recolouring_a_tag_that_is_not_there_says_so(self) -> None:
        session = FakeSession()

        assert TagRepository(session).recolor("tag:gone", "#fff") is False  # ty: ignore[invalid-argument-type]


class TestTaggingIsDecidedOnce:
    def test_a_message_already_tagged_is_not_sent_again(self) -> None:
        session = FakeSession(tagged=("m1",))

        TagRepository(session).tag_messages(  # ty: ignore[invalid-argument-type]
            "tag:nord", ["m1", "m2"], source=TagSource.AUTO
        )

        assert [row["id"] for row in session.sent[0]["rows"]] == ["m2"]

    def test_nothing_is_sent_when_every_message_already_wears_it(self) -> None:
        """Not merely cheaper — a ``SET`` over the existing edges would rewrite
        ``at`` and ``source``, which is the decision this method must not
        touch."""
        session = FakeSession(tagged=("m1", "m2"))

        written = TagRepository(session).tag_messages(  # ty: ignore[invalid-argument-type]
            "tag:nord", ["m1", "m2"], source=TagSource.AUTO
        )

        assert written == 0
        assert session.sent == []

    def test_a_repeated_id_is_written_once(self) -> None:
        session = FakeSession()

        TagRepository(session).tag_messages(  # ty: ignore[invalid-argument-type]
            "tag:nord", ["m1", "m1"], source=TagSource.MANUAL
        )

        assert [row["id"] for row in session.sent[0]["rows"]] == ["m1"]

    def test_the_rows_carry_the_encoded_decision(self) -> None:
        """``$rows`` never passes through the mapper, so the enum and the
        timestamp have to arrive already encoded or the driver refuses them."""
        session = FakeSession()
        at = datetime(2026, 3, 4, 9, 15, tzinfo=UTC)

        TagRepository(session).tag_messages(  # ty: ignore[invalid-argument-type]
            "tag:nord", ["m1"], source=TagSource.ACCEPTED, at=at
        )

        assert session.sent[0]["rows"] == [
            {
                "id": "m1",
                "tag_id": "tag:nord",
                "source": "accepted",
                "at": "2026-03-04T09:15:00+00:00",
            }
        ]

    def test_an_empty_list_costs_no_round_trip(self) -> None:
        session = FakeSession()

        assert (
            TagRepository(session).untag("tag:nord", [])  # ty: ignore[invalid-argument-type]
            == 0
        )
        assert (
            TagRepository(session).tag_messages(  # ty: ignore[invalid-argument-type]
                "tag:nord", [], source=TagSource.MANUAL
            )
            == 0
        )
        assert session.sent == []
