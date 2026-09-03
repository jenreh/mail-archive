"""The reader against a real graph — and the cross-check against a broken one.

Everything above this file works from scripted result sets, which proves the
façade decodes what it is handed and nothing about what a FalkorDB actually
hands it. So the planted corpus is archived with the real writer, rebuilt with
the real rebuild, and read back with the real reader: the numbers asserted here
are the ones ``test_derived_rebuild_local.py`` measured off the same corpus, so
a change in an analysis shows up as a disagreement between two files rather
than as a quietly updated expectation in one.

**The part that makes this a test rather than a report** is at the bottom.
:data:`~mailarc_analytics.queries.catalog.CO_RECIPIENTS` is A1's definition and
the ``CO_ADDRESSED`` edge is its materialisation, and the catalogue says
plainly that if the two disagree the edge is wrong — so the corpus is rebuilt,
one stored count is then corrupted by hand, and the reader has to say so. A
cross-check nobody has ever seen fail is a cross-check nobody knows works.
"""

from datetime import UTC, datetime
from functools import partial

import corpus
import pytest
from runic.ogm import Session

from mailarc_analytics import AnalyticsReader, TemplateDirection, rebuild_derived
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import rows_of
from mailarc_core.archive.model import TagOrigin, TagSource
from mailarc_core.archive.tags import TagRepository
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

INVENTED = 4
"""What the fabricated pair claims. Any number does; a count nothing supports
is wrong at one as much as at a hundred."""


@pytest.fixture
def derived(archived: GraphConfig) -> GraphConfig:
    """The planted corpus with exactly one rebuild over it.

    One rebuild and not two, because idempotence is
    ``test_derived_rebuild_local.py``'s claim and this file's numbers should
    fail for their own reasons.
    """
    with client.session(archived) as graph:
        rebuild_derived(graph, CONFIG)
    return archived


def _reader(config: GraphConfig) -> AnalyticsReader:
    """The façade wired the way ``app/composition.py`` wires it."""
    return AnalyticsReader(partial(client.session, config))


def _heaviest_pair(session: Session) -> tuple[str, str, int]:
    """The stored pair with the largest count — read without an arrow, the way
    every read of an undirected edge has to be."""
    rows = session.execute(
        "MATCH (a:Address)-[r:CO_ADDRESSED]-(b:Address) WHERE a.id < b.id "
        "RETURN a.id AS left_id, b.id AS right_id, r.count AS together "
        "ORDER BY together DESC LIMIT 1"
    ).rows
    left, right, together = rows[0]
    return str(left), str(right), int(together)


def _set_count(session: Session, left: str, right: str, count: int) -> None:
    session.execute(
        "MATCH (a:Address {id: $left})-[r:CO_ADDRESSED]-(b:Address {id: $right}) "
        "SET r.count = $count",
        {"left": left, "right": right, "count": count},
    )


def _drop_pair(session: Session, left: str, right: str) -> None:
    session.execute(
        "MATCH (a:Address {id: $left})-[r:CO_ADDRESSED]-(b:Address {id: $right}) "
        "DELETE r",
        {"left": left, "right": right},
    )


def _invent_pair(session: Session, left: str, right: str, count: int) -> None:
    """Write a pair through the same statement a rebuild writes with.

    Hand-rolled Cypher would plant an edge no write path can produce, and then
    the cross-check would only be catching something that cannot happen. This
    is what a bug in A1 would leave behind.

    Run through ``rows_of`` — the statement is a query-builder object and only
    the session can bind it — and with the pair put in the order the write path
    guarantees. ``CoAddressedPair`` orders every finding smaller-id-first
    because the ``MERGE`` is directed, so a helper that wrote them the other way
    round would be planting an edge a rebuild cannot produce, which is the one
    thing this helper exists not to do.
    """
    smaller, larger = sorted((left, right))
    rows_of(
        session,
        catalog.MERGE_CO_ADDRESSED,
        {
            "rows": [
                {
                    "left": smaller,
                    "right": larger,
                    "count": count,
                    "first_seen": None,
                    "last_seen": None,
                }
            ]
        },
    )


