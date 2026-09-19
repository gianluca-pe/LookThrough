"""Exact sources, explicit precision and audited legacy migration."""
from decimal import Decimal as D
import io
import json
import sqlite3

import pytest
from sqlalchemy import select, text, inspect
from sqlalchemy.exc import IntegrityError, StatementError

from app import create_portfolio_app
from app.extensions import db
from app.models import Posting, Portfolio, CashBalanceCheckpoint, Price, FxRate, DecimalConversion, PositionRegistration
from app.models.types import ScaledDecimal
from app.decimal_policy import DECIMAL_FIELDS, exact_units, money
from app.services.activity import post_trade, preview_trade, ActivityValidationError
from app.services.backup import export_backup, validate_backup, restore_backup, BackupValidationError
from app.services.positions import quantity_as_of
from app.services.local_databases import migrate_file, inspect_database, upgrade_database, DatabaseError
from app.template_filters import money_amount
from test_activity_service import _base_records, _command, TRADE_DATE
from legacy_decimal_fixture import legacy_decimal_types


@pytest.mark.parametrize('amount,places,units', [
    ('100000.01',6,100000010000), ('123.456789',6,123456789),
    ('0.12345678',8,12345678), ('100000.001',3,100000001),
    ('-12.34',3,-12340), ('9223372036854775.807',3,2**63-1),
])
def test_exact_integer_codec(amount, places, units):
    codec=ScaledDecimal(places)
    assert codec.process_bind_param(D(amount),None)==units
    assert codec.process_result_value(units,None)==D(amount)


@pytest.mark.parametrize('value', [D('0.0000001'), D('9223372036854.775808'), 1.25, True, D('NaN'), D('Infinity')])
def test_codec_rejects_excess_precision_overflow_and_nondecimal_values(value):
    with pytest.raises((TypeError, ValueError)):
        ScaledDecimal(6).process_bind_param(value,None)


def test_all_source_numeric_fields_are_exact_integer_types():
    assert sum(map(len,DECIMAL_FIELDS.values()))==42
    for table, fields in DECIMAL_FIELDS.items():
        for name,(places,*_) in fields.items():
            assert isinstance(db.metadata.tables[table].c[name].type,ScaledDecimal)
            assert db.metadata.tables[table].c[name].type.places==places


def test_original_f04_exact_quantity_sale_and_accumulation(app):
    with app.app_context():
        records=_base_records()
        for _ in range(3):
            post_trade(_command(*records,activity_type='buy',quantity='100000.01',price='1.123456'))
        db.session.expire_all()
        registration=db.session.scalar(select(PositionRegistration))
        assert quantity_as_of(registration,TRADE_DATE)==D('300000.03')
        assert db.session.scalar(select(Posting.quantity_delta).where(Posting.posting_kind=='instrument').limit(1))==D('100000.01')
        assert db.session.execute(text("SELECT typeof(quantity_delta), quantity_delta FROM postings WHERE posting_kind='instrument' LIMIT 1")).one()==('integer',100000010000)
        post_trade(_command(*records,activity_type='sell',quantity='300000.03',price='1.123456'))
        assert quantity_as_of(registration,TRADE_DATE)==D('0')
        original=json.loads(export_backup())['tables']
        restore_backup(validate_backup(export_backup()))
        assert json.loads(export_backup())['tables']==original


@pytest.mark.parametrize('currency,price,expected',[('USD','1.005','1.01'),('KWD','1.0055','1.006'),('OMR','1.0015','1.002'),('BHD','1.0025','1.003'),('JPY','1.5','2')])
def test_posted_trade_rounds_once_to_currency_minor_units(app,currency,price,expected):
    with app.app_context():
        records=_base_records(account_currency=currency,instrument_currency=currency)
        result=post_trade(_command(*records,activity_type='buy',quantity='1',price=price))
        assert result.preview.gross_amount==D(expected)
        assert db.session.scalar(select(Posting.cash_amount_delta).where(Posting.posting_kind=='cash'))==-D(expected)
        assert money_amount(D(expected),currency=currency)==expected


@pytest.mark.parametrize('field,value',[('quantity','1.0000001'),('unit_price','1.0000001'),('fee_amount','0.001')])
def test_trade_precision_failure_is_field_linked_and_atomic(app,field,value):
    from dataclasses import replace
    with app.app_context():
        records=_base_records()
        command=_command(*records,activity_type='buy',quantity='1',price='1')
        with pytest.raises(ActivityValidationError) as error:
            post_trade(replace(command,**{field:D(value)}))
        assert error.value.field==field
        assert not db.session.scalar(select(Posting.id))


