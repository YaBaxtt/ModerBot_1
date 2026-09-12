"""Make internal IDs SQLite-compatible without discarding rows.

Revision ID: 20260907_02
Revises: 20260907_01
Create Date: 2026-09-07
"""
from alembic import op
import sqlalchemy as sa

revision = "20260907_02"
down_revision = "20260907_01"
branch_labels = None
depends_on = None

TABLES = (
    "users", "chats", "user_chat_stats", "daily_activity", "activity_messages",
    "warnings", "moderation_actions", "reports", "advertising_placements",
    "advertising_requests", "shop_items", "shop_purchases", "broadcasts",
)


def upgrade() -> None:
    # Batch mode creates a temporary table, copies every row, then swaps it in.
    # It is required because SQLite cannot ALTER a primary-key column in place.
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute("PRAGMA foreign_keys=OFF")
    for table in TABLES:
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.alter_column("id", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False, autoincrement=True)
    op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect bot data.")
