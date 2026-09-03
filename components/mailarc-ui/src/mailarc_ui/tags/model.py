"""A tag and a suggestion, as a chip and a row print them.

Projections, not nodes: nothing here holds a graph node or a live session, and
nothing here does any I/O. What each class adds to the row it was built from is
the rendering the browser cannot do — a date in words, a score twice, a colour
that is a string rather than ``None``.

The one decision worth stating is that **a suggestion carries its score twice**.
Once between nought and a hundred, which is what a bar takes, and once as it is
defined, which is what a reader argues with. A bar on its own is a ranking
nobody can check, and the whole reason a suggestion is arithmetic over group
membership rather than a model's opinion is that every term of it can be named.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from mailarc_analytics import TagSuggestionRow
from mailarc_core.archive import TagOrigin, TagSummary

logger = logging.getLogger(__name__)

PROMOTE_FIELD = "promote_name"
"""The key the promote form's complaint is filed under.

Named once because two files have to agree on it: the state writes
``errors[PROMOTE_FIELD]`` and the box reads it back. A literal in both places
is the partial binding ``test_ui_forms_are_one_look`` exists to catch.
"""

NAME_TAKEN = "A tag with that name already exists."
"""What a duplicate says. The store raises ``TagExists`` and this is the same
fact in the words the box under the field can print."""

NOTHING_TO_PROMOTE = "There is nothing in this cluster to tag."
"""And what an empty one says.

A tag created over no messages is worse than no tag at all: it looks like a
project whose mail has been deleted.
"""


class TagView(BaseModel):
    """One tag as a chip and a listing show it.

    :attr:`color` is a string and never ``None``: an absent colour reaches the
    browser as ``null``, and a dot painted with ``null`` is painted with the
    word. Empty means "this tag has no colour of its own", which is what the
    component draws its default for.

    :attr:`suggestions` is filled by the listing that counts and is nought
    everywhere else — the same contract
    :class:`~mailarc_core.archive.model.TagSummary` keeps for its own count.
    """

    model_config = ConfigDict(frozen=True)

    id: str = ""
    name: str = ""
    color: str = ""
    origin: str = TagOrigin.MANUAL.value
    """A :class:`~mailarc_core.archive.model.TagOrigin` value as a plain string,
    because what reads it is an ``rx.match`` in the browser."""

    message_count: int = 0
    suggestions: int = 0

    @classmethod
    def of(cls, summary: TagSummary, suggestions: int = 0) -> TagView:
        """One stored tag, plus how many messages are being offered to it."""
        return cls(
            id=summary.id,
            name=summary.name or summary.id,
            color=summary.color or "",
            origin=summary.origin.value,
            message_count=summary.message_count,
            suggestions=suggestions,
        )


class SuggestionView(BaseModel):
    """A message an analysis thinks a tag might want, and the case for it.

    Never a membership: ``TAGGED`` records what a person decided, and this is a
    ``SUGGESTED`` edge that the next rebuild deletes and computes again. R8 is
    why :attr:`method` is on the row — "these two answer each other" and "these
    two are in the same circle" are not the same claim, and somebody accepting
    one should see which was made.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str = ""
    subject: str = ""
    when: str = ""
    score: float = 0.0
    """``0..100``, which is what a bar takes."""

    score_label: str = "0.00"
    """The same number as it is defined, ``0..1``."""

    method: str = ""

    @classmethod
    def of(cls, row: TagSuggestionRow) -> SuggestionView:
        return cls(
            message_id=row.message_id,
            subject=row.subject or "(no subject)",
            when=short_date(row.sent_at),
            score=round(row.score * 100, 1),
            score_label=f"{row.score:.2f}",
            method=row.method or "unknown",
        )


def short_date(value: datetime | None) -> str:
    """One date the way a row prints one, in the reader's own zone.

    Total, for the reason :func:`mailarc_ui.insights.model.short_date` is: a
    ``Date:`` header is whatever a sender wrote and the archive range-checks
    nothing, so ``Date: Fri, 31 Dec 9999 23:59:59 +0000`` parses, gets stored,
    comes back an aware datetime — and then ``astimezone()`` raises
    ``OverflowError`` in every zone east of UTC. One archived spam mail must not
    take a whole listing down with it, so a date that cannot be printed is an
    empty cell.

    A copy rather than an import because the insights page will host this mixin
    (phase 5) and the import would close a cycle. Six lines is the cheaper of
    the two.
    """
    if value is None:
        return ""
    try:
        return value.astimezone().strftime("%d %b %Y")
    except OverflowError, OSError, ValueError:
        logger.debug("Un-printable date %r — leaving the cell empty", value)
        return ""
