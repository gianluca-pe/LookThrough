"""Add immutable derived portfolio snapshots.

Revision ID: d1e5f7a9b2c4
Revises: c4e8f1a2b3d5
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op


revision = "d1e5f7a9b2c4"
down_revision = "c4e8f1a2b3d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("reporting_currency_code", sa.String(length=3), nullable=False),
        sa.Column("summary_status", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "summary_status IN ('current','stale','partial','missing','empty')",
            name="snapshot_status_supported",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "portfolio_id", "as_of_date", name="uq_snapshot_portfolio_as_of"
        ),
    )
    op.create_index(
        op.f("ix_portfolio_snapshots_portfolio_id"),
        "portfolio_snapshots",
        ["portfolio_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_portfolio_snapshots_as_of_date"),
        "portfolio_snapshots",
        ["as_of_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_portfolio_snapshots_as_of_date"),
        table_name="portfolio_snapshots",
    )
    op.drop_index(
        op.f("ix_portfolio_snapshots_portfolio_id"),
        table_name="portfolio_snapshots",
    )
    op.drop_table("portfolio_snapshots")
