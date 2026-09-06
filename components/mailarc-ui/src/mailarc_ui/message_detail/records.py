"""The two attachments the panel lays out instead of rendering.

Every other shape in :mod:`mailarc_ui.message_detail.components` hands the file
to something that draws it — a viewer, a frame, an ``<img>``, a block of
monospaced text. A meeting and a contact card have already been *read*, by
:mod:`mailarc_ui.message_detail.invite` and
:mod:`mailarc_ui.message_detail.vcard`, into records with named fields; what is
left is to put those fields somewhere a person's eye can find them, and that is
a different job with a different shape, so it is a different module.

One row per fact, each behind its own glyph and each absent when the file did
not say. That is the whole layout, and the absence is the part worth stating:
an invitation with no location is not an invitation with an empty location
line. A row that is there but blank tells a reader that something is missing
and makes them look for it; a row that is not there tells them the same thing
by saying nothing at all, which is how a mail client has always printed a
header it does not have.

Layout only, like its neighbour: every value is a ``Var`` off the state class
handed in, and nothing here decides what a file *is* or what any of it means.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import avatar_initials, panel_card, status_badge
from mailarc_ui.message_detail.invite import CalendarEvent, CalendarPerson
from mailarc_ui.message_detail.state import MessageDetailState
from mailarc_ui.message_detail.vcard import ContactCard, ContactDetail

GLYPH_SIZE = 16
"""The mark in front of a fact.

Smaller than the 18 a card heading wears and larger than the text beside it,
which is what makes a column of them read as a margin rather than as content.
"""

AVATAR_SIZE = 48
"""The contact's own circle — the largest in the application on purpose.

The reading pane draws a sender at 44 and the mail list at 36. A contact card
*is* the person, where those two are a message that happens to have one, and
a photograph the file went to the trouble of carrying deserves to be looked at.
"""

VALUE = "ma-record-value"
"""The class a record's value wears, named once.

Three elements draw one — a fact's text, an attendee's name, and a detail row
that may instead be a link — and they have to be the same size and weight or a
contact's email address would sit a pixel off the telephone number under it.
The link is the one exception and has a class of its own, because it also has
a hover state.
"""

TAG_WIDTH = 58
"""How much room the ``Work`` / ``Home`` / ``Mobile`` column takes.

