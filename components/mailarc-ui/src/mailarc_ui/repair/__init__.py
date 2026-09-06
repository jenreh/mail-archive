"""Repairing the archive's conversations, as a card an operator can press.

The administration side of one missing edge. A message is grouped with its
replies through the ``Thread`` node its headers name, and mail archived before
the writer learned to key that node on a message's own ``Message-ID`` carries
no such edge — so the first message of an old exchange is listed beside its own
replies instead of under them. No import repairs it: the engine skips every
message the ledger already knows.

``model``
    ``RepairJobView`` and the sentences the card says. No I/O.
``state``
    ``ConversationRepairState`` — the count, the ``repair`` job it queues, and
    the row it follows.
``components``
    ``repair_card`` — what the graph status page drops in.

The work itself is ``mailarc_core.archive.threads`` and the worker's ``repair``
handler; nothing here writes to the graph.
"""

from mailarc_ui.repair.components import repair_card
from mailarc_ui.repair.model import (
    NO_JOB,
    RepairJobView,
    outside_label,
    repaired_label,
)
from mailarc_ui.repair.state import ConversationRepairState

__all__ = [
    "NO_JOB",
    "ConversationRepairState",
    "RepairJobView",
    "outside_label",
    "repair_card",
    "repaired_label",
]
