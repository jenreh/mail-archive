"""What a rebuild does to the two layers it does not own — measured.

``test_derived_rebuild_local.py`` next door asserts what a rebuild *finds* and
that it finds the same thing twice. These are the other half of the same run
and they were split off when that file reached the thousand-line limit: what
happens to the **annotation layer**, which a rebuild may read and must never
write, and what happens to the **ground truth**, which since phase 2 carries
five properties the rebuild really does write.

The two belong together because they are the same question asked about the two
things a delete could destroy. ``Tag`` and ``TAGGED`` hold what a person
decided and nothing outside the graph ever held a copy of them; ``Message`` and
``Address`` can at least be imported again, but only if the import's own
properties are exactly where it left them.

The Cypher side of both claims is in ``test_derived_rebuild.py``, which refuses
to import a delete statement that could reach either. This is the same claim
against a real store.
"""

import corpus
import pytest
from planted_graph import ground_truth_properties, plant_tag

from mailarc_analytics import DerivedCounts, rebuild_derived
from mailarc_analytics.derived.centrality import RANK_VERSION
from mailarc_analytics.derived.importance import IMPORTANCE_VERSION, IMPORTANCE_WEIGHTS
from mailarc_core.archive.model import TagSource
from mailarc_core.archive.tags import TagRepository
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

PROJECT = tuple(corpus.canonical(f"p{n}") for n in range(1, 6))
"""The five messages of the planted ``NORD-42`` project — one topic, and the
group a tag's suggestions are argued from."""

TAG = "tag:nord-42"


def _rebuild(config: GraphConfig) -> DerivedCounts:
    with client.session(config) as graph:
        return rebuild_derived(graph, CONFIG)


class TestTheAnnotationLayerSurvives:
    """The one layer a rebuild may read and must never write.

    ``Tag`` and ``TAGGED`` are what a person decided, they are written by
    ``mailarc-core`` alone, and no delete statement in the rebuild can reach
    them — which is asserted against the compiled Cypher in
    ``test_derived_rebuild.py`` and against a real store here.
    """

    def test_a_tag_planted_before_the_rebuild_comes_through_with_its_members(
        self, archived: GraphConfig
    ) -> None:
        """Two rebuilds, and the tag still names the same five messages.

        Ground truth can be imported again if a rebuild took it; a tag cannot,
        because nothing outside the graph ever held it.
        """
        plant_tag(archived, TAG, "NORD-42", PROJECT)

        _rebuild(archived)
        _rebuild(archived)

        with client.session(archived) as graph:
            found = TagRepository(graph).list_tags()
            members = TagRepository(graph).members(TAG, limit=50)

        assert [(one.id, one.name, one.message_count) for one in found] == [
            (TAG, "NORD-42", 5)
        ]
        assert sorted(members) == sorted(PROJECT)

    def test_the_membership_keeps_the_source_the_person_gave_it(
        self, archived: GraphConfig
    ) -> None:
        """``manual`` and not ``auto``: the rebuild writes suggestions, and a
        suggestion that rewrote the decision behind an existing membership
        would be the analysis quietly claiming a human's work."""
        plant_tag(archived, TAG, "NORD-42", PROJECT)

        _rebuild(archived)

        with client.session(archived) as graph:
            sources = {
                row[0]
                for row in graph.execute(
                    "MATCH (:Message)-[r:TAGGED]->(:Tag) RETURN DISTINCT r.source"
                ).rows
            }

        assert sources == {TagSource.MANUAL.value}

    def test_a_half_tagged_topic_gets_the_rest_offered(
        self, archived: GraphConfig
    ) -> None:
        """§5.7 end to end: two of the project's five messages wear the tag, so
        the other three come back as ``SUGGESTED`` — and as suggestions only,
        which is why the membership count does not move."""
        plant_tag(archived, TAG, "NORD-42", PROJECT[:2])

        counts = _rebuild(archived)

        with client.session(archived) as graph:
            offered = sorted(
                row[0]
                for row in graph.execute(
                    "MATCH (m:Message)-[:SUGGESTED]->(:Tag) RETURN m.id"
                ).rows
            )
            members = TagRepository(graph).members(TAG, limit=50)

        assert counts.suggestions == 3
        assert offered == sorted(PROJECT[2:])
        assert sorted(members) == sorted(PROJECT[:2]), "a suggestion is not a decision"

    def test_the_suggestions_are_gone_when_the_tag_no_longer_wants_them(
        self, archived: GraphConfig
    ) -> None:
        """The delete half, on the edge that points at the annotation layer.

        Tagging the rest of the project leaves nothing to suggest, so the
        second rebuild has to remove the three edges the first one wrote —
        and leave the tag and all five messages exactly where they are.
        """
        plant_tag(archived, TAG, "NORD-42", PROJECT[:2])
        _rebuild(archived)

        with client.session(archived) as graph:
            TagRepository(graph).tag_messages(
                TAG, list(PROJECT[2:]), source=TagSource.ACCEPTED
            )
        second = _rebuild(archived)

        with client.session(archived) as graph:
            standing = graph.execute("MATCH ()-[r:SUGGESTED]->() RETURN count(r)").rows[
                0
            ][0]
            members = TagRepository(graph).members(TAG, limit=50)

        assert (second.suggestions, standing) == (0, 0)
        assert second.deleted_edges == 6, "three pairs and the three suggestions"
        assert sorted(members) == sorted(PROJECT)


