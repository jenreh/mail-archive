"""annotation layer

Revision ID: 3824f164c0a6
Revises: 5f4678dfc5a4
Create Date: 2026-09-02 19:40:00.000000+00:00

Three declarations for two phases, in one revision because the history is worth
more short than granular and because nothing here can half-apply usefully.

The ``UNIQUE`` constraint on ``Tag.id`` is the only real *guarantee* in the
annotation layer. ``TagRepository.create`` looks the key up before it writes and
raises ``TagExists``, which is what turns a duplicate into a sentence a person
can read — but a lookup is not a guarantee: two sessions can both find nothing
and both write, and the second one would create a *second* ``Tag`` node under
the same id, splitting one project's mail across two nodes that no listing
could tell apart. A constraint costs a check on every write to the label, and
tags are written by hand a few times a day, so the trade is not close.

Unlike the derived labels, which get a range index and no constraint for the
opposite reason: those are deleted and rewritten wholesale on every rebuild,
their ids are already hashes of their own contents, and a constraint would
charge a per-write toll for a guarantee the id gives away.

No explicit range index on ``Tag.id``. ``create_constraint`` creates one
itself, unconditionally, and FalkorDB rejects a second ``CREATE INDEX`` on an
indexed attribute — the trap the baseline (``fc2f7a8d4b66``) records against
``Message.rfc_message_id`` and verified against a live server. The downgrade
therefore has to drop the constraint *and* then the index it left behind:
``GRAPH.CONSTRAINT DROP`` does not remove it, and dropping the index first is
refused with "Index supports constraint".

The two range indexes belong to phase 2 and are here early on purpose. The
properties are declared on the models now — ``Message.importance`` and
``Address.rank`` — so that the writer and the migration cannot disagree about
their names, and an index on a property nothing has written yet costs nothing
but the empty structure. What it buys is the read: the insights page orders by
``importance`` desc and the community labels order by ``rank``, and both are a
full sort of every node of that label without one.

Nothing here creates a node. A graph has no tables to prepare, and a ``Tag`` is
something a person makes.
"""

from datetime import datetime
from typing import Any

message = "annotation layer"
create_date = datetime.fromisoformat("2026-09-02T19:40:00.000000+00:00")

revision = "3824f164c0a6"
down_revision = "5f4678dfc5a4"
branch_labels: list[str] = []
depends_on: list[str] = []
irreversible = False
snapshot = False

TAG_LABEL = "Tag"
TAG_KEY = "id"
"""The annotation node and its key — ``tag:<slug>``, derived from the name."""

SCORE_INDEXES = (("Message", "importance"), ("Address", "rank"))
"""The two derived scores phase 2 writes and the analysis reads in order."""


def upgrade(op: Any) -> None:
    # The constraint creates its own range index on `Tag.id`; asking for one as
    # well fails the migration on FalkorDB.
    op.create_constraint("UNIQUE", "NODE", TAG_LABEL, [TAG_KEY])

    for label, prop in SCORE_INDEXES:
        op.create_range_index(label, prop)


def downgrade(op: Any) -> None:
    for label, prop in reversed(SCORE_INDEXES):
        op.drop_range_index(label, prop)

    # Constraint first: dropping the index while the constraint stands is
    # refused with "Index supports constraint". Dropping the constraint does
    # not take the index the upgrade never asked for, so it goes explicitly.
    op.drop_constraint("UNIQUE", "NODE", TAG_LABEL, [TAG_KEY])
    op.drop_range_index(TAG_LABEL, TAG_KEY)
