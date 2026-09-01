"""Undoing one mailbox's import, so that it can be imported again.

The engine's mirror image, and it needs the same two stores the engine writes:
the graph that holds the mail and the relational ledgers that record what has
already been fetched. Emptying only one of them leaves a mailbox that looks
clear and re-imports nothing.

Two modules, layered so nothing points back up:

``model``
    ``EraseCounts`` — what one clear-out removed, store by store — and
    ``AccountBusy``, the one refusal that means "wait", not "broken".
``eraser``
    ``AccountEraser`` — the order the two stores are cleared in, and why.
"""

from mailarc_sync.erase.eraser import AccountEraser
from mailarc_sync.erase.model import AccountBusy, EraseCounts

__all__ = [
    "AccountBusy",
    "AccountEraser",
    "EraseCounts",
]