class TestWhatTheArchiveTotalsSay:
    """Six numbers off a real graph, ground truth and derived layer together."""

    def test_they_are_the_ones_the_rebuild_reported(self, derived: GraphConfig) -> None:
        found = _reader(derived).totals()

        assert found.messages == 33
        assert found.unidentified == 0
        assert (found.groups, found.topics, found.templates) == (2, 1, 2)
        assert found.co_addressed == 3

    def test_an_archive_nobody_has_rebuilt_says_so(self, archived: GraphConfig) -> None:
        """Thirty-three messages and nothing derived is one sentence — "no
        rebuild has run" — and a different one from "the analyses found
        nothing"."""
        found = _reader(archived).totals()

        assert found.messages == 33
        assert found.derived == 0


class TestReadingTheFindingsBack:
    """Exactly what was planted, through the façade rather than through rows."""

    def test_the_recurring_groups_are_the_two_planted_circles(
        self, derived: GraphConfig
    ) -> None:
        found = _reader(derived).recurring_groups(
            min_size=CONFIG.min_group_size, min_messages=CONFIG.min_group_messages
        )

        assert [(one.id, one.size, one.message_count) for one in found] == [
            (corpus.circle_of("p1"), 3, 5),
            (corpus.circle_of("b1"), 3, 2),
        ]

    def test_a_group_brings_a_span_back_that_can_be_compared(
        self, derived: GraphConfig
    ) -> None:
        """Aware, because the graph stores an ISO string and raw Cypher goes
        past the converter that would have decoded it."""
        first_seen = _reader(derived).recurring_groups()[0].first_seen

        assert first_seen is not None
        assert first_seen.tzinfo is not None
        assert first_seen > datetime(2020, 1, 1, tzinfo=UTC)

    def test_a_stricter_threshold_narrows_the_listing(
        self, derived: GraphConfig
    ) -> None:
        """Asking for more here cannot widen anything — the thresholds that
        decided which groups exist were applied when they were written."""
        found = _reader(derived).recurring_groups(min_messages=5)

        assert [one.id for one in found] == [corpus.circle_of("p1")]

    def test_the_topic_is_the_project_and_says_what_drew_it(
        self, derived: GraphConfig
    ) -> None:
        found = _reader(derived).topics()

        assert [(one.id, one.label, one.method, one.messages) for one in found] == [
            (
                "topic:8ddcd22af04394667b0b8bfef1d1a97e",
                "angebot datenmigration",
                "ref",
                5,
            )
        ]

    def test_the_templates_come_back_one_direction_at_a_time(
        self, derived: GraphConfig
    ) -> None:
        """§6.3: only what you write yourself is worth automating, and the
        scores are calibrated within a direction."""
        reader = _reader(derived)

        sent = reader.templates(TemplateDirection.SENT)
        received = reader.templates(TemplateDirection.RECEIVED)

        assert [(one.id, one.occurrences, one.automation_score) for one in sent] == [
            ("template:1e164feec6258562:sent", 12, 0.641072)
        ]
        assert [
            (one.id, one.occurrences, one.automation_score) for one in received
        ] == [("template:132b71d16ae83c39:received", 10, 0.279724)]

    def test_every_template_row_knows_which_way_it_travelled(
        self, derived: GraphConfig
    ) -> None:
        found = _reader(derived).templates(TemplateDirection.RECEIVED)

        assert all(one.direction is TemplateDirection.RECEIVED for one in found)
        assert all(one.sample_text for one in found)

    def test_both_readings_of_a1_return_the_same_three_pairs(
        self, derived: GraphConfig
    ) -> None:
        reader = _reader(derived)

        defined = reader.co_recipients()
        stored = reader.top_co_addressed()

        assert len(stored) == 3
        assert sorted((one.left_id, one.right_id, one.together) for one in defined) == (
            sorted((one.left_id, one.right_id, one.together) for one in stored)
        )


