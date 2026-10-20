"""Add accounts and community tables; preserve all original schema-4 IDs."""
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "20261020_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    root = Path(__file__).resolve().parents[2]
    for filename in ("schema_accounts.sql", "schema_room_features.sql", "schema_community.sql"):
        source = (root / filename).read_text(encoding="utf8")
        for statement in source.split(";"):
            if statement.strip():
                connection.execute(text(statement))


def downgrade():
    raise RuntimeError("Community data cannot be discarded by downgrade; restore a verified backup.")
