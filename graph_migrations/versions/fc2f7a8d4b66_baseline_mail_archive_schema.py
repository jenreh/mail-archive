"""baseline mail archive schema

Revision ID: fc2f7a8d4b66
Revises: None
Create Date: 2026-08-19 20:23:08.744152+00:00

The indexes the ground truth of ``mailarc_core.archive.model`` declares. runic
creates them; the model only says they exist. Nothing here creates nodes — a
graph has no tables to prepare, so a fresh archive is one that has never been
written to, not one that is empty of structure.

Two searches drive the UI: full text over ``subject`` and ``body_text``, and
lookups by ``sent_at``, ``subject_norm``, ``simhash`` and ``participant_key``
— the four properties the analyses A1-A3 group and window by. The unique
constraint on ``rfc_message_id`` is what makes re-importing the same mail a
no-op rather than a duplicate.

The vector index on ``Message.embedding`` is deliberately absent: nothing
writes embeddings before phase 6, and an HNSW index costs memory from the day
it exists.
"""

from datetime import datetime
from typing import Any

message = "baseline mail archive schema"
create_date = datetime.fromisoformat("2026-08-19T20:23:08.744152+00:00")

revision = "fc2f7a8d4b66"
down_revision = None
branch_labels: list[str] = []
depends_on: list[str] = []
irreversible = False
snapshot = False

MESSAGE_RANGE_PROPS = ("sent_at", "subject_norm", "simhash", "participant_key")
"""Message properties the analyses filter and group by."""


def upgrade(op: Any) -> None:
    for prop in MESSAGE_RANGE_PROPS:
        op.create_range_index("Message", prop)
    op.create_range_index("Address", "domain")

    # The range index for `rfc_message_id` is NOT created here, although the
    # rule says index before constraint: `create_constraint` creates it itself,
    # unconditionally, and FalkorDB rejects a second `CREATE INDEX` on an
    # indexed attribute. Verified against a live server — asking for both
    # fails the migration.
    op.create_constraint("UNIQUE", "NODE", "Message", ["rfc_message_id"])

    # One full-text index over both properties — the archive searches them
    # together, and FalkorDB keeps one such index per label.
    op.create_fulltext_index("Message", "subject", "body_text")


def downgrade(op: Any) -> None:
    op.drop_fulltext_index("Message", "subject", "body_text")

    # Constraint first: dropping the index while the constraint stands is
    # refused with "Index supports constraint".
    op.drop_constraint("UNIQUE", "NODE", "Message", ["rfc_message_id"])
    op.drop_range_index("Message", "rfc_message_id")

    op.drop_range_index("Address", "domain")
    for prop in reversed(MESSAGE_RANGE_PROPS):
        op.drop_range_index("Message", prop)
