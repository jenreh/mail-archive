"""What the tag actions do to the annotation layer, and what they refuse to do.

``TagActionsState`` is a mixin, so there is nothing to instantiate until a page
lists it. The host below is that page reduced to nothing — a state with the
mixin on it and no vars of its own — which is exactly what the two real hosts
(the explorer and, in phase 5, the insights page) add to.

Everything under test is a write, and the writes are the reason this file is
long. A tag is the one thing in the archive a rebuild does not recompute, so a
handler that tags the wrong messages, tags them twice with the wrong source, or
swallows a store failure leaves a person with a project label they can no longer
trust — and none of that shows up on screen.
"""

import inspect
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import reflex as rx
from appkit_commons.registry import service_registry

from mailarc_analytics import AnalyticsReader, TagSuggestionRow
from mailarc_core.archive import TagExists, TagOrigin, TagSource, TagStore, TagSummary
from mailarc_ui.tags import (
    PROMOTE_FIELD,
    SuggestionView,
    TagActionsState,
    TagView,
    promote_form,
    short_date,
    suggestion_rows,
    tag_chips,
)

MARCH = datetime(2026, 3, 12, 9, 0, tzinfo=UTC)


class FakeTagStore:
    """The annotation layer in a dict, recording every verb it was asked for.

    Keyed by tag id like the real store, because the bug this is here to catch
    is a handler that tags the right messages under the wrong tag.
    """

    def __init__(self) -> None:
        self.tags: dict[str, TagSummary] = {}
        self.members: dict[str, list[str]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.failing = False

    def _guard(self) -> None:
        if self.failing:
            raise ConnectionError("graph is down")

    def plant(self, name: str, *, members: Sequence[str] = ()) -> TagSummary:
        summary = TagSummary(
            id=f"tag:{name}",
            name=name,
            origin=TagOrigin.MANUAL,
            created_at=MARCH,
            message_count=len(members),
        )
        self.tags[summary.id] = summary
        self.members[summary.id] = list(members)
        return summary

    def list_tags(self) -> tuple[TagSummary, ...]:
        self._guard()
        self.calls.append(("list_tags",))
        return tuple(
            one.model_copy(update={"message_count": len(self.members[one.id])})
            for one in self.tags.values()
        )

    def promote(
        self,
        name: str,
        member_ids: Any,
        *,
        origin: TagOrigin = TagOrigin.TOPIC,
    ) -> TagSummary:
        self._guard()
        members = list(dict.fromkeys(member_ids))
        self.calls.append(("promote", name, tuple(members), origin))
        if f"tag:{name}" in self.tags:
            raise TagExists(f"tag:{name}")
        summary = self.plant(name, members=members)
        return summary.model_copy(update={"origin": origin})

    def tag_messages(
        self,
        tag_id: str,
        ids: Sequence[str],
        *,
        source: TagSource = TagSource.MANUAL,
        at: datetime | None = None,
    ) -> int:
        self._guard()
        self.calls.append(("tag_messages", tag_id, tuple(ids), source))
        held = self.members.setdefault(tag_id, [])
        added = [one for one in ids if one not in held]
        held.extend(added)
        return len(added)

    def untag(self, tag_id: str, ids: Sequence[str]) -> int:
        self._guard()
        self.calls.append(("untag", tag_id, tuple(ids)))
        held = self.members.setdefault(tag_id, [])
        removed = [one for one in ids if one in held]
        self.members[tag_id] = [one for one in held if one not in removed]
        return len(removed)

    def delete(self, tag_id: str) -> bool:
        self._guard()
        self.calls.append(("delete", tag_id))
        self.members.pop(tag_id, None)
        return self.tags.pop(tag_id, None) is not None

    def rename(self, tag_id: str, name: str) -> bool:
        self._guard()
        self.calls.append(("rename", tag_id, name))
        if (held := self.tags.get(tag_id)) is None:
            return False
        self.tags[tag_id] = held.model_copy(update={"name": name})
        return True

    def tags_of(self, ids: Sequence[str]) -> dict[str, tuple[TagSummary, ...]]:
        self._guard()
        self.calls.append(("tags_of", tuple(ids)))
        return {
            one: tuple(tag for tag in self.tags.values() if one in self.members[tag.id])
            for one in ids
        }

    @property
    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeAnalytics:
    """The two reads the tag actions make of the derived layer, and no others.

    A stand-in rather than the real reader over a fake session: what these
    handlers need from analytics is a count and a listing, and scripting the
    catalogue statements behind them would be testing
    ``AnalyticsReader``, which has its own tests.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.offers: dict[str, tuple[TagSuggestionRow, ...]] = {}
        self.failing = False

    def suggestion_counts(self) -> dict[str, int]:
        if self.failing:
            raise ConnectionError("graph is down")
        return dict(self.counts)

    def suggestions_for(
        self, tag_id: str, *, limit: int = 20
    ) -> tuple[TagSuggestionRow, ...]:
        if self.failing:
            raise ConnectionError("graph is down")
        return self.offers.get(tag_id, ())[:limit]


class TagHostState(TagActionsState, rx.State):
    """A page that does nothing but list the mixin.

    ``_cluster_members`` is the hook a real host fills in from whatever it
    knows about the cluster on screen; here it is a dictionary, so a promote
    test says which ids were on offer without drawing a graph.
    """

    offered: dict[str, list[str]] = {}

    async def _cluster_members(self, kind: str, cluster_id: str) -> tuple[str, ...]:
        return tuple(self.offered.get(cluster_id, []))


class TagActionsHostState(TagActionsState, rx.State):
    """A host that fills nothing in — the mixin's own defaults, on their own."""


def _suggestion(message_id: str, score: float, method: str) -> TagSuggestionRow:
    return TagSuggestionRow(
        message_id=message_id,
        subject=f"Subject of {message_id}",
        sent_at=MARCH,
        score=score,
        method=method,
    )


@pytest.fixture
def store() -> FakeTagStore:
    return FakeTagStore()


@pytest.fixture
def analytics() -> FakeAnalytics:
    return FakeAnalytics()


@pytest.fixture
def published(store: FakeTagStore, analytics: FakeAnalytics) -> Iterator[FakeTagStore]:
    """Both halves, left where the composition root would leave them."""
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(TagStore, cast(TagStore, store))
    registry.register_as(AnalyticsReader, cast(AnalyticsReader, analytics))
    yield store
    registry.restore(saved)


@pytest.fixture
def state(published: FakeTagStore) -> TagHostState:
    root = rx.State()
    return cast(
        TagHostState, root.get_substate(TagHostState.get_full_name().split(".")[1:])
    )


async def _fire(handler: Any, state: TagActionsState, *args: Any) -> None:
    """Run one handler the way Reflex runs it — through its wrapped function.

    Both shapes, because the mixin holds both: a setter that validates is
    ordinary and every read or write is a coroutine.
    """
    found = handler.fn(state, *args)
    if inspect.isawaitable(found):
        await found


class TestReadingTheTags:
    async def test_every_tag_arrives_with_the_number_it_is_being_offered(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        store.plant("nord-42", members=["m1", "m2"])
        analytics.counts["tag:nord-42"] = 3

        await _fire(TagHostState.refresh_tags, state)

        assert state.tags == [
            TagView(
                id="tag:nord-42",
                name="nord-42",
                origin=TagOrigin.MANUAL.value,
                message_count=2,
                suggestions=3,
            )
        ]

    async def test_a_tag_no_analysis_had_anything_to_say_about_offers_nothing(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        """Absent from the counts is nought offers, not an unknown number."""
        store.plant("nord-42")

        await _fire(TagHostState.refresh_tags, state)

        assert state.tags[0].suggestions == 0

    async def test_a_graph_that_went_away_is_a_sentence_not_an_exception(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.failing = True

        await _fire(TagHostState.refresh_tags, state)

        assert state.tags == []
        assert "graph is down" in state.tag_error


class TestPromotingAClusterToATag:
    async def test_a_blank_name_is_a_complaint_under_the_box_and_no_write(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        """The whole point of validating on the setter: nothing reaches the
        store, and the message is on the field a person has to fix."""
        state.offered = {"topic:abc": ["m1", "m2"]}

        await _fire(TagHostState.promote, state, "topic", "topic:abc")

        assert state.errors[PROMOTE_FIELD]
        assert "promote" not in store.verbs

    async def test_a_named_cluster_becomes_a_tag_over_its_own_members(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        state.offered = {"topic:abc": ["m1", "m2", "m1"]}
        await _fire(TagHostState.set_promote_name, state, "NORD-42")

        await _fire(TagHostState.promote, state, "topic", "topic:abc")

        assert ("promote", "NORD-42", ("m1", "m2"), TagOrigin.TOPIC) in store.calls
        assert state.promote_name == ""
        assert state.errors == {}

    async def test_a_community_promotes_with_its_own_origin(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        """The origin records what kind of thing was promoted and never which
        one — a cluster id is a digest that the next rebuild mints afresh."""
        state.offered = {"community:abc": ["m1"]}
        await _fire(TagHostState.set_promote_name, state, "Nordlicht")

        await _fire(TagHostState.promote, state, "community", "community:abc")

        assert store.calls[0] == (
            "promote",
            "Nordlicht",
            ("m1",),
            TagOrigin.COMMUNITY,
        )

    async def test_a_cluster_with_nothing_in_it_is_refused_before_the_store(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        """An empty tag looks like a project whose mail has been deleted."""
        await _fire(TagHostState.set_promote_name, state, "NORD-42")

        await _fire(TagHostState.promote, state, "topic", "topic:gone")

        assert "promote" not in store.verbs
        assert state.errors[PROMOTE_FIELD]

    async def test_a_name_already_taken_lands_on_the_box_rather_than_raising(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("NORD-42")
        state.offered = {"topic:abc": ["m1"]}
        await _fire(TagHostState.set_promote_name, state, "NORD-42")

        await _fire(TagHostState.promote, state, "topic", "topic:abc")

        assert state.errors[PROMOTE_FIELD]
        assert state.promote_name == "NORD-42", "the name is kept to be corrected"

    async def test_a_promoted_tag_is_in_the_listing_afterwards(
        self, state: TagHostState
    ) -> None:
        state.offered = {"topic:abc": ["m1"]}
        await _fire(TagHostState.set_promote_name, state, "NORD-42")

        await _fire(TagHostState.promote, state, "topic", "topic:abc")

        assert [one.name for one in state.tags] == ["NORD-42"]


class TestAcceptingWhatWasSuggested:
    async def test_the_offers_for_one_tag_are_read_strongest_first(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        store.plant("nord-42")
        analytics.offers["tag:nord-42"] = (
            _suggestion("m9", 0.8, "thread"),
            _suggestion("m8", 0.4, "topic"),
        )

        await _fire(TagHostState.show_suggestions, state, "tag:nord-42")

        assert state.suggestion_tag == "tag:nord-42"
        assert [one.message_id for one in state.suggestions] == ["m9", "m8"]
        assert state.suggestions[0].score_label == "0.80"

    async def test_accepting_one_records_a_decision_a_person_made(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        """``accepted`` and never ``auto``: a click is a person deciding."""
        store.plant("nord-42")
        analytics.offers["tag:nord-42"] = (_suggestion("m9", 0.8, "thread"),)
        await _fire(TagHostState.show_suggestions, state, "tag:nord-42")

        await _fire(TagHostState.accept_suggestion, state, "tag:nord-42", "m9")

        assert (
            "tag_messages",
            "tag:nord-42",
            ("m9",),
            TagSource.ACCEPTED,
        ) in store.calls
        assert state.suggestions == [], "an accepted offer stops being one"

    async def test_accepting_all_of_them_is_one_write_with_every_id(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        store.plant("nord-42")
        analytics.offers["tag:nord-42"] = (
            _suggestion("m9", 0.8, "thread"),
            _suggestion("m8", 0.4, "topic"),
        )

        await _fire(TagHostState.accept_all, state, "tag:nord-42")

        assert (
            "tag_messages",
            "tag:nord-42",
            ("m9", "m8"),
            TagSource.ACCEPTED,
        ) in store.calls

    async def test_accepting_all_of_nothing_writes_nothing(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("nord-42")

        await _fire(TagHostState.accept_all, state, "tag:nord-42")

        assert "tag_messages" not in store.verbs


class TestTaggingOneMessageByHand:
    async def test_tagging_records_the_manual_source(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("nord-42")

        await _fire(TagHostState.tag_message, state, "tag:nord-42", "m1")

        assert (
            "tag_messages",
            "tag:nord-42",
            ("m1",),
            TagSource.MANUAL,
        ) in store.calls

    async def test_untagging_takes_the_membership_and_leaves_the_message(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("nord-42", members=["m1"])

        await _fire(TagHostState.untag_message, state, "tag:nord-42", "m1")

        assert ("untag", "tag:nord-42", ("m1",)) in store.calls
        assert store.members["tag:nord-42"] == []

    async def test_the_tags_one_message_wears_are_read_for_the_chips(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("nord-42", members=["m1"])
        store.plant("invoices")

        await _fire(TagHostState.read_message_tags, state, "m1")

        assert [one.name for one in state.message_tags] == ["nord-42"]

    async def test_a_store_that_refused_says_so_instead_of_raising(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.failing = True

        await _fire(TagHostState.tag_message, state, "tag:nord-42", "m1")

        assert "graph is down" in state.tag_error


class TestRenamingAndDeleting:
    async def test_renaming_keeps_the_id_and_changes_the_name(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("nord-42")

        await _fire(TagHostState.rename_tag, state, "tag:nord-42", "NORD-42")

        assert [one.name for one in state.tags] == ["NORD-42"]
        assert state.tags[0].id == "tag:nord-42"

    async def test_a_blank_rename_is_refused(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("nord-42")

        await _fire(TagHostState.rename_tag, state, "tag:nord-42", "   ")

        assert "rename" not in store.verbs

    async def test_deleting_a_tag_takes_it_out_of_the_listing(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        store.plant("nord-42")
        store.plant("invoices")

        await _fire(TagHostState.delete_tag, state, "tag:nord-42")

        assert [one.id for one in state.tags] == ["tag:invoices"]

    async def test_deleting_the_tag_whose_offers_are_open_closes_them(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        """Rows about a tag that no longer exists are rows nothing can accept."""
        store.plant("nord-42")
        analytics.offers["tag:nord-42"] = (_suggestion("m9", 0.8, "thread"),)
        await _fire(TagHostState.show_suggestions, state, "tag:nord-42")

        await _fire(TagHostState.delete_tag, state, "tag:nord-42")

        assert state.suggestions == []
        assert state.suggestion_tag == ""


class TestWhatTheComponentsBranchOn:
    """The two computed vars a panel switches its whole body on."""

    async def test_a_tag_listing_and_an_offer_listing_are_asked_separately(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        assert state.has_tags is False
        assert state.has_suggestions is False
        store.plant("nord-42")
        analytics.offers["tag:nord-42"] = (_suggestion("m9", 0.8, "thread"),)

        await _fire(TagHostState.refresh_tags, state)
        await _fire(TagHostState.show_suggestions, state, "tag:nord-42")

        assert state.has_tags is True
        assert state.has_suggestions is True


class TestWhenHalfTheArchiveIsMissing:
    async def test_tags_are_still_listed_when_nothing_has_been_derived(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        """R8: a fresh archive holds no ``SUGGESTED`` edge at all, and a page
        that refused to show a person their own tags over it would be answering
        the wrong question."""
        store.plant("nord-42")
        analytics.failing = True

        await _fire(TagHostState.refresh_tags, state)

        assert [one.name for one in state.tags] == ["nord-42"]
        assert state.tags[0].suggestions == 0
        assert state.tag_error == ""

    async def test_offers_that_could_not_be_read_are_a_sentence(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        store.plant("nord-42")
        analytics.failing = True

        await _fire(TagHostState.show_suggestions, state, "tag:nord-42")

        assert "graph is down" in state.tag_error

    async def test_accepting_all_of_offers_that_failed_writes_nothing(
        self, state: TagHostState, store: FakeTagStore, analytics: FakeAnalytics
    ) -> None:
        analytics.failing = True

        await _fire(TagHostState.accept_all, state, "tag:nord-42")

        assert "tag_messages" not in store.verbs
        assert "graph is down" in state.tag_error

    async def test_a_refused_untag_leaves_the_chips_alone(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        """Re-reading them after a write that did not happen would show the
        membership as gone."""
        store.plant("nord-42", members=["m1"])
        store.failing = True

        await _fire(TagHostState.untag_message, state, "tag:nord-42", "m1")

        assert "graph is down" in state.tag_error
        assert store.members["tag:nord-42"] == ["m1"]

    async def test_reading_the_tags_of_no_message_asks_nothing(
        self, state: TagHostState, store: FakeTagStore
    ) -> None:
        await _fire(TagHostState.read_message_tags, state, "")

        assert state.message_tags == []
        assert "tags_of" not in store.verbs

    async def test_the_mixin_promotes_nothing_when_no_host_answers(
        self, published: FakeTagStore
    ) -> None:
        """The default hook, which is what makes an unfilled host safe rather
        than a tag over the wrong messages."""
        root = rx.State()
        bare = cast(
            TagActionsHostState,
            root.get_substate(TagActionsHostState.get_full_name().split(".")[1:]),
        )
        await _fire(TagActionsHostState.set_promote_name, bare, "NORD-42")

        await _fire(TagActionsHostState.promote, bare, "topic", "topic:abc")

        assert bare.errors[PROMOTE_FIELD]
        assert "promote" not in published.verbs


class TestTheComponentsDraw:
    """Built rather than merely imported: a prop Mantine does not have only
    shows up when the component is constructed."""

    def test_the_chips_build(self) -> None:
        assert isinstance(tag_chips(TagHostState, "m1"), rx.Component)

    def test_the_promote_form_builds(self) -> None:
        assert isinstance(
            promote_form(TagHostState, "topic", "topic:abc"), rx.Component
        )

    def test_the_suggestion_rows_build(self) -> None:
        assert isinstance(suggestion_rows(TagHostState), rx.Component)


class TestTheViews:
    def test_a_suggestion_carries_its_score_twice(self) -> None:
        """Once as a bar takes it and once as it is defined — a bar with no
        number beside it is a ranking nobody can argue with."""
        view = SuggestionView.of(_suggestion("m9", 0.42, "topic"))

        assert view.score == pytest.approx(42.0)
        assert view.score_label == "0.42"
        assert view.method == "topic"

    def test_a_date_nobody_can_print_is_an_empty_cell(self) -> None:
        """A ``Date:`` header is whatever a sender wrote and the archive
        range-checks nothing, so one archived mail from the year 9999 raises
        ``OverflowError`` out of ``astimezone`` east of UTC — and used to take
        a whole listing with it."""
        assert short_date(None) == ""
        assert short_date(datetime.max.replace(tzinfo=UTC)) in ("", "31 Dec 9999")

    def test_a_suggestion_with_no_subject_still_names_itself(self) -> None:
        view = SuggestionView.of(
            TagSuggestionRow(message_id="m9", score=0.1, method="")
        )

        assert view.subject == "(no subject)"
        assert view.method == "unknown"
        assert view.when == ""

    def test_a_tag_with_no_colour_carries_an_empty_string(self) -> None:
        """``None`` would reach the browser as ``null`` and colour a dot with
        the word."""
        view = TagView.of(TagSummary(id="tag:x", name="x"))

        assert view.color == ""
