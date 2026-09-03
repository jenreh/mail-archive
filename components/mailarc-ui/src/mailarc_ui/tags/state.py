"""Tagging, as the half of a page that is the same wherever it appears.

A mixin for :class:`~mailarc_ui.message_detail.state.MessageDetailState`'s
reason: Reflex copies a mixin's vars and handlers into every concrete state that
lists it, so the graph explorer and the insights page each get their **own** tag
listing, their own open suggestions and their own form errors rather than
sharing one substate and with it one half-typed name.

What a concrete state has to bring is the cluster. :meth:`~TagActionsState._cluster_members`
is the one hook: the mixin knows how to turn a name and a set of message ids
into a tag, and only the host knows which messages are in the topic on screen.
The default answers with nothing, which is refused before any write — a tag
created over no messages looks like a project whose mail has been deleted.

**Nothing here is a background handler**, the same rule the reading pane keeps.
A background task takes the state lock and yields, and a handler copied into two
unrelated states would be two tasks writing the same vars under two locks. Each
of these is one write or one listing; the host's own expensive read is where a
background task belongs.

Two layers, always. ``TAGGED`` is annotation on ground truth and belongs to
``mailarc-core``; ``SUGGESTED`` is derived, and the next rebuild deletes and
computes it again (R8). Accepting a suggestion therefore *writes* through the
tag store and never touches the suggestion — the offer stops being made because
the message now wears the tag.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import reflex as rx

from mailarc_core.archive import TagExists, TagOrigin, TagSource
from mailarc_ui.kit import FieldErrors
from mailarc_ui.tags.model import (
    NAME_TAKEN,
    NOTHING_TO_PROMOTE,
    PROMOTE_FIELD,
    SuggestionView,
    TagView,
)
from mailarc_ui.tags.reads import analytics_reader, answered, tag_store

logger = logging.getLogger(__name__)

SUGGESTION_LIMIT = 50
"""How many offers one tag's panel shows.

A suggestion is accepted one at a time or all at once, and a list longer than a
screenful is one nobody reads before pressing "Accept all".
"""

NO_NAME = "A tag needs a name."
"""What a rename with nothing in it says. Not a field complaint: the rename box
belongs to a row rather than to the form the errors map is drawn under."""

_ORIGINS: dict[str, TagOrigin] = {
    TagOrigin.TOPIC.value: TagOrigin.TOPIC,
    TagOrigin.COMMUNITY.value: TagOrigin.COMMUNITY,
}
"""Which kind of cluster was promoted, by the word the canvas uses for it.