class TestReadingPhaseTwoBack:
    """The five listings the new stages made answerable, off a real rebuild.

    The scripted tests above prove the façade decodes what it is handed; these
    prove the store hands it that. The numbers are the ones
    ``test_derived_rebuild_local.py`` measured off the same corpus, so a change
    in an analysis shows up as two files disagreeing.
    """

    def test_the_one_planted_circle_comes_back_labelled_by_its_domain(
        self, derived: GraphConfig
    ) -> None:
        """A name a human recognises and one nobody invented — §1.2's rule
        about what may appear on a derived node."""
        found = _reader(derived).communities()

        assert len(found) == 1
        assert found[0].id.startswith("community:")
        assert found[0].label.endswith(".example")
        assert (found[0].size, found[0].message_count) == (3, 33)
        assert found[0].method == "lpa"

    def test_the_important_messages_come_back_scored_and_argued(
        self, derived: GraphConfig
    ) -> None:
        """Ordered by the score, every row carrying the vocabulary behind it."""
        found = _reader(derived).important_messages(limit=5)

        assert len(found) == 5
        assert [one.importance for one in found] == sorted(
            (one.importance for one in found), reverse=True
        )
        assert all(one.sender for one in found), "the sender hop found every one"
        assert any(one.reasons for one in found)

    def test_an_archive_nobody_has_rebuilt_has_nothing_important_in_it(
        self, archived: GraphConfig
    ) -> None:
        """``importance IS NOT NULL`` doing its work: a null sorts first on a
        ``DESC`` here, so without the filter the top of this listing would be
        whichever unscored messages the store happened to visit."""
        assert _reader(archived).important_messages() == ()

    def test_the_topic_comes_back_with_the_words_its_members_used(
        self, derived: GraphConfig
    ) -> None:
        found = _reader(derived).topic_keywords()

        assert len(found) == 1
        assert found[0].message_count == 5
        assert found[0].keywords
        assert "nord" not in found[0].keywords, "the ticket token is not a keyword"

    def test_a_tag_is_offered_the_rest_of_its_topic(
        self, archived: GraphConfig
    ) -> None:
        """The whole of §5.7 read back through the façade: two of the five
        project messages are tagged, the rebuild offers the other three, and
        both the badge and the listing say so."""
        with client.session(archived) as graph:
            repository = TagRepository(graph)
            repository.create("NORD-42", origin=TagOrigin.MANUAL)
            repository.tag_messages(
                "tag:nord-42",
                [corpus.canonical("p1"), corpus.canonical("p2")],
                source=TagSource.MANUAL,
            )
            rebuild_derived(graph, CONFIG)
        reader = _reader(archived)

        counted = reader.suggestion_counts()
        offered = reader.suggestions_for("tag:nord-42")

        assert counted == {"tag:nord-42": 3}
        assert sorted(one.message_id for one in offered) == sorted(
            corpus.canonical(f"p{n}") for n in (3, 4, 5)
        )
        assert [one.score for one in offered] == sorted(
            (one.score for one in offered), reverse=True
        )
        assert {one.method for one in offered} <= {"thread", "topic", "community"}

    def test_a_tag_nothing_was_suggested_for_is_a_zero_and_not_an_absence(
        self, derived: GraphConfig
    ) -> None:
        """A tag with no suggestions and a tag missing from a listing look the
        same to a card, and only one of them is a state a user should see."""
        with client.session(derived) as graph:
            TagRepository(graph).create("Empty", origin=TagOrigin.MANUAL)

        assert _reader(derived).suggestion_counts() == {"tag:empty": 0}
        assert _reader(derived).suggestions_for("tag:empty") == ()


class TestTheCrossCheckOverAHealthyArchive:
    """What the panel must say when nothing is wrong, or it is worth nothing."""

    def test_a_freshly_rebuilt_archive_agrees(self, derived: GraphConfig) -> None:
        found = _reader(derived).co_addressed_agreement()

        assert found.agrees
        assert len(found.matched) == 3
        assert found.edge_overstates == ()

    def test_nothing_is_left_unjudged_when_neither_side_was_cut(
        self, derived: GraphConfig
    ) -> None:
        """Three pairs against a limit of five hundred, so the verdict really
        does cover the whole archive rather than a comfortable prefix."""
        found = _reader(derived).co_addressed_agreement()

        assert found.unjudged == 0
        assert (found.truth_floor, found.edge_floor) == (0, 0)

    def test_an_archive_with_no_rebuild_does_not_agree(
        self, archived: GraphConfig
    ) -> None:
        """The edge is not merely absent, it is *wrong* about an archive that
        has pairs — and the reader says which direction, so nobody reads it as
        a broken write path."""
        found = _reader(archived).co_addressed_agreement()

        assert not found.agrees
        assert len(found.truth_only) == 3
        assert found.edge_overstates == ()


