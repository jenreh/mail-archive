"""What the second phase of analysis found, as the insights page prints it.

The circles, the scored messages, the topic keywords and the annotation layer —
the four listings §5 added and §8 put on this page. Its sibling
:mod:`test_ui_insights_state` holds the cross-check and everything the first
phase wrote; the split is by *finding* and not by module, because these four
share one set of planted rows and one question.

Both read the real :class:`~mailarc_analytics.AnalyticsReader` over the fake
session in :mod:`insights_archive`, so a test that plants rows for
``TOP_COMMUNITIES`` also proves the reader ran that statement and not another.
The tags are the exception and come from a stand-in store: a ``Tag`` belongs to
``mailarc-core`` and only its *count* is derived, which is the split that makes
this card two reads rather than one.

R8 is the claim worth stating up front. ``SUGGESTED`` is recomputed by every
rebuild, so a tag made between two of them is being offered nothing — a true
state that looks exactly like a broken analysis, and the card has to say which
of the two it is.
"""

import json

import pytest
from insights_archive import (
    AUGUST,
    MARCH,
    FakeTags,
    graph,
    published,
    tags,
)

from mailarc_analytics import AnalyticsReader
from mailarc_analytics.queries import catalog
from mailarc_ui.insights import (
    AnalyticsInsightsState,
    communities_card,
    important_card,
    tags_card,
    topics_card,
)
from mailarc_ui.shell import routes

__all__ = ["graph", "published", "tags"]
"""pytest collects a fixture off the importing module's namespace, so the three
are imported to be used; ``__all__`` is what stops ruff removing them again."""


@pytest.fixture
def state(published: AnalyticsReader) -> AnalyticsInsightsState:
    """The state under test, over the published reader and tag store."""
    return AnalyticsInsightsState()


async def _load(state: AnalyticsInsightsState) -> None:
    """Invoke the page's ``on_load`` the way Reflex invokes a background task.

    ``load`` reads the whole archive and holds the state lock only around its
    two mutations, so Reflex refuses a direct call on it; going through the
    ``EventHandler``'s wrapped function is the same code the app runs.
    """
    await AnalyticsInsightsState.load.fn(state)


class TestTheCircles:
    """B3 as a listing: which correspondents actually form a group.

    A circle is not a recurring group. A group is one exact set of people a
    message was addressed to, hashed; a circle is what label propagation found
    over the whole co-addressing graph, so its members need never have shared a
    single mail. The two panels sit beside each other for that reason.
    """

    async def test_a_circle_is_the_row_the_catalogue_answered_made_printable(
        self, state
    ) -> None:
        await _load(state)

        first, _ = state.communities
        assert first.id == "community:" + "e" * 32
        assert first.key == "e" * 12 + "…"
        assert first.label == "kunde.example"
        assert first.size == 5
        assert first.message_count == 41
        assert first.method == "lpa"
        assert first.span == (
            f"{MARCH.astimezone():%d.%m.%y} – {AUGUST.astimezone():%d.%m.%y}"
        )
        assert state.communities_error == ""

    async def test_a_circle_with_no_common_domain_says_so(self, state) -> None:
        """An empty cell reads as a label that went missing, which is a
        different claim from "these people share no domain"."""
        await _load(state)

        _, second = state.communities
        assert second.label == "(no common domain)"
        assert second.method == "unknown"
        assert second.span == ""

    async def test_the_whole_id_travels_so_a_link_can_name_it(self, state) -> None:
        """The key is the readable end; the pill into ``/graph`` needs all of
        it, the same reason :class:`TopicView` carries both."""
        await _load(state)

        assert [one.id for one in state.communities] == [
            "community:" + "e" * 32,
            "community:" + "f" * 32,
        ]


class TestWhatProbablyMatters:
    """B2: a score nobody can argue with is not what §1.1 asked for."""

    async def test_an_important_message_carries_the_reasons_for_its_score(
        self, state
    ) -> None:
        await _load(state)

        first, _ = state.important
        assert first.id == "<nord-42@example.com>"
        assert first.subject == "Angebot NORD-42"
        assert first.sender == "anna@example.com"
        assert first.when == f"{AUGUST.astimezone():%d.%m.%y}"
        assert first.score == 82.0, "0..100, which is what a bar takes"
        assert first.score_label == "0.82", "and 0..1, which is how it is defined"
        assert first.reasons == ["replied by you", "addressed directly"]
        assert state.important_error == ""

    async def test_a_message_the_scorer_had_no_reason_for_is_still_a_row(
        self, state
    ) -> None:
        """A stored ``importance`` with no ``importance_reasons`` beside it is
        a scored message, not an unscored one — the statement already filters
        the unscored out — so it keeps its bar and shows no chips."""
        await _load(state)

        _, second = state.important
        assert second.subject == "(no subject)"
        assert second.sender == ""
        assert second.reasons == []
        assert second.score == 40.0


