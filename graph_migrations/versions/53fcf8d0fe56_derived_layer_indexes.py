"""derived layer indexes

Revision ID: 53fcf8d0fe56
Revises: fc2f7a8d4b66
Create Date: 2026-08-22 12:26:33.565254+00:00

The three primary keys `mailarc_analytics.derived.model` declares with
``index=True``. The declaration is only a statement of intent — runic.migrate
is what creates the index — and until it is honoured, every ``MERGE (:Group
{id: ...})`` a rebuild issues is a scan of every node wearing that label. A
rebuild writes one such ``MERGE`` per group, per topic and per template, so the
cost of leaving them out is quadratic in exactly the number that grows with the
archive.

Range indexes and not unique constraints, although each of the three properties
*is* unique: a constraint on FalkorDB is enforced on every write to the label,
and these nodes are deleted and rewritten wholesale on every rebuild. The
uniqueness is already guaranteed upstream — a derived id is a hash of the thing
it names — so a constraint would buy nothing and charge for it.

``Message.id`` gets one too, although it belongs to the ground truth. The
baseline indexed the four properties the analyses *filter* by and left the
primary key alone, because until this phase nothing walked the archive in id
order. The derived reader does: it pages the ground truth with
``WHERE m.id > $after ... ORDER BY m.id``, which is an index seek with the
index and a full sort of every message without it — once per page.

Nothing here touches the derived edges. ``CO_ADDRESSED``, ``ADDRESSED_GROUP``,
``ABOUT`` and ``INSTANCE_OF`` are all merged from their endpoints, never looked
up by a property of their own, and FalkorDB reaches an edge through the node it
hangs off.
"""

from datetime import datetime
from typing import Any

message = "derived layer indexes"
create_date = datetime.fromisoformat("2026-08-22T12:26:33.565254+00:00")

revision = "53fcf8d0fe56"
down_revision = "fc2f7a8d4b66"
branch_labels: list[str] = []
depends_on: list[str] = []
irreversible = False
snapshot = False

DERIVED_LABELS = ("Group", "Topic", "Template")
"""The three labels `task graph:rebuild-derived` deletes and writes again."""


def upgrade(op: Any) -> None:
    for label in DERIVED_LABELS:
        op.create_range_index(label, "id")
    op.create_range_index("Message", "id")


def downgrade(op: Any) -> None:
    op.drop_range_index("Message", "id")
    for label in reversed(DERIVED_LABELS):
        op.drop_range_index(label, "id")
