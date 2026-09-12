"""Trusted moderators scoped to one chat."""
from alembic import op

from app.database.models import ChatModerator

revision = '20260909_08'
down_revision = '20260909_07'
branch_labels = None
depends_on = None


def upgrade():
    ChatModerator.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to preserve moderator assignments.')
