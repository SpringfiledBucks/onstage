"""Durable coalesced Web Push and private cross-room feedback safeguards."""
from pathlib import Path
from alembic import op
from sqlalchemy import text

revision = '20261020_02'
down_revision = '20261020_01'
branch_labels = None
depends_on = None


def upgrade():
    root = Path(__file__).resolve().parents[2]
    for filename in ('schema_community.sql', 'schema_notifications.sql'):
        for statement in (root / filename).read_text(encoding='utf8').split(';'):
            if statement.strip():
                op.get_bind().execute(text(statement))


def downgrade():
    raise RuntimeError('Preserve notification consent and feedback history; restore a verified backup.')
