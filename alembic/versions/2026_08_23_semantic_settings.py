"""semantic settings

Revision ID: semantic_settings
Revises: mail_import
Create Date: 2026-08-23 10:00:00.000000

The one row that holds the embedder a human picked, so the API key for OpenAI
stops being something only a file on the server can carry. Written by hand
(§9.2): autogenerate would render ``EncryptedString`` as the ``VARCHAR`` it
compiles to and would happily offer to drop the tables ``appkit_user`` owns.

Two things here that autogenerate could not have got right on its own:

``ck_semantic_settings_singleton``
    ``CHECK (id = 1)``. The table is a single row by construction — an archive
    has one embedder, and §7.4 needs "which model is this archive embedded
    with" to have exactly one answer. SQLite has no other way to say it.

``api_key``
    An :class:`EncryptedString`, which is Fernet-encrypted by the *type* at
    write time, so the DDL below is a plain ``VARCHAR`` — the same shape
    ``mail_credentials.secret`` has, and the reason the column is spelled with
    the type here rather than with ``sa.String``: a later autogenerate diff
    then sees no change.

The downgrade drops the table, which loses the stored key. That is the correct
loss: the file/env ``SemanticConfig`` is what answers once the table is gone,
and a fresh installation reads ``provider: none`` — the state this application
is designed to be complete in.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from appkit_commons.database.entities import EncryptedString

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "semantic_settings"
down_revision: str | None = "mail_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEMANTIC_SETTINGS_ID = 1
BASE_URL_LENGTH = 1024


def upgrade() -> None:
    op.create_table(
        "semantic_settings",
        # All five nullable, and NULL means "not set" rather than "empty": the
        # composition root lays a non-NULL value over the configured
        # `SemanticConfig` and lets a NULL fall through to it.
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("dimension", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            f"id = {SEMANTIC_SETTINGS_ID}", name="ck_semantic_settings_singleton"
        ),
    )
    with op.batch_alter_table("semantic_settings", schema=None) as batch_op:
        batch_op.create_index("ix_semantic_settings_id", ["id"], unique=False)


def downgrade() -> None:
    op.drop_table("semantic_settings")
