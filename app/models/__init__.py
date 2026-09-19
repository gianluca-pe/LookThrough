"""Current SQLAlchemy domain models."""

from app.models.configuration import (
    ACCOUNT_TYPE_CODES,
    CASH_TRACKING_MODE_CODES,
    Account,
    Institution,
    Portfolio,
    RelationshipRule,
)
from app.models.portfolio import (
    CAPITAL_CERTAINTY_CODES,
    CREDIT_BAND_CODES,
    CURRENCY_TREATMENT_CODES,
    DURATION_BAND_CODES,
    ECONOMIC_ROLE_CODES,
    EQUITY_SENSITIVITY_CODES,
    FIRE_BUCKET_CODES,
    LEGACY_ECONOMIC_ROLE_CODES,
    LEGACY_FIRE_BUCKET_CODES,
    MATURITY_ACTION_CODES,
    HEDGING_STATUS_CODES,
    INSTRUMENT_TYPE_CODES,
    LIQUIDITY_PROFILE_CODES,
    TRACKING_MODE_CODES,
    CashBalanceCheckpoint,
    FxRate,
    FixedDeposit,
    Instrument,
    InstrumentClassification,
    PositionRegistration,
    Posting,
    Price,
    Transaction,
    ValuationObservation,
)
from app.models.planning import (
    ASSET_ROLE_CODES,
    PLANNING_BUCKET_CODES,
    AllocationTarget,
    RetirementAssumption,
)
from app.models.snapshot import SNAPSHOT_STATUS_CODES, PortfolioSnapshot
from app.models.retirement_plan import RetirementPlan, RetirementIncome

from app.models.retirement_scenario import RetirementScenario
from app.models.fx_reference import FxReferenceSet
from app.models.decimal_conversion import DecimalConversion

__all__ = [
    "DecimalConversion",
    "FxReferenceSet",
    "RetirementScenario",
    "RetirementPlan",
    "RetirementIncome",
    "ACCOUNT_TYPE_CODES",
    "CASH_TRACKING_MODE_CODES",
    "Account",
    "Institution",
    "Portfolio",
    "RelationshipRule",
    "CAPITAL_CERTAINTY_CODES",
    "CREDIT_BAND_CODES",
    "CURRENCY_TREATMENT_CODES",
    "DURATION_BAND_CODES",
    "ECONOMIC_ROLE_CODES",
    "EQUITY_SENSITIVITY_CODES",
    "FIRE_BUCKET_CODES",
    "LEGACY_ECONOMIC_ROLE_CODES",
    "LEGACY_FIRE_BUCKET_CODES",
    "MATURITY_ACTION_CODES",
    "HEDGING_STATUS_CODES",
    "INSTRUMENT_TYPE_CODES",
    "LIQUIDITY_PROFILE_CODES",
    "TRACKING_MODE_CODES",
    "CashBalanceCheckpoint",
    "FxRate",
    "FixedDeposit",
    "Instrument",
    "InstrumentClassification",
    "PositionRegistration",
    "Posting",
    "Price",
    "Transaction",
    "ValuationObservation",
    "ASSET_ROLE_CODES",
    "PLANNING_BUCKET_CODES",
    "AllocationTarget",
    "RetirementAssumption",
    "SNAPSHOT_STATUS_CODES",
    "PortfolioSnapshot",
]


# Model declarations express business bounds; SQL checks operate in integer units.
from sqlalchemy import CheckConstraint, DefaultClause, text
from app.extensions import db
from app.decimal_policy import DECIMAL_FIELDS, scaled_constraint, currency_check

for _table_name, _fields in DECIMAL_FIELDS.items():
    _table = db.metadata.tables[_table_name]
    for _constraint in list(_table.constraints):
        if isinstance(_constraint, CheckConstraint):
            _constraint.sqltext = text(scaled_constraint(str(_constraint.sqltext), _fields))
    for _field, (_places, *_) in _fields.items():
        _table.append_constraint(CheckConstraint(
            f"{_field} IS NULL OR typeof({_field}) = 'integer'", name=f'{_field}_integer'))
    for _field in ('portfolio_share_decimal', 'present_access_decimal'):
        if _field in _fields:
            _table.c[_field].server_default = DefaultClause('100000000')

    for _field, (_places, _precision, _legacy_scale, _currency) in _fields.items():
        if _currency:
            _expression = f"COALESCE({_currency}, reporting_currency_code)" if _table_name == 'portfolios' else _currency
            _table.append_constraint(CheckConstraint(currency_check(_field, _expression), name=f'{_field}_currency_precision'))