class TestWhatTheRebuildDoesToTheGroundTruth:
    """R10: five properties on nodes the import wrote, and nothing else.

    ``Message.importance`` and ``Address.rank`` sit on ground-truth nodes the
    way ``Message.embedding`` already does — the import never writes them, the
    rebuild nulls them and computes them again — so the claim "a rebuild does
    not touch ground truth" has to be restated rather than dropped: everything
    the import wrote is still there, byte for byte, and the only properties
    that moved are the ones no import ever set.
    """

    def test_nothing_the_import_wrote_is_different_afterwards(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            before = ground_truth_properties(graph)

        _rebuild(archived)
        _rebuild(archived)

        with client.session(archived) as graph:
            assert ground_truth_properties(graph) == before

    def test_the_scores_land_on_the_messages_with_their_reasons(
        self, archived: GraphConfig
    ) -> None:
        """The other side of the same claim: the properties really are written,
        versioned, and carry the vocabulary a user argues with."""
        _rebuild(archived)

        with client.session(archived) as graph:
            scored = graph.execute(
                "MATCH (m:Message) WHERE m.importance IS NOT NULL "
                "RETURN count(m), min(m.importance), max(m.importance)"
            ).rows[0]
            versions = {
                row[0]
                for row in graph.execute(
                    "MATCH (m:Message) RETURN DISTINCT m.importance_version"
                ).rows
            }
            reasons = {
                reason
                for row in graph.execute(
                    "MATCH (m:Message) WHERE m.importance_reasons IS NOT NULL "
                    "RETURN m.importance_reasons"
                ).rows
                for reason in row[0]
            }

        assert scored[0] == 33
        assert 0.0 <= scored[1] <= scored[2] <= 1.0
        assert versions == {IMPORTANCE_VERSION}
        assert reasons <= set(IMPORTANCE_WEIGHTS) | {"1 reply", "2 replies"}

    def test_the_ranks_land_on_the_addresses_that_are_written_to_together(
        self, archived: GraphConfig
    ) -> None:
        """Three addresses, because A1 found three pairs — an address nobody is
        co-addressed with has no centrality to report and keeps a null."""
        _rebuild(archived)

        with client.session(archived) as graph:
            ranked = graph.execute(
                "MATCH (a:Address) WHERE a.rank IS NOT NULL RETURN count(a)"
            ).rows[0][0]
            versions = {
                row[0]
                for row in graph.execute(
                    "MATCH (a:Address) WHERE a.rank IS NOT NULL "
                    "RETURN DISTINCT a.rank_version"
                ).rows
            }

        assert ranked == 3
        assert versions == {RANK_VERSION}

    def test_a_property_from_an_earlier_run_is_nulled_before_the_new_one(
        self, archived: GraphConfig
    ) -> None:
        """The clear half, and why it is in the delete stage.

        A message that dropped out of the archive's scoring — its template
        gone, its replies purged with an account — would otherwise keep the
        number the *previous* run gave it forever. Here the stale value sits on
        a message the reads step over, so nothing recomputes it and only the
        clear can remove it.
        """
        with client.session(archived) as graph:
            graph.execute(
                "CREATE (m:Message {subject: 'no canonical id', importance: 0.99, "
                "importance_version: 'stale'})"
            )

        _rebuild(archived)

        with client.session(archived) as graph:
            standing = graph.execute(
                "MATCH (m:Message) WHERE m.importance_version = 'stale' RETURN count(m)"
            ).rows[0][0]

        assert standing == 0
