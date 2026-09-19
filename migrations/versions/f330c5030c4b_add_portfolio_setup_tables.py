"""Add portfolio setup tables.

Revision ID: f330c5030c4b
Revises: 040c3a312c85
Create Date: 2026-08-02 20:15:44.328564

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f330c5030c4b"
down_revision = "040c3a312c85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("reporting_currency_code", sa.String(length=3), nullable=False),
        sa.Column(
            "annual_spending_amount",
            sa.Numeric(precision=24, scale=4),
            nullable=False,
        ),
        sa.Column("default_as_of_date", sa.Date(), nullable=True),
        sa.Column("price_stale_days", sa.Integer(), server_default="14", nullable=False),
        sa.Column("fx_stale_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column(
            "statement_value_stale_days",
            sa.Integer(),
            server_default="45",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "annual_spending_amount > 0", name="annual_spending_amount_positive"
        ),
        sa.CheckConstraint("fx_stale_days > 0", name="fx_stale_days_positive"),
        sa.CheckConstraint("price_stale_days > 0", name="price_stale_days_positive"),
        sa.CheckConstraint(
            "statement_value_stale_days > 0",
            name="statement_value_stale_days_positive",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolios")),
    )
    op.create_table(
        "institutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_institutions_portfolio_id_portfolios"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_institutions")),
    )
    with op.batch_alter_table("institutions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_institutions_portfolio_id"),
            ["portfolio_id"],
            unique=False,
        )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("account_type", sa.String(length=24), nullable=False),
        sa.Column("default_currency_code", sa.String(length=3), nullable=False),
        sa.Column("is_multicurrency", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("cash_tracking_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "portfolio_share_decimal",
            sa.Numeric(precision=10, scale=8),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "present_access_decimal",
            sa.Numeric(precision=10, scale=8),
            server_default="1",
            nullable=False,
        ),
        sa.Column("earliest_access_date", sa.Date(), nullable=True),
        sa.Column("access_note", sa.Text(), nullable=True),
        sa.Column(
            "relationship_eligible", sa.Boolean(), server_default="1", nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "account_type IN ('cash', 'brokerage', 'retirement', 'deposit', 'other')",
            name="account_type_supported",
        ),
        sa.CheckConstraint(
            "cash_tracking_mode IN ('separate_cash', 'included_in_aggregate')",
            name="cash_tracking_mode_supported",
        ),
        sa.CheckConstraint(
            "portfolio_share_decimal >= 0 AND portfolio_share_decimal <= 1",
            name="portfolio_share_between_zero_and_one",
        ),
        sa.CheckConstraint(
            "present_access_decimal >= 0 AND present_access_decimal <= 1",
            name="present_access_between_zero_and_one",
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institutions.id"],
            name=op.f("fk_accounts_institution_id_institutions"),
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_accounts_portfolio_id_portfolios"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_accounts_institution_id"),
            ["institution_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_accounts_portfolio_id"), ["portfolio_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_accounts_portfolio_id"))
        batch_op.drop_index(batch_op.f("ix_accounts_institution_id"))

    op.drop_table("accounts")
    with op.batch_alter_table("institutions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_institutions_portfolio_id"))

    op.drop_table("institutions")
    op.drop_table("portfolios")