The *kind* and never the id: a ``Topic.id`` is a digest of its members and is a
different string after every rebuild, so storing one would be storing a
reference that goes stale by design. Anything else promotes as ``manual``.
"""


async def read_tags() -> tuple[list[TagView], str]:
    """Every tag, with how many messages each is being offered.

    One unit of work rather than two reads, because the two halves are one row
    on screen and a listing without its badges would have to be rendered twice.

    A count that could not be read is nought offers rather than no listing: an
    archive nobody has rebuilt holds no ``SUGGESTED`` edge at all (R8), and the
    tags are still what a person made by hand.
    """

    def work() -> list[TagView]:
        summaries = tag_store().list_tags()
        counts = _suggestion_counts()
        return [TagView.of(one, counts.get(one.id, 0)) for one in summaries]

    return await answered(work, "read the tags", [])


async def read_suggestions(tag_id: str) -> tuple[list[SuggestionView], str]:
    """What one tag is being offered, strongest case first."""

    def work() -> list[SuggestionView]:
        rows = analytics_reader().suggestions_for(tag_id, limit=SUGGESTION_LIMIT)
        return [SuggestionView.of(one) for one in rows]

    return await answered(work, f"read the suggestions for {tag_id}", [])


def _suggestion_counts() -> dict[str, int]:
    """The badge on each tag, or none of them.

    Swallowed on purpose, and only here: the derived layer is the half of this
    listing a fresh archive simply does not have, and a page that refused to
    show a person their own tags because nothing had been derived yet would be
    answering the wrong question.
    """
    try:
        return analytics_reader().suggestion_counts()
    except Exception:
        logger.debug("No suggestion counts — nothing derived yet, or no graph")
        return {}


class TagActionsState(FieldErrors, rx.State, mixin=True):
    """What a page can do to the annotation layer, wherever it draws it."""

    tags: list[TagView] = []
    """Every tag in the archive, with its suggestion badge."""

    message_tags: list[TagView] = []
    """What the one open message wears.

    Read per message rather than filtered out of :attr:`tags`: a membership is
    an edge, and the listing above carries counts rather than the edges behind
    them.
    """

    suggestions: list[SuggestionView] = []
    suggestion_tag: str = ""
    """Which tag :attr:`suggestions` belongs to.

    Separate from the rows for the reason the import panel keeps its job id
    separate from its last reading: a read that came back empty must not make
    the panel forget what it was showing.
    """

    promote_name: str = ""
    tag_error: str = ""
    """What the annotation layer said when it refused. Never a form complaint —
    those go under the field, in :attr:`~mailarc_ui.kit.FieldErrors.errors`."""

    tag_notice: str = ""
    """One quiet sentence about what just happened — never a page error."""

    tagging: bool = False
    """Whether a write is in flight, so a button can say so."""

    @rx.var
    def has_tags(self) -> bool:
        return len(self.tags) > 0

    @rx.var
    def has_suggestions(self) -> bool:
        return len(self.suggestions) > 0

    @rx.event
    async def refresh_tags(self) -> None:
        """Read the tags again — after a write, and on a page's load."""
        self.tags, self.tag_error = await read_tags()

    @rx.event
    def set_promote_name(self, value: str) -> None:
        """Remember what is being typed, and check it as it is typed.

        Validating on the setter rather than at submit is the whole reason the
        complaints live in state: a message that appears while a person types
        and leaves when they fix it is worth having, and a form that stays
        silent until Save is a form people submit twice.
        """
        self.promote_name = value
        self._required(PROMOTE_FIELD, value)

    @rx.event
    async def promote(self, kind: str, cluster_id: str) -> None:
        """Turn the cluster on screen into a tag its members wear.

        Two things are refused before the store is asked: a blank name, and a
        cluster with nothing in it. Both land under the field rather than in
        :attr:`tag_error`, because both are about what was typed rather than
        about the archive.
        """
        name = self.promote_name.strip()
        if not self._required(PROMOTE_FIELD, name):
            return
        members = await self._cluster_members(kind, cluster_id)
        if not members:
            self._fail(PROMOTE_FIELD, NOTHING_TO_PROMOTE)
            return
        origin = _ORIGINS.get(kind, TagOrigin.MANUAL)
        made, failure = await self._written(
            lambda: tag_store().promote(name, members, origin=origin),
            f"promote {cluster_id}",
        )
        self.tag_error = failure
        if made is None:
            return
        self.promote_name = ""
        self._pass(PROMOTE_FIELD)
        self.tag_notice = f"Tagged {len(members)} messages as {name}."
        await self.refresh_tags()

    @rx.event
    async def tag_message(self, tag_id: str, message_id: str) -> None:
        """Put one message under one tag, by hand.

        ``manual``, and the store never overwrites it: a message tagged by hand
        and later suggested again keeps the decision a person made.
        """
        if await self._tag(tag_id, [message_id], TagSource.MANUAL):
            await self.read_message_tags(message_id)

    @rx.event
    async def untag_message(self, tag_id: str, message_id: str) -> None:
        """Take the membership and leave the message."""
        _, failure = await self._written(
            lambda: tag_store().untag(tag_id, [message_id]),
            f"untag {message_id}",
        )
        self.tag_error = failure
        if not failure:
            await self.refresh_tags()
            await self.read_message_tags(message_id)

    @rx.event
    async def read_message_tags(self, message_id: str) -> None:
        """What this one message wears — the chips beside the reading pane."""
        if not message_id:
            self.message_tags = []
            return

        def work() -> list[TagView]:
            found = tag_store().tags_of([message_id])
            return [TagView.of(one) for one in found.get(message_id, ())]

        self.message_tags, failure = await answered(
            work, f"read the tags of {message_id}", []
        )
        if failure:
            self.tag_error = failure

    @rx.event
    async def show_suggestions(self, tag_id: str) -> None:
        """Open what one tag is being offered."""
        self.suggestion_tag = tag_id
        self.suggestions, failure = await read_suggestions(tag_id)
        if failure:
            self.tag_error = failure

    @rx.event
    async def accept_suggestion(self, tag_id: str, message_id: str) -> None:
        """Take one offer. ``accepted``, because a click is a person deciding."""
        if not await self._tag(tag_id, [message_id], TagSource.ACCEPTED):
            return
        self.suggestions = [
            one for one in self.suggestions if one.message_id != message_id
        ]

    @rx.event
    async def accept_all(self, tag_id: str) -> None:
        """Take every offer this tag has, in one write.

        Read afresh rather than taken off :attr:`suggestions`: the panel may be
        showing another tag's offers, or none at all, and "accept everything
        suggested for this tag" has to mean the same thing either way.
        """
        offered, failure = await read_suggestions(tag_id)
        if failure:
            self.tag_error = failure
            return
        if not offered:
            self.tag_notice = "Nothing is being suggested for that tag."
            return
        if not await self._tag(
            tag_id, [one.message_id for one in offered], TagSource.ACCEPTED
        ):
            return
        self.tag_notice = f"Accepted {len(offered)} suggestions."
        if self.suggestion_tag == tag_id:
            self.suggestions = []

    @rx.event
    async def rename_tag(self, tag_id: str, name: str) -> None:
        """Give a tag a different name. A blank one is refused."""
        wanted = name.strip()
        if not wanted:
            self.tag_notice = NO_NAME
            return
        _, failure = await self._written(
            lambda: tag_store().rename(tag_id, wanted), f"rename {tag_id}"
        )
        self.tag_error = failure
        await self.refresh_tags()

    @rx.event
    async def delete_tag(self, tag_id: str) -> None:
        """Remove a tag; the messages it named keep everything else.

        Whatever it was being offered goes with it — rows about a tag that no
        longer exists are rows nothing can accept.
        """
        _, failure = await self._written(
            lambda: tag_store().delete(tag_id), f"delete {tag_id}"
        )
        self.tag_error = failure
        if self.suggestion_tag == tag_id:
            self.suggestions = []
            self.suggestion_tag = ""
        await self.refresh_tags()

    async def _cluster_members(self, kind: str, cluster_id: str) -> tuple[str, ...]:
        """Which messages are in the cluster a host is offering to promote.

        Nothing, unless the host says otherwise. The mixin cannot know: a topic
        on the explorer is a subgraph the page already holds and a topic on the
        insights page is a row whose members would have to be read, and neither
        belongs in a module that knows only about tags.
        """
        logger.debug("No host answered what is in %s (%s)", cluster_id, kind)
        return ()

    async def _tag(self, tag_id: str, ids: list[str], source: TagSource) -> bool:
        """Write one batch of memberships, and say whether it landed."""
        written, failure = await self._written(
            lambda: tag_store().tag_messages(tag_id, ids, source=source),
            f"tag {len(ids)} messages",
        )
        self.tag_error = failure
        if written is None:
            return False
        await self.refresh_tags()
        return True

    async def _written[T](
        self, work: Callable[[], T], what: str
    ) -> tuple[T | None, str]:
        """One write, off the event loop, with a refusal as its answer.

        ``None`` and a sentence rather than an exception, because every one of
        these is somebody pressing a button: a graph that went away is a state
        this page has to render.

        ``TagExists`` is the one refusal that is not about the archive — it is
        about the name in the box — so it lands under the field and leaves the
        sentence empty.
        """
        self.tagging = True
        try:
            return await asyncio.to_thread(work), ""
        except TagExists:
            self._fail(PROMOTE_FIELD, NAME_TAKEN)
            return None, ""
        except Exception as error:
            logger.exception("Could not %s", what)
            return None, str(error) or type(error).__name__
        finally:
            self.tagging = False