def test_money_currency_precision_is_enforced_by_database(app):
    with app.app_context():
        portfolio,*_=_base_records()
        with pytest.raises(IntegrityError):
            db.session.execute(text('UPDATE portfolios SET annual_spending_amount=1001 WHERE id=:id'),{'id':portfolio.id})
        db.session.rollback()
        with pytest.raises(IntegrityError):
            db.session.execute(text('UPDATE portfolios SET annual_spending_amount=1000.5'))
        db.session.rollback()
        assert db.session.scalar(select(Portfolio.annual_spending_amount))==D('48000')


def make_legacy(path):
    migrate_file(path,'d7e1f5a9b3c6')
    application=create_portfolio_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':f'sqlite:///{path}'})
    with application.app_context(),legacy_decimal_types():
        records=_base_records()
        post_trade(_command(*records,activity_type='buy',quantity='100000.01',price='1'))
        assert db.session.scalar(select(Posting.quantity_delta).where(Posting.posting_kind=='instrument'))==D('100000.009999999995')
    with application.app_context():
        db.engine.dispose()
    return application


def test_chooser_upgrade_preserves_recovery_and_audits_changed_values(tmp_path):
    path=tmp_path/'Legacy.sqlite3'
    application=make_legacy(path)
    original=path.read_bytes()
    with sqlite3.connect(path) as connection:
        original_dump=list(connection.iterdump())
    candidate=inspect_database(tmp_path,path.name)
    backup=upgrade_database(tmp_path,path.name,candidate.identity,candidate.revision)
    with sqlite3.connect(backup) as connection:
        assert list(connection.iterdump())==original_dump
    with application.app_context():
        assert db.session.scalar(select(Posting.quantity_delta).where(Posting.posting_kind=='instrument'))==D('100000.01')
        audit=db.session.scalar(select(DecimalConversion).where(DecimalConversion.field_name=='quantity_delta'))
        assert (audit.before_value,audit.after_value)==('100000.009999999995','100000.010000')
        assert not db.session.execute(text('PRAGMA foreign_key_check')).all()
        blob=export_backup()
        original_tables=json.loads(blob)['tables']
        restore_backup(validate_backup(blob))
        assert json.loads(export_backup())['tables']==original_tables
        assert application.test_cli_runner().invoke(args=['db','check']).exit_code==0
        db.session.remove()
        db.engine.dispose()


