"""Permanent Telegram Stars PRO access per group."""
from alembic import op

from app.database.models import ChatPremiumAccess, ChatProtectionSetting

revision = '20260909_09'
down_revision = '20260909_08'
branch_labels = None
depends_on = None


def upgrade():
    ChatPremiumAccess.__table__.create(bind=op.get_bind(), checkfirst=True)
    ChatProtectionSetting.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to preserve purchase records.')
