"""Add dated reference FX sets without changing existing observations."""
from alembic import op
import sqlalchemy as sa

revision = "d7e1f5a9b3c6"
down_revision = "c6d0e4f8a2b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fx_reference_sets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("rates_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("source IN ('banca_italia', 'manual')", name="fx_reference_source"),
    )
    op.create_index("ix_fx_reference_sets_effective_date", "fx_reference_sets", ["effective_date"])


def downgrade():
    op.drop_table("fx_reference_sets")
