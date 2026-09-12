"""Initial ModerBot schema.

Revision ID: 20260907_01
Revises:
Create Date: 2026-09-07
"""
from alembic import op
from app.database.models import Base

revision = "20260907_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    # Deliberately left non-destructive: production moderation data must not be erased by accident.
    raise RuntimeError("Downgrade is intentionally disabled to protect production data.")