Fixed rather than sized to its content, so that four telephone numbers line up
under each other instead of each starting wherever its own word ended. Wide
enough for ``Textphone`` to sit without wrapping at the size it is set in.
"""


def calendar_preview(state: type[MessageDetailState], **props: Any) -> rx.Component:
    """Every meeting in the opened ``.ics``, one card each.

    A list even though almost every file holds exactly one event: the two cases
    are the same layout and a special path for the common one would be a second
    thing to keep right.

    ``props`` is how the panel says how tall this is allowed to be. The sizing
    belongs to the pane the records are drawn inside and not to the records —
    reaching into :mod:`mailarc_ui.message_detail.components` for its ``GROW``
    would make this module depend on the one that draws it.
    """
    return mn.scroll_area(
        mn.stack(
            rx.foreach(state.preview.events, _event_card),
            gap="sm",
        ),
        type="hover",
        **props,
    )


def contact_preview(state: type[MessageDetailState], **props: Any) -> rx.Component:
    """Every person in the opened ``.vcf``, one card each."""
    return mn.scroll_area(
        mn.stack(
            rx.foreach(state.preview.contacts, _contact_card),
            gap="sm",
        ),
        type="hover",
        **props,
    )


def _event_card(event: CalendarEvent) -> rx.Component:
    """One meeting: what it is called, when it is, and who is on it.

    The badge is above the title rather than beside it. A cancellation is the
    thing a reader has to see first, and a word set next to a heading that may
    run to three lines is a word that ends up somewhere unpredictable.
    """
    return panel_card(
        mn.stack(
            _badge(event),
            mn.text(event.summary, class_name="ma-record-title"),
            _fact("calendar-clock", event.when),
            _fact("repeat", event.recurrence),
            _fact("map-pin", event.location),
            _fact("user", event.organizer),
            _attendees(event),
            _paragraph("align-left", event.description),
            gap=8,
            style={"minWidth": 0},
        ),
        padding="md",
    )


def _contact_card(card: ContactCard) -> rx.Component:
    """One person: their picture or their initials, and their details."""
    return panel_card(
        mn.stack(
            _contact_head(card),
            _details("mail", card.emails),
            _details("phone", card.phones),
            _details("map-pin", card.addresses),
            _details("link", card.links),
            _fact("cake", card.birthday),
            _paragraph("notebook-pen", card.note),
            gap=8,
            style={"minWidth": 0},
        ),
        padding="md",
    )


def _contact_head(card: ContactCard) -> rx.Component:
    """The name, what they do and where — beside the circle they wear.

    The photograph is only handed over when there is one. An empty ``src`` is
    not the same as no ``src`` — see :func:`~mailarc_ui.kit.avatar.avatar_initials`
    — so the choice is made here, in the browser, where whether this particular
    card carried a picture is known.
    """
    return mn.group(
        rx.cond(
            card.photo != "",
            avatar_initials(card.name, size=AVATAR_SIZE, src=card.photo),
            avatar_initials(card.name, size=AVATAR_SIZE),
        ),
        mn.stack(
            mn.text(card.name, class_name="ma-record-title"),
            _quiet(card.title),
            _quiet(card.organisation),
            gap=2,
            style={"minWidth": 0},
        ),
        gap=14,
        align="center",
        wrap="nowrap",
        style={"minWidth": 0},
    )


def _fact(icon: str, value: rx.Var | str) -> rx.Component:
    """One line of the record, or nothing at all when the file did not say."""
    return rx.cond(
        value != "",
        mn.group(
            _glyph(icon),
            mn.text(value, class_name=VALUE),
            gap=10,
            align="flex-start",
            wrap="nowrap",
            style={"minWidth": 0},
        ),
        rx.fragment(),
    )


def _paragraph(icon: str, value: rx.Var | str) -> rx.Component:
    """A description or a note — the one field that arrives with its own lines.

    ``pre-wrap`` rather than the plain wrapping every other row gets: an agenda
    is a list somebody typed as one, and running its three items together into
    a paragraph loses the only structure it had.
    """
    return rx.cond(
        value != "",
        mn.group(
            _glyph(icon),
            mn.text(value, class_name="ma-record-body"),
            gap=10,
            align="flex-start",
            wrap="nowrap",
            style={"minWidth": 0},
        ),
        rx.fragment(),
    )


def _attendees(event: CalendarEvent) -> rx.Component:
    """Everybody invited, under one glyph, one to a line.

    The condition is the list itself and not a length: an empty list is falsy
    in the browser too, and ``Var.length()`` is what this repository has
    already found the type checker rejects inside a ``foreach``.
    """
    return rx.cond(
        event.attendees,
        mn.group(
            _glyph("users"),
            mn.stack(
                rx.foreach(event.attendees, _attendee),
                _quiet(event.more_attendees),
                gap=2,
                style={"minWidth": 0},
            ),
            gap=10,
            align="flex-start",
            wrap="nowrap",
            style={"minWidth": 0},
        ),
        rx.fragment(),
    )


def _attendee(person: CalendarPerson) -> rx.Component:
    """One person and their answer — ``Anna Bauer · Accepted``.

    The answer trails the name in the quieter colour rather than sitting in a
    column of its own, because it is missing on every attendee of an ordinary
    invitation and a column that is empty for all of them is a column that
    should not have been drawn.
    """
    return mn.group(
        mn.text(person.label, class_name=VALUE),
        rx.cond(
            person.status != "",
            mn.text("· " + person.status, class_name="ma-record-tag"),
            rx.fragment(),
        ),
        gap=6,
        align="baseline",
        wrap="nowrap",
        style={"minWidth": 0},
    )


def _details(icon: str, rows: rx.Var | list[ContactDetail]) -> rx.Component:
    """One kind of contact detail — every address, every number — under a glyph."""
    return rx.cond(
        rows,
        mn.group(
            _glyph(icon),
            mn.stack(
                rx.foreach(rows, _detail),
                gap=2,
                style={"minWidth": 0},
            ),
            gap=10,
            align="flex-start",
            wrap="nowrap",
            style={"minWidth": 0},
        ),
        rx.fragment(),
    )


def _detail(row: ContactDetail) -> rx.Component:
    """``Work    anna@example.com`` — the kind, then the thing.

    The kind is always drawn even when it is empty, which is the opposite of
    the rule the rest of this module follows and is right here for one reason:
    it is a column. Four numbers under each other line up because every row
    reserves the same width for a word most of them have, and a row that
    dropped its empty label would step left out of the column.
    """
    return mn.group(
        mn.text(row.label, class_name="ma-record-tag", w=TAG_WIDTH),
        rx.cond(row.href != "", _link(row), mn.text(row.value, class_name=VALUE)),
        gap=6,
        align="baseline",
        wrap="nowrap",
        style={"minWidth": 0},
    )


def _link(row: ContactDetail) -> rx.Component:
    """A row a reader can press — an address to write to, a site to open.

    Whether there is one at all was decided by
    :mod:`mailarc_ui.message_detail.vcard`, which is the module that knows the
    value came out of a file a stranger wrote; this only draws what it was
    handed. ``is_external`` comes off the row for the same reason: a
    ``mailto:`` hands over to a mail program and should not also open an empty
    tab, and that is a fact about the scheme rather than about the layout.
    """
    return rx.link(
        row.value,
        href=row.href,
        is_external=row.external,
        rel="noopener noreferrer",
        class_name="ma-record-link",
    )


def _badge(event: CalendarEvent) -> rx.Component:
    """``Cancelled``, ``Invitation`` — or nothing, which is the usual case."""
    return rx.cond(
        event.label != "",
        mn.group(
            status_badge(event.label, event.label_color),
            gap=0,
        ),
        rx.fragment(),
    )


def _quiet(value: rx.Var | str) -> rx.Component:
    """A secondary line under a name, drawn only when there is one."""
    return rx.cond(
        value != "",
        mn.text(value, class_name="ma-record-tag"),
        rx.fragment(),
    )


def _glyph(icon: str) -> rx.Component:
    """The mark in the record's left margin.

    It does not shrink and it does not grow: a glyph in a flex row beside a
    long address is squashed to a sliver by the browser's default, and one
    that is a different width per row would stop being a margin.
    """
    return rx.icon(
        icon,
        size=GLYPH_SIZE,
        class_name="ma-record-glyph",
        style={"flexShrink": 0},
    )
