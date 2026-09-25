"""Add generated and owner-managed captcha questions."""
from alembic import op
import sqlalchemy as sa

from app.database.models import CaptchaQuestion

revision = '20260925_12'
down_revision = '20260912_11'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    CaptchaQuestion.__table__.create(bind=bind, checkfirst=True)
    columns = {column['name'] for column in inspector.get_columns('join_verifications')}
    if 'correct_index' not in columns:
        op.add_column('join_verifications', sa.Column('correct_index', sa.Integer(), nullable=True))
        # Keep already-issued prompts usable during a rolling deployment. New
        # challenges always persist their answer directly in this column.
        legacy = {
            'founder': 0, 'channels': 0, 'username': 0, 'stickers': 0,
            'groups': 0, 'login_code': 1, 'saved': 2, 'voice': 2,
            'poll': 1, 'reaction': 3, 'scam': 2, 'reply': 0,
            'spam': 1, 'rules': 2, 'search': 3,
        }
        for key, index in legacy.items():
            bind.execute(
                sa.text('UPDATE join_verifications SET correct_index = :index WHERE question_key = :key'),
                {'index': index, 'key': key},
            )


def downgrade() -> None:
    raise RuntimeError('Downgrade is disabled to preserve captcha questions.')
