"""llm settings

Revision ID: llm_settings
Revises: semantic_settings
Create Date: 2026-09-06 17:30:00.000000

The one row that holds the model a human picked to read their questions, so
that natural-language search can be turned on from the application rather than
only from a file. Written by hand (§9.2) for ``semantic_settings``' reasons:
autogenerate would render ``EncryptedString`` as the ``VARCHAR`` it compiles to
and would happily offer to drop the tables ``appkit_user`` owns.

``semantic_settings`` twinned, minus one column. Everything that table's
revision explains applies unchanged — the ``CHECK (id = 1)`` because SQLite has
no other way to say "one row", and ``api_key`` spelled with the type rather
than with ``sa.String`` so a later autogenerate diff sees no change.

**A second table rather than three more columns on the first.** These are two
models and not two settings of one: they differ in what leaves the machine, and
an installation may reasonably have either without the other. Sharing a row
would also share ``updated``, which is both forms' concurrency baseline — so
saving the embedder would silently invalidate the interpreter's, and one person
would be told their settings had been changed by somebody else when nobody had
touched them.

No ``dimension``. Nothing here produces a vector, so there is no index length
for a setting to disagree with.

The downgrade drops the table, which loses the stored key. That is the correct
loss: the file/env ``LlmConfig`` answers once the table is gone, and a fresh
installation reads ``provider: none`` — the state this application is complete
in.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from appkit_commons.database.entities import EncryptedString

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "llm_settings"
down_revision: str | None = "semantic_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LLM_SETTINGS_ID = 1
BASE_URL_LENGTH = 1024


def upgrade() -> None:
    op.create_table(
        "llm_settings",
        # All four nullable, and NULL means "not set" rather than "empty": the
        # composition root lays a non-NULL value over the configured
        # `LlmConfig` and lets a NULL fall through to it.
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("base_url", sa.String(length=BASE_URL_LENGTH), nullable=True),
        sa.Column("api_key", EncryptedString(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(f"id = {LLM_SETTINGS_ID}", name="ck_llm_settings_singleton"),
    )
    with op.batch_alter_table("llm_settings", schema=None) as batch_op:
        batch_op.create_index("ix_llm_settings_id", ["id"], unique=False)


def downgrade() -> None:
    op.drop_table("llm_settings")
