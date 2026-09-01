"""mail import tables

Revision ID: mail_import
Revises: appkit_user
Create Date: 2026-08-19 10:00:00.000000

The six tables of ``mailarc_core.database.entities`` — what the import has
*done*, next to the graph that holds what a message *is*. Written by hand:
autogenerate is an anti-pattern here, and it would happily drop the tables
``appkit_user`` owns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from appkit_commons.database.entities import EncryptedString

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "mail_import"
down_revision: str | None = "appkit_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDRESS_LENGTH = 320
PROVIDER_ID_LENGTH = 255


def _entity_columns() -> list[sa.Column]:
    """The three columns the ``Entity`` mixin adds to every table."""

    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email_address", sa.String(length=ADDRESS_LENGTH), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_entity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "email_address", name="uq_mail_accounts_provider_address"
        ),
    )
    with op.batch_alter_table("mail_accounts", schema=None) as batch_op:
        batch_op.create_index("ix_mail_accounts_id", ["id"], unique=False)

    op.create_table(
        "mail_credentials",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        # The secret is Fernet-encrypted by the type, not by the column: the
        # key is read at write time, so the DDL is a plain VARCHAR.
        sa.Column("secret", EncryptedString(), nullable=False),
        *_entity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "kind", name="uq_mail_credentials_account_kind"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["mail_accounts.id"], ondelete="CASCADE"
        ),
    )
    with op.batch_alter_table("mail_credentials", schema=None) as batch_op:
        batch_op.create_index(
            "ix_mail_credentials_account_id", ["account_id"], unique=False
        )
        batch_op.create_index("ix_mail_credentials_id", ["id"], unique=False)

    op.create_table(
        "mail_sync_checkpoints",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("messages_seen", sa.Integer(), nullable=False),
        *_entity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "scope", name="uq_mail_sync_checkpoints_account_scope"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["mail_accounts.id"], ondelete="CASCADE"
        ),
    )
    with op.batch_alter_table("mail_sync_checkpoints", schema=None) as batch_op:
        batch_op.create_index(
            "ix_mail_sync_checkpoints_account_id", ["account_id"], unique=False
        )
        batch_op.create_index("ix_mail_sync_checkpoints_id", ["id"], unique=False)

    op.create_table(
        "mail_sync_jobs",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        # Nullable: `derive` and `embed` work on the whole archive, not on one
        # mailbox.
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("messages_total", sa.Integer(), nullable=False),
        sa.Column("messages_done", sa.Integer(), nullable=False),
        sa.Column("messages_failed", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_entity_columns(),
        sa.ForeignKeyConstraint(
            ["account_id"], ["mail_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("mail_sync_jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_mail_sync_jobs_account_id", ["account_id"], unique=False
        )
        batch_op.create_index("ix_mail_sync_jobs_id", ["id"], unique=False)
        batch_op.create_index("ix_mail_sync_jobs_state", ["state"], unique=False)

    op.create_table(
        "mail_archived_messages",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column(
            "provider_message_id", sa.String(length=PROVIDER_ID_LENGTH), nullable=False
        ),
        # Unbounded: the `sha256:` fallback id is 71 characters and a real
        # Message-ID has no length the sender owes us. See the column's
        # comment in `mailarc_core.database.entities`.
        sa.Column("canonical_id", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        *_entity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "provider_message_id",
            name="uq_mail_archived_messages_account_message",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["mail_accounts.id"], ondelete="CASCADE"
        ),
    )
    with op.batch_alter_table("mail_archived_messages", schema=None) as batch_op:
        batch_op.create_index(
            "ix_mail_archived_messages_account_id", ["account_id"], unique=False
        )
        batch_op.create_index(
            "ix_mail_archived_messages_canonical_id", ["canonical_id"], unique=False
        )
        batch_op.create_index("ix_mail_archived_messages_id", ["id"], unique=False)

    op.create_table(
        "mail_failed_messages",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column(
            "provider_message_id", sa.String(length=PROVIDER_ID_LENGTH), nullable=False
        ),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_entity_columns(),
        sa.ForeignKeyConstraint(
            ["account_id"], ["mail_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("mail_failed_messages", schema=None) as batch_op:
        batch_op.create_index(
            "ix_mail_failed_messages_account_id", ["account_id"], unique=False
        )
        batch_op.create_index("ix_mail_failed_messages_id", ["id"], unique=False)


def downgrade() -> None:
    # Children first: every table but `mail_accounts` points at it.
    op.drop_table("mail_failed_messages")
    op.drop_table("mail_archived_messages")
    op.drop_table("mail_sync_jobs")
    op.drop_table("mail_sync_checkpoints")
    op.drop_table("mail_credentials")
    op.drop_table("mail_accounts")
