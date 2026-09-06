"""Syncing the whole archive from one press, and what the rail shows of it.

``state`` owns everything that touches the queue or the provider registry;
``components`` owns the single button the icon rail composes. The decision
*which* mailbox is owed what is in neither — it is
:class:`mailarc_sync.jobs.sweep.MailboxSweep`'s, shared with the worker's
schedule so the button and the timer cannot come to disagree.
"""

from mailarc_ui.sync.components import rail_sync_button
from mailarc_ui.sync.state import (
    NOTHING_TO_SYNC,
    POLL_SECONDS,
    SYNC_LABEL,
    SyncAllState,
    announce,
    mailboxes,
)

__all__ = [
    "NOTHING_TO_SYNC",
    "POLL_SECONDS",
    "SYNC_LABEL",
    "SyncAllState",
    "announce",
    "mailboxes",
    "rail_sync_button",
]
