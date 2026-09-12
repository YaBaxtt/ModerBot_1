"""Store private complaints without requiring a known group or target user."""
from alembic import op
from app.database.models import PrivateReport

revision = '20260908_05'
down_revision = '20260907_04'
branch_labels = None
depends_on = None


def upgrade():
    PrivateReport.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to preserve complaints.')