def test_rounding_nonzero_quantity_to_zero_blocks_upgrade_without_changing_original(tmp_path):
    path=tmp_path/'TooSmall.sqlite3'
    make_legacy(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE postings SET quantity_delta=0.0000001 WHERE posting_kind='instrument'")
    original=path.read_bytes()
    with sqlite3.connect(path) as connection:
        original_dump=list(connection.iterdump())
    candidate=inspect_database(tmp_path,path.name)
    with pytest.raises(DatabaseError):
        upgrade_database(tmp_path,path.name,candidate.identity,candidate.revision)
    assert path.read_bytes()==original
    with sqlite3.connect(next((tmp_path/'upgrade-backups').glob('*.sqlite3'))) as connection:
        assert list(connection.iterdump())==original_dump


def test_old_backup_rounding_is_audited_and_new_format_rejects_excess_precision(app,client):
    with app.app_context():
        records=_base_records()
        post_trade(_command(*records,activity_type='buy',quantity='100000.01',price='1'))
        payload=json.loads(export_backup())
        payload['version']=10
        payload['tables'].pop('decimal_conversions')
        row=next(row for row in payload['tables']['postings'] if row['posting_kind']=='instrument')
        row['quantity_delta']='100000.009999999995'
        validated=validate_backup(json.dumps(payload).encode())
        assert validated.decoded_tables['decimal_conversions'][0]['after_value']=='100000.010000'
        restore_backup(validated)
        assert db.session.scalar(select(Posting.quantity_delta).where(Posting.posting_kind=='instrument'))==D('100000.01')
        payload=json.loads(export_backup())
        payload['tables']['postings'][0]['quantity_delta']='1.0000001'
        with pytest.raises(BackupValidationError):
            validate_backup(json.dumps(payload).encode())


def test_quantity_error_route_preserves_input_and_uses_linked_error(app,client):
    with app.app_context():
        records=_base_records()
        ids=[row.id for row in records]
    response=client.post('/activity/new?type=buy',data={
        'activity_type':'buy','effective_date':TRADE_DATE.isoformat(),'account_id':ids[1],
        'instrument_id':ids[2],'quantity_mode':'entered','quantity':'1.0000001',
        'unit_price':'1.234567','fee_amount':'0','post_trade':'Record Buy'})
    assert response.status_code==200
    assert b'Use at most 6 decimal places' in response.data
    assert b'href="#quantity"' in response.data
    assert b'value="1.0000001"' in response.data


def test_failure_mid_schema_rebuild_keeps_original_and_recovery(tmp_path):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    path = tmp_path / 'Interrupted.sqlite3'
    make_legacy(path)
    original = path.read_bytes()
    candidate = inspect_database(tmp_path, path.name)
    reached = []
    def fail(connection, cursor, statement, parameters, context, executemany):
        if 'CREATE TABLE _alembic_tmp_postings' in statement:
            reached.append(True)
            raise RuntimeError('Injected failure after earlier tables were rebuilt')
    event.listen(Engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(DatabaseError):
            upgrade_database(tmp_path,path.name,candidate.identity,candidate.revision)
    finally:
        event.remove(Engine, 'before_cursor_execute', fail)
    assert reached
    assert path.read_bytes() == original
    assert inspect_database(tmp_path,path.name).revision == 'd7e1f5a9b3c6'
    assert len(list((tmp_path/'upgrade-backups').glob('*.sqlite3'))) == 1


def test_rounding_a_balanced_activity_into_an_imbalance_blocks_conversion(app):
    with app.app_context():
        records = _base_records()
        post_trade(_command(*records,activity_type='buy',quantity='3',price='1',fee='0.01'))
        payload = json.loads(export_backup())
        payload['version'] = 10
        payload['tables'].pop('decimal_conversions')
        amounts = {'cash':'-3.010', 'clearing':'3.005', 'expense':'0.005'}
        for row in payload['tables']['postings']:
            if row['posting_kind'] in amounts:
                row['cash_amount_delta'] = amounts[row['posting_kind']]
        before = export_backup()
        with pytest.raises(BackupValidationError, match='balance after rounding'):
            validate_backup(json.dumps(payload).encode())
        assert json.loads(export_backup())['tables'] == json.loads(before)['tables']


def test_fx_form_accepts_six_places_and_rejects_seven(app):
    from app.position_forms import FxRateForm
    from werkzeug.datastructures import MultiDict
    with app.test_request_context():
        data = dict(base_currency_code='USD',quote_currency_code='EUR',effective_date='2026-09-19',quote_per_base_amount='0.912345')
        assert FxRateForm(MultiDict(data)).validate()
        data['quote_per_base_amount'] = '0.9123456'
        form = FxRateForm(MultiDict(data))
        assert not form.validate()
        assert '6 decimal places' in form.quote_per_base_amount.errors[0]


def test_reference_source_precision_is_retained_but_unusable_tiny_rate_is_missing(app):
    from app.models import FxReferenceSet
    from app.services.fx import resolve_fx
    from app.services.fx_reference import rate_text, FxReferenceError
    with app.app_context():
        rates = {'EUR':'1', 'USD':'1.1234567', 'OMR':'0.00000001'}
        db.session.add(FxReferenceSet(effective_date=TRADE_DATE, rates_json=json.dumps(rates),source='banca_italia',source_note='Synthetic'))
        db.session.commit()
        assert resolve_fx('EUR','USD',TRADE_DATE,stale_days=7).rate == D('1.123457')
        result = resolve_fx('OMR','USD',TRADE_DATE,stale_days=7)
        assert result.rate is None and result.status == 'missing'
        assert json.loads(db.session.scalar(select(FxReferenceSet)).rates_json) == rates
        with pytest.raises(FxReferenceError,match='6 decimal places'):
            rate_text('1.1234567',manual=True)


def test_inverse_correction_that_would_be_zero_changes_nothing(app):
    from app.services.fx import fx_correction_context
    with app.app_context():
        row = FxRate(effective_date=TRADE_DATE,base_currency_code='EUR',quote_currency_code='USD',quote_per_base_amount=D('1'))
        db.session.add(row)
        db.session.commit()
        with pytest.raises(ValueError,match='inverse rate would round to zero'):
            fx_correction_context((row,),base_currency='USD',quote_currency='EUR',quote_per_base=D('3000000'))
        assert row.quote_per_base_amount == 1 and row.source_note is None


@pytest.mark.parametrize('currency,expected', [('OMR','240.001'), ('JPY','240')])
def test_affordability_allowance_uses_currency_minor_unit(app,currency,expected):
    from test_retirement_affordability import seed, solve, _trial
    from app.models import Account, Instrument, ValuationObservation
    with app.app_context():
        portfolio, _, _ = seed('1000')
        # Move the complete synthetic source into the tested currency.
        portfolio.reporting_currency_code = currency
        for row in db.session.scalars(select(Account)):
            row.default_currency_code = currency
        for row in db.session.scalars(select(Instrument)):
            row.valuation_currency_code = currency
        for row in db.session.scalars(select(ValuationObservation)):
            row.currency_code = currency
        for row in db.session.scalars(select(CashBalanceCheckpoint)):
            row.currency_code = currency
            if currency == 'OMR':
                row.confirmed_balance_amount = D('1000.003')
        db.session.commit()
        result,basis = solve(portfolio,currency_code=currency)
        assert result['flexible_amount'] == D(expected)
        assert _trial(basis,D(expected) + (D('0.001') if currency=='OMR' else 1))['final_value_amount'] < 100
