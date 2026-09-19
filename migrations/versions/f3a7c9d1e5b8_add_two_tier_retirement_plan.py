"""Add the explicitly adopted two-tier retirement plan and external income."""
import sqlalchemy as sa
from alembic import op

revision = "f3a7c9d1e5b8"
down_revision = "e2f6a8c0d3b5"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('retirement_plans',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('portfolio_id', sa.Integer(), sa.ForeignKey('portfolios.id'), nullable=False, primary_key=False),
        sa.Column('base_date', sa.Date(), nullable=False, primary_key=False),
        sa.Column('currency_code', sa.String(3), nullable=False, primary_key=False),
        sa.Column('current_age_years', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('withdrawal_start_age_years', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('final_age_years', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('core_amount', sa.Numeric(28, 12), nullable=False, primary_key=False),
        sa.Column('flexible_amount', sa.Numeric(28, 12), nullable=False, primary_key=False),
        sa.Column('core_inflation_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('flexible_inflation_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('equity_return_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('income_return_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('liquidity_return_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('alternatives_return_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('lower_rate_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('upper_rate_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('lower_multiplier_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('middle_multiplier_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('upper_multiplier_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('terminal_legacy_target_amount', sa.Numeric(28, 12), nullable=True, primary_key=False),
        sa.Column('terminal_legacy_target_currency_code', sa.String(3), nullable=True, primary_key=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, primary_key=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False, primary_key=False),
        sa.CheckConstraint('current_age_years >= 0 AND current_age_years <= 120 AND withdrawal_start_age_years >= current_age_years AND withdrawal_start_age_years <= 120 AND final_age_years > withdrawal_start_age_years AND final_age_years <= 130', name='plan_age_order'),
        sa.CheckConstraint('core_inflation_decimal >= 0 AND core_inflation_decimal <= 1 AND flexible_inflation_decimal >= 0 AND flexible_inflation_decimal <= 1', name='plan_inflation_bounds'),
        sa.CheckConstraint('(terminal_legacy_target_amount IS NULL AND terminal_legacy_target_currency_code IS NULL) OR (terminal_legacy_target_amount IS NOT NULL AND terminal_legacy_target_amount >= 0 AND terminal_legacy_target_currency_code IS NOT NULL)', name='plan_legacy_paired'),
        sa.CheckConstraint('lower_multiplier_decimal >= middle_multiplier_decimal AND middle_multiplier_decimal >= upper_multiplier_decimal AND upper_multiplier_decimal >= 0 AND lower_multiplier_decimal <= 2', name='plan_multiplier_order'),
        sa.CheckConstraint('lower_rate_decimal > 0 AND upper_rate_decimal > lower_rate_decimal AND upper_rate_decimal <= 1', name='plan_rate_order'),
        sa.CheckConstraint('equity_return_decimal >= -1 AND equity_return_decimal <= 1 AND income_return_decimal >= -1 AND income_return_decimal <= 1 AND liquidity_return_decimal >= -1 AND liquidity_return_decimal <= 1 AND alternatives_return_decimal >= -1 AND alternatives_return_decimal <= 1', name='plan_return_bounds'),
        sa.CheckConstraint('core_amount >= 0 AND flexible_amount >= 0 AND core_amount + flexible_amount > 0', name='plan_spending_positive'),
        sa.UniqueConstraint("portfolio_id", name="uq_retirement_plan_portfolio"),
    )
    op.create_index('ix_retirement_plans_portfolio_id', 'retirement_plans', ['portfolio_id'])
    op.create_table('retirement_income',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('retirement_plans.id'), nullable=False, primary_key=False),
        sa.Column('name', sa.String(160), nullable=False, primary_key=False),
        sa.Column('annual_amount', sa.Numeric(28, 12), nullable=False, primary_key=False),
        sa.Column('currency_code', sa.String(3), nullable=False, primary_key=False),
        sa.Column('start_date', sa.Date(), nullable=False, primary_key=False),
        sa.Column('end_date', sa.Date(), nullable=True, primary_key=False),
        sa.Column('inflation_decimal', sa.Numeric(10, 8), nullable=False, primary_key=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, primary_key=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False, primary_key=False),
        sa.CheckConstraint('end_date IS NULL OR end_date >= start_date', name='retirement_income_dates'),
        sa.CheckConstraint('inflation_decimal >= 0 AND inflation_decimal <= 1', name='retirement_income_inflation'),
        sa.CheckConstraint('annual_amount >= 0', name='retirement_income_nonnegative'),
    )
    op.create_index('ix_retirement_income_plan_id', 'retirement_income', ['plan_id'])

def downgrade():
    op.drop_table("retirement_income")
    op.drop_table("retirement_plans")
