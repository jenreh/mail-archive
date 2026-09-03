"""§5.2's six write halves against a real graph — upserts, and what they touch.

``test_derived_writer_local.py``'s argument for the stages this phase adds.
Idempotence rests on the catalogue's statements being ``MERGE`` rather than
``CREATE``, and none of that is visible in Python; it is visible in a node
count.

Two of these are not merges at all and get the most attention here.
``WRITE_IMPORTANCE`` and ``WRITE_ADDRESS_RANKS`` set properties on
**ground-truth** nodes — a ``Message`` and an ``Address`` the import wrote —
which is R10, and the whole safety of it rests on two claims a store can
settle: a row naming a node that is not there writes *nothing* (rather than
merging an empty one into existence), and what comes back is the count the
store found (rather than the number of rows that were sent).

``SUGGESTED`` is the third one worth a graph. It is the only derived edge that
points at the **annotation layer**, and the ``Tag`` at its far end is written by
``mailarc-core`` and may never be created here — so a suggestion naming a tag
somebody deleted has to write nothing at all.
"""

import corpus
import pytest
from runic.ogm import Session

from mailarc_analytics import (
    CommunityFacts,
    CommunityFindings,
    GroupingKind,
    ImportanceScore,
    Suggestion,
    build_topics,
    write_address_ranks,
    write_communities,
    write_importance,
    write_keywords,
    write_suggestions,
    write_topics,
)
from mailarc_core.archive.tags import TagRepository
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

CIRCLE = "community:planted"
"""A key no digest would mint, so a survivor of a delete is unmistakable."""


def _facts() -> CommunityFacts:
    """One circle over three of the corpus's addresses and one of its messages."""
    return CommunityFacts(
        id=CIRCLE,
        label="kunde.example",
        members={corpus.ANNA: 0.5, corpus.THOMAS: 0.25, corpus.OWN: 0.75},
        messages={corpus.canonical("p1"): 1.0},
    )


def _counted(session: Session, pattern: str) -> int:
    return int(session.execute(f"MATCH {pattern} RETURN count(*)").rows[0][0])


