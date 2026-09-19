"""Add immutable retirement comparison runs.

Revision ID: b5c9d3e7f1a2
Revises: a4b8d0e2f6c9
"""
import sqlalchemy as sa
from alembic import op

revision = "b5c9d3e7f1a2"
down_revision = "a4b8d0e2f6c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "retirement_scenarios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_retirement_scenarios_portfolio_id"), "retirement_scenarios", ["portfolio_id"])


def downgrade():
    op.drop_index(op.f("ix_retirement_scenarios_portfolio_id"), table_name="retirement_scenarios")
    op.drop_table("retirement_scenarios")
