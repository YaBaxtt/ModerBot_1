"""Add persistent, one-per-day user rewards."""
from alembic import op

from app.database.models import DailyReward

revision = '20260909_06'
down_revision = '20260908_05'
branch_labels = None
depends_on = None


def upgrade():
    # Revision 01 imports live metadata, so fresh databases already have it.
    DailyReward.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to preserve reward history.')