class TestTheCrossCheckOverABrokenArchive:
    """The whole point of the feature: corrupt the edge, and be told."""

    def test_a_count_bumped_by_hand_is_reported_as_a_mismatch(
        self, derived: GraphConfig
    ) -> None:
        """One stored count moved and nothing else touched. The ground truth
        still says what it always said, so the two now disagree about exactly
        one pair — and this is the assertion that makes the panel an oracle for
        the A1 write path rather than a second view of it.
        """
        with client.session(derived) as graph:
            left, right, together = _heaviest_pair(graph)
            _set_count(graph, left, right, together + 7)

        found = _reader(derived).co_addressed_agreement()

        assert not found.agrees
        assert [(one.left_id, one.right_id) for one in found.count_mismatches] == [
            (left, right)
        ]
        assert found.count_mismatches[0].truth == together
        assert found.count_mismatches[0].edge == together + 7

    def test_a_bumped_count_is_called_out_as_the_edge_overstating(
        self, derived: GraphConfig
    ) -> None:
        """No stale rebuild, no ceiling and no distribution list makes the edge
        count *higher* than the archive, so this direction has no innocent
        reading and is the one a reader is shown first."""
        with client.session(derived) as graph:
            left, right, together = _heaviest_pair(graph)
            _set_count(graph, left, right, together + 7)

        found = _reader(derived).co_addressed_agreement()

        assert [(one.left_id, one.right_id) for one in found.edge_overstates] == [
            (left, right)
        ]

    def test_a_count_lowered_by_hand_is_a_mismatch_that_is_not_called_out(
        self, derived: GraphConfig
    ) -> None:
        """The same defect in the other direction, and deliberately quieter: a
        rebuild that has not run since the last import produces exactly this
        shape, and treating it as a bug would cry wolf on every archive with
        mail arriving in it."""
        with client.session(derived) as graph:
            left, right, together = _heaviest_pair(graph)
            _set_count(graph, left, right, together - 1)

        found = _reader(derived).co_addressed_agreement()

        assert not found.agrees
        assert len(found.count_mismatches) == 1
        assert found.edge_overstates == ()

    def test_a_deleted_edge_is_reported_as_missing_from_the_edge(
        self, derived: GraphConfig
    ) -> None:
        """A pair the messages support and the derived layer has lost."""
        with client.session(derived) as graph:
            left, right, _together = _heaviest_pair(graph)
            _drop_pair(graph, left, right)

        found = _reader(derived).co_addressed_agreement()

        assert not found.agrees
        assert [(one.left_id, one.right_id) for one in found.truth_only] == [
            (left, right)
        ]
        assert len(found.matched) == 2

    def test_a_pair_no_message_supports_is_reported_and_called_out(
        self, derived: GraphConfig
    ) -> None:
        """The loudest failure there is. ``revision@`` is only ever a Bcc in
        this corpus, so no message puts it on a recipient list with anybody —
        and a ``CO_ADDRESSED`` edge naming it would materialise exactly the
        confidentiality the header exists to protect.
        """
        with client.session(derived) as graph:
            _invent_pair(graph, corpus.NEWS, corpus.REVISION, INVENTED)

        found = _reader(derived).co_addressed_agreement()

        assert not found.agrees
        assert [(one.left_id, one.right_id) for one in found.edge_only] == [
            (corpus.NEWS, corpus.REVISION)
        ]
        assert found.edge_only[0].edge == INVENTED
        assert [(one.left_id, one.right_id) for one in found.edge_overstates] == [
            (corpus.NEWS, corpus.REVISION)
        ]

    def test_the_pairs_that_are_still_right_are_still_reported_as_matching(
        self, derived: GraphConfig
    ) -> None:
        """A verdict that went red wholesale would be no more useful than one
        that never goes red at all."""
        with client.session(derived) as graph:
            _invent_pair(graph, corpus.NEWS, corpus.REVISION, INVENTED)

        found = _reader(derived).co_addressed_agreement()

        assert len(found.matched) == 3
        assert found.compared == 4