class TestWhatTheTopicsAreAbout:
    """The keyword column, and the join behind it.

    ``TOPIC_BREAKDOWN`` answers once per topic *per signal* and
    ``TOPIC_KEYWORDS`` once per topic, so the words have to be looked up by id
    rather than zipped onto the listing.
    """

    async def test_a_topic_wears_the_words_its_own_members_used(self, state) -> None:
        await _load(state)

        strong, weak = state.topics
        assert strong.keywords == ["rechnung", "swiftscan"]
        assert weak.keywords == [], "the keyword stage never reached this one"

    async def test_the_words_are_per_topic_and_not_per_signal(
        self, state, graph
    ) -> None:
        """One topic joined two ways is two rows and one set of keywords.

        Zipping the two listings positionally would put the second topic's
        words on the first topic's second signal — a mislabelling that looks
        entirely plausible on screen.
        """
        graph.rows(
            catalog.TOPIC_BREAKDOWN,
            ["id", "label", "method", "messages"],
            [
                ["topic:" + "c" * 32, "rechnung swiftscan", "ref", 6],
                ["topic:" + "c" * 32, "rechnung swiftscan", "thread", 2],
            ],
        )

        await _load(state)

        assert [one.keywords for one in state.topics] == [
            ["rechnung", "swiftscan"],
            ["rechnung", "swiftscan"],
        ]
        assert graph.asked.count(catalog.TOPIC_KEYWORDS) == 1

    async def test_a_keyword_read_that_dies_is_the_topics_panels_own_sentence(
        self, state, graph
    ) -> None:
        """The clusters and their words are one row on screen, so they are one
        unit of work and one error string — never half a table."""
        graph.failing = {catalog.TOPIC_KEYWORDS}

        await _load(state)

        assert state.topics_error == "graph is down"
        assert state.topics == []
        assert len(state.groups) == 2, "and only that panel"
        assert state.busy is False


class TestTheTagsPanel:
    """The annotation layer on this page, and R8's sentence under it.

    ``SUGGESTED`` is derived: every rebuild deletes it and computes it again.
    So a tag made between two rebuilds is being offered nothing, and a card
    that showed that as a bare zero would look broken rather than early.
    """

    async def test_every_tag_arrives_with_what_it_is_being_offered(self, state) -> None:
        await _load(state)

        assert [
            (one.id, one.name, one.message_count, one.suggestions) for one in state.tags
        ] == [
            ("tag:nord-42", "nord-42", 7, 4),
            ("tag:steuer", "steuer", 2, 0),
        ]
        assert state.tag_error == ""
        assert state.loading_tags is False

    async def test_a_tag_nothing_is_suggested_for_is_a_zero_and_not_an_absence(
        self, state, tags
    ) -> None:
        """Both halves come from different layers — the tag from
        ``mailarc-core``, the count from the derived layer — and a tag missing
        from the counts is still a tag."""
        tags.plant("privat", message_count=1)

        await _load(state)

        assert [one.id for one in state.tags][-1] == "tag:privat"
        assert state.tags[-1].suggestions == 0

    async def test_tags_that_no_rebuild_has_seen_yet_ask_for_one(
        self, state, graph
    ) -> None:
        """R8, as the var the card's notice hangs off."""
        graph.rows(
            catalog.SUGGESTION_COUNTS,
            ["id", "name", "suggestions"],
            [["tag:nord-42", "nord-42", 0], ["tag:steuer", "steuer", 0]],
        )

        await _load(state)

        assert state.tags_await_a_rebuild is True

    async def test_one_tag_with_an_offer_is_not_a_page_asking_for_a_rebuild(
        self, state
    ) -> None:
        await _load(state)

        assert state.tags_await_a_rebuild is False

    async def test_an_archive_with_no_tags_at_all_is_not_a_rebuild_problem(
        self, state, tags: FakeTags
    ) -> None:
        """Nothing to suggest for is not the same state as nothing suggested,
        and the card says something else entirely about it."""
        tags.summaries.clear()

        await _load(state)

        assert state.tags == []
        assert state.tags_await_a_rebuild is False

    async def test_a_store_that_went_away_is_this_cards_error_and_no_others(
        self, state, tags: FakeTags
    ) -> None:
        tags.failing = True

        await _load(state)

        assert state.tag_error == "graph is down"
        assert state.tags == []
        assert len(state.topics) == 2
        assert state.busy is False


class TestTheNewCardsDraw:
    """What each of the four listings has to have on it.

    A prop appkit_mantine does not have only shows up when a component is
    built, and the parametrized build-and-render next door cannot fail on
    *content* — so the two things a reader acts on are asserted here: where the
    row's pill goes, and whether the tags card explains its own zeroes.
    """

    @pytest.mark.parametrize(
        ("build", "view"),
        [
            (topics_card, "topic"),
            (communities_card, "community"),
            (tags_card, "tag"),
            (important_card, "message"),
        ],
    )
    def test_every_listing_offers_the_explorer(self, build, view) -> None:
        """Each card's rows carry a pill into ``/graph``, rooted at the row.

        Asserted per card and on the ``?view=`` it names, because the four
        links differ only in that word: a communities pill copied from the
        topics card would send a reader to a topic id that no ``Topic``
        answers to, and the explorer would say the cluster was recomputed
        rather than that the link is wrong.
        """
        rendered = json.dumps(build().render(), default=str)

        assert f"{routes.GRAPH}?view={view}&id=" in rendered

    def test_the_tags_card_says_why_a_new_tag_is_offered_nothing(self) -> None:
        """R8 on screen: ``SUGGESTED`` is recomputed by every rebuild, so a
        tag made since the last one has none — which has to read as early
        rather than as broken, and the rebuild is offered in the same breath."""
        rendered = json.dumps(tags_card().render(), default=str)

        assert "tags_await_a_rebuild" in rendered, "the notice hangs off nothing"
        assert "rebuild" in rendered.lower()
        assert "start_rebuild" in rendered, "and the button that ends the wait"

    def test_the_tags_card_wires_both_of_its_writes(self) -> None:
        """Accept all and Delete, on the row they are drawn beside.

        A handler is a prop and reaches the render, so a pill whose ``on_click``
        was dropped in a rearrangement shows up here rather than as a button
        that quietly does nothing."""
        rendered = json.dumps(tags_card().render(), default=str)

        assert "accept_all" in rendered
        assert "delete_tag" in rendered
