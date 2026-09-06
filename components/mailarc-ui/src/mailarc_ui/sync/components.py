"""The Sync button, as the rail wears it.

One component, and it is built here rather than in
:mod:`mailarc_ui.shell.navigation` for the reason every other package in this
application splits the same way: ``navigation`` owns the *table* of pages and
how a rail entry is drawn, and this is not a page. The rail composes it.

It is an ``mn.unstyled_button`` — the same element the light/dark toggle at the
foot of the rail is — and deliberately not one of the kit's five buttons. The
kit owns what the *design* has one of, and a 44px square wearing
``.ma-rail-item`` is the rail's vocabulary rather than the page's: a
``kit.primary_button`` here would be a coral pill in a column of quiet glyphs.
``test_ui_kit_owns_every_element`` agrees — ``mn.unstyled_button`` is not in
its table, precisely because it carries no design of its own.

The animation is one attribute. ``data-syncing`` crosses the Var boundary and
``assets/css/mail-archive.css`` turns it into the rotation, for the argument
``.ma-rail-item[data-active]`` already makes: a spin is a keyframe, a duration
and a timing function together, and none of those fit in a ``style`` dict
assembled with ``rx.cond``.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.sync.state import SYNC_LABEL, SyncAllState


def rail_sync_button() -> rx.Component:
    """The archive's one Sync control, tooltipped like every rail entry.

    ``aria_label`` stays :data:`~mailarc_ui.sync.state.SYNC_LABEL` while the
    tooltip changes under it: the tooltip is the news and the label is the
    name, and a control renamed by its own progress is a control a screen
    reader user meets for the first time on every poll.

    It is not disabled while a sync runs, only while a press is in flight. A
    second press mid-sync is a reasonable thing to do — it picks up the mailbox
    that finished, and reports the rest as already syncing — and a button that
    goes dead for the length of an import teaches people that it is broken.
    """
    return mn.tooltip(
        mn.unstyled_button(
            rx.icon("refresh-cw", size=20),
            class_name="ma-rail-item ma-rail-sync",
            custom_attrs={"data-syncing": SyncAllState.syncing},
            aria_label=SYNC_LABEL,
            disabled=SyncAllState.starting,
            on_click=SyncAllState.sync_all,
            on_mount=SyncAllState.adopt,
        ),
        label=SyncAllState.hint,
        position="right",
        offset=10,
    )