class TestTheCircleWrites:
    """A ``Community`` and the two edges that hang off it."""

    def test_the_circle_its_members_and_its_mail_all_arrive(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            write_communities(graph, CommunityFindings(communities=(_facts(),)))

            assert _counted(graph, "(c:Community)") == 1
            assert _counted(graph, "(:Address)-[:MEMBER_OF]->(:Community)") == 3
            assert _counted(graph, "(:Message)-[:IN_CIRCLE]->(:Community)") == 1

    def test_writing_the_same_circle_twice_changes_nothing(
        self, archived: GraphConfig
    ) -> None:
        findings = CommunityFindings(communities=(_facts(),))
        with client.session(archived) as graph:
            write_communities(graph, findings)
            before = graph.execute(
                "MATCH (c:Community) RETURN c.id, properties(c) ORDER BY c.id"
            ).rows

            write_communities(graph, findings)

            assert (
                graph.execute(
                    "MATCH (c:Community) RETURN c.id, properties(c) ORDER BY c.id"
                ).rows
                == before
            )
            assert _counted(graph, "(c:Community)") == 1

    def test_the_rank_the_centrality_stage_found_rides_on_the_edge(
        self, archived: GraphConfig
    ) -> None:
        """So a subgraph read can size a member without a second hop — and the
        reason ``CENTRALITY`` runs before ``COMMUNITIES``."""
        with client.session(archived) as graph:
            write_communities(graph, CommunityFindings(communities=(_facts(),)))

            rows = graph.execute(
                "MATCH (a:Address)-[r:MEMBER_OF]->(:Community) "
                "RETURN a.id, r.rank ORDER BY a.id"
            ).rows

        assert rows == [
            [corpus.ANNA, 0.5],
            [corpus.OWN, 0.75],
            [corpus.THOMAS, 0.25],
        ]

    def test_an_address_the_archive_does_not_hold_writes_no_edge(
        self, archived: GraphConfig
    ) -> None:
        """``MERGE_MEMBER_OF`` matches both ends. Merging the address end would
        invent a *ground-truth* node carrying nothing but a rank."""
        stranger = CommunityFacts(id=CIRCLE, members={"nobody@nowhere.example": 0.1})

        with client.session(archived) as graph:
            write_communities(graph, CommunityFindings(communities=(stranger,)))

            assert _counted(graph, "(:Address {id: 'nobody@nowhere.example'})") == 0
            assert _counted(graph, "()-[:MEMBER_OF]->()") == 0


class TestTheGroundTruthPropertyWrites:
    """R10 — a property on a node the import wrote, set and never merged."""

    def test_an_address_rank_lands_with_its_version(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            written = write_address_ranks(graph, {corpus.ANNA: 0.25}, version="1")

            rows = graph.execute(
                "MATCH (a:Address {id: $id}) RETURN a.rank, a.rank_version",
                {"id": corpus.ANNA},
            ).rows

        assert written == 1
        assert rows == [[0.25, "1"]]

    def test_a_rank_for_an_address_that_is_not_there_writes_nothing(
        self, archived: GraphConfig
    ) -> None:
        """And is not counted, so a stage reports what landed rather than what
        it hoped for."""
        with client.session(archived) as graph:
            written = write_address_ranks(graph, {"gone@nowhere.example": 0.9})

            assert _counted(graph, "(:Address {id: 'gone@nowhere.example'})") == 0

        assert written == 0

    def test_an_importance_score_lands_with_its_reasons_and_version(
        self, archived: GraphConfig
    ) -> None:
        scored = ImportanceScore(
            message_id=corpus.canonical("p1"),
            score=0.75,
            reasons=("addressed directly", "has attachments"),
        )

        with client.session(archived) as graph:
            written = write_importance(graph, (scored,))

            rows = graph.execute(
                "MATCH (m:Message {id: $id}) "
                "RETURN m.importance, m.importance_reasons, m.importance_version",
                {"id": corpus.canonical("p1")},
            ).rows

        assert written == 1
        assert rows == [[0.75, ["addressed directly", "has attachments"], "1"]]

    def test_a_score_for_a_message_that_is_not_there_invents_no_message(
        self, archived: GraphConfig
    ) -> None:
        """The one thing this package may never do to ground truth."""
        before = _messages(archived)

        with client.session(archived) as graph:
            written = write_importance(
                graph, (ImportanceScore(message_id="ghost@nowhere.example"),)
            )

        assert written == 0
        assert _messages(archived) == before


class TestTheKeywordWrite:
    """A second pass over a ``Topic`` the clustering stage already wrote."""

    def test_keywords_land_on_the_topic_the_clustering_produced(
        self, archived: GraphConfig
    ) -> None:
        clusters = build_topics(corpus.planted_facts(), CONFIG).clusters

        with client.session(archived) as graph:
            write_topics(graph, clusters)
            write_keywords(graph, {clusters[0].id: ("angebot", "datenmigration")})

            rows = graph.execute(
                "MATCH (t:Topic {id: $id}) RETURN t.keywords", {"id": clusters[0].id}
            ).rows

        assert rows == [[["angebot", "datenmigration"]]]

    def test_keywords_for_a_topic_that_is_not_there_invent_no_topic(
        self, archived: GraphConfig
    ) -> None:
        """The two stages disagreeing is a bug, and an empty ``Topic`` is a
        worse way to find that out than a row that wrote nothing."""
        with client.session(archived) as graph:
            write_keywords(graph, {"topic:gone": ("angebot",)})

            assert _counted(graph, "(t:Topic)") == 0


class TestTheSuggestionWrite:
    """The one derived edge that points at the annotation layer."""

    def test_a_suggestion_lands_on_a_tag_a_person_created(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            tag = TagRepository(graph).create("NORD-42")
            written = write_suggestions(
                graph,
                (
                    Suggestion(
                        tag_id=tag.id,
                        message_id=corpus.canonical("p1"),
                        score=0.6,
                        method=GroupingKind.THREAD,
                    ),
                ),
            )

            rows = graph.execute(
                "MATCH (:Message)-[r:SUGGESTED]->(:Tag) RETURN r.score, r.method"
            ).rows

        assert written == 1
        assert rows == [[0.6, "thread"]], "the method is a plain string, not a repr"

    def test_a_suggestion_naming_a_tag_nobody_created_writes_nothing(
        self, archived: GraphConfig
    ) -> None:
        """A row naming a tag a human deleted has to write nothing at all — a
        merge would resurrect it as a node with a name nobody chose."""
        with client.session(archived) as graph:
            write_suggestions(
                graph,
                (Suggestion(tag_id="tag:deleted", message_id=corpus.canonical("p1")),),
            )

            assert _counted(graph, "(t:Tag)") == 0
            assert _counted(graph, "()-[:SUGGESTED]->()") == 0

    def test_writing_the_same_suggestion_twice_leaves_one_edge(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            tag = TagRepository(graph).create("NORD-42")
            offered = (
                Suggestion(
                    tag_id=tag.id,
                    message_id=corpus.canonical("p1"),
                    score=0.6,
                    method=GroupingKind.TOPIC,
                ),
            )

            write_suggestions(graph, offered)
            write_suggestions(graph, offered)

            assert _counted(graph, "()-[:SUGGESTED]->()") == 1

    def test_a_suggestion_never_becomes_a_membership(
        self, archived: GraphConfig
    ) -> None:
        """``TAGGED`` records what a human decided and nothing in this package
        may write one."""
        with client.session(archived) as graph:
            tag = TagRepository(graph).create("NORD-42")
            write_suggestions(
                graph,
                (
                    Suggestion(
                        tag_id=tag.id,
                        message_id=corpus.canonical("p1"),
                        score=0.9,
                        method=GroupingKind.THREAD,
                    ),
                ),
            )

            assert _counted(graph, "()-[:TAGGED]->()") == 0


def _messages(config: GraphConfig) -> list[list[object]]:
    """Every message id in the archive, so an invented one is visible."""
    with client.session(config) as graph:
        return graph.execute("MATCH (m:Message) RETURN m.id ORDER BY m.id").rows
