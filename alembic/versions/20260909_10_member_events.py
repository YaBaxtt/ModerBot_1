"""Track joins and leaves for per-group statistics."""
from alembic import op

from app.database.models import MemberEvent

revision = '20260909_10'
down_revision = '20260909_09'
branch_labels = None
depends_on = None


def upgrade():
    MemberEvent.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to preserve group statistics.')
