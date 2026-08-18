"""baseline

Revision ID: d5521dc3737c
Revises: 2cc0d832def5
Create Date: 2025-12-30 14:43:47.334328

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy_utils import StringEncryptedType
from sqlalchemy_utils.types.encrypted.encrypted_type import FernetEngine

from alembic import op

from app import configuration

# revision identifiers, used by Alembic.
revision: str = "appkit_user"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def get_encryption_key() -> str:
    """Get encryption key from environment or config."""
    config = configuration.app.database
    return str(config.encryption_key.get_secret_value())


def upgrade() -> None:
    encryption_key = get_encryption_key()

    op.create_table(
        "auth_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("avatar_url", sa.String(length=500), nullable=True),
        sa.Column("_password", sa.String(length=200), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("needs_password_reset", sa.Boolean(), nullable=False),
        sa.Column(
            "roles",
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "last_login",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
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
        sa.UniqueConstraint("email"),
    )
    with op.batch_alter_table("auth_users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_auth_users_id"), ["id"], unique=False)

    op.create_table(
        "auth_oauth_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("account_email", sa.String(length=200), nullable=False),
        sa.Column(
            "access_token",
            StringEncryptedType(sa.Unicode(), encryption_key, FernetEngine),
            nullable=False,
        ),
        sa.Column(
            "refresh_token",
            StringEncryptedType(sa.Unicode(), encryption_key, FernetEngine),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_type", sa.String(length=20), nullable=False),
        sa.Column("scope", sa.String(length=500), nullable=True),
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
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "account_id", name="uq_oauth_provider_account"),
    )
    with op.batch_alter_table("auth_oauth_accounts", schema=None) as batch_op:
        batch_op.create_index("ix_oauth_accounts_user_id", ["user_id"], unique=False)

    op.create_table(
        "auth_oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("code_verifier", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("auth_oauth_states", schema=None) as batch_op:
        batch_op.create_index(
            "ix_oauth_states_expires_at", ["expires_at"], unique=False
        )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )

    op.execute(
        """
        INSERT INTO public.auth_users
        (email, name, avatar_url, is_active, is_admin, is_verified, _password, created, updated, last_login, needs_password_reset, roles)
        VALUES(
            'admin',
            'Default Admin (please change)',
            '',
            true,
            true,
            true,
            'scrypt:32768:8:1$bIA8HVQhPyudwZyV$76d044d2322d395a3a9c95b29337c0c4d24e2426d86d246cc72095fe2455be0540590ecea3c4d433262ea9d9aaa44eaa285363eed568451895ef25652911a2dc',
            '2025-02-03 10:18:40.258',
            '2025-02-03 10:19:14.354',
            '2025-02-03 10:19:14.354',
            false,
            '{"user"}'
        );
        """
    )


def downgrade() -> None:
    op.drop_table("auth_sessions")
    with op.batch_alter_table("auth_oauth_states", schema=None) as batch_op:
        batch_op.drop_index("ix_oauth_states_expires_at")

    op.drop_table("auth_oauth_states")
    with op.batch_alter_table("auth_oauth_accounts", schema=None) as batch_op:
        batch_op.drop_index("ix_oauth_accounts_user_id")

    op.drop_table("auth_oauth_accounts")
    with op.batch_alter_table("auth_users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_auth_users_id"))

    op.drop_table("auth_users")
