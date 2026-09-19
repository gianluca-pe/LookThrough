"""Coherent FX sources, no-network failure recovery, and database persistence."""
import io
import json
import re
from datetime import date
from decimal import Decimal
from html import unescape
from urllib.error import HTTPError, URLError

import pytest
from sqlalchemy import select, func, text

from app.extensions import db
from app.models import FxRate, FxReferenceSet, Portfolio
from app.services import fx_reference as fx
from app.services.fx import resolve_fx
from app.services.backup import export_backup, validate_backup, restore_backup, BackupValidationError
from test_maintenance_ui import _portfolio

DAY = date(2026, 9, 11)
TODAY = date(2026, 9, 13)


def download_blob(rates=None, day=DAY):
    rows = [{"isoCode": c, "eurRate": v, "referenceDate": day.isoformat()}
            for c,v in (rates or {"EUR":"1", "USD":"1.2", "GBP":"0.8", "AED":"4.4"}).items()]
    return json.dumps({"resultsInfo": {"totalRecords": len(rows), "notice": "Foreign currency amount for 1 Euro. Reference rates"}, "latestRates": rows}).encode()


@pytest.fixture
def configured(app):
    app.config["CURRENT_DATE_PROVIDER"] = lambda: TODAY
    return _portfolio(app)


def seed_set(rates=None, day=DAY):
    data = fx.ReferenceData(day, fx.validate_rates(rates or {"EUR":"1", "USD":"1.2", "GBP":"0.8"}))
    fx.save_set(data, source="banca_italia", note="Synthetic source", expected_state=fx.state_id(), replace=True, acknowledge=True)
    return fx.latest_set()


def token(response):
    match = re.search(r'name="token"[^>]*value="([^"]+)"', response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)
    return unescape(match[1])


def test_parser_deduplicates_country_rows_and_preserves_source_precision():
    payload=json.loads(download_blob({"EUR":"1", "USD":"1.1592", "RUB":"N.A."}))
    payload["latestRates"].append(dict(payload["latestRates"][1]))
    payload["resultsInfo"]["totalRecords"] += 1
    parsed=fx.parse_download(json.dumps(payload).encode(), TODAY)
    assert parsed.rates == {"EUR":"1", "USD":"1.1592"}
    assert parsed.unavailable == ("RUB",)
    assert parsed.effective_date == DAY


@pytest.mark.parametrize("raw, message", [
    (b"<html>Unavailable</html>", "read as JSON"),
    (b"[]", "format has changed"),
    (b'{"latestRates": []}', "unrecognized"),
    (b'{"x":1,"x":2}', "read as JSON"),
    (b"x" * (fx.MAX_BYTES+1), "unexpectedly large"),
])
def test_parser_explains_unreadable_or_changed_format(raw, message):
    with pytest.raises(fx.FxReferenceError, match=message): fx.parse_download(raw, TODAY)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e999999", "1e-100", "wat", 1.2, None])
def test_parser_rejects_invalid_rate_values(value):
    with pytest.raises(fx.FxReferenceError):
        fx.parse_download(download_blob({"EUR":"1", "USD":value}), TODAY)


def test_parser_rejects_conflict_mixed_dates_future_and_wrong_direction():
    payload=json.loads(download_blob())
    payload["latestRates"].append(dict(payload["latestRates"][1], eurRate="1.3"))
    payload["resultsInfo"]["totalRecords"] += 1
    with pytest.raises(fx.FxReferenceError, match="conflicting USD"):
        fx.parse_download(json.dumps(payload).encode(), TODAY)
    payload=json.loads(download_blob())
    payload["latestRates"][1]["referenceDate"]="2026-09-10"
    with pytest.raises(fx.FxReferenceError, match="mixes reference dates"):
        fx.parse_download(json.dumps(payload).encode(), TODAY)
    with pytest.raises(fx.FxReferenceError, match="future"):
        fx.parse_download(download_blob(day=date(2026,9,14)), TODAY)
    payload["resultsInfo"]["notice"]="EUR per unit of currency"
    with pytest.raises(fx.FxReferenceError, match="direction"):
        fx.parse_download(json.dumps(payload).encode(), TODAY)


def test_anchor_resolution_preserves_legacy_dates_and_overrules_conflicting_pairs(app):
    with app.app_context():
        db.session.add(FxRate(effective_date=date(2026,9,1), base_currency_code="USD", quote_currency_code="GBP", quote_per_base_amount=Decimal("0.9")))
        db.session.commit()
        seed_set()
        forward=resolve_fx("USD","GBP",DAY,stale_days=7)
        inverse=resolve_fx("GBP","USD",DAY,stale_days=7)
        assert forward.rate == Decimal("0.8")/Decimal("1.2")
        assert inverse.rate == Decimal("1.5")
        assert forward.path == ("USD","EUR","GBP")
        assert forward.effective_date == DAY and forward.source_mode == "banca_italia"
        assert resolve_fx("USD","GBP",date(2026,9,10),stale_days=20).rate == Decimal("0.9")
        assert resolve_fx("EUR","USD",DAY,stale_days=7).rate == Decimal("1.2")
        assert resolve_fx("USD","EUR",DAY,stale_days=7).rate == Decimal(1)/Decimal("1.2")
        assert resolve_fx("USD","GBP",date(2026,9,20),stale_days=7).status == "stale"
        assert db.session.scalar(select(func.count()).select_from(FxRate)) == 1


def test_missing_reference_currency_never_uses_a_conflicting_legacy_fallback(app):
    with app.app_context():
        db.session.add(FxRate(effective_date=DAY,base_currency_code="THB",quote_currency_code="USD",quote_per_base_amount=Decimal("0.1")))
        db.session.commit()
        seed_set()
        result=resolve_fx("THB","USD",DAY,stale_days=7)
        assert result.rate is None and "Settings" in result.missing_reason
        assert result.effective_date == DAY


def test_revision_noop_large_change_and_stale_review_are_atomic(app):
    with app.app_context():
        first=seed_set()
        first_id=first.id
        data=fx.ReferenceData(DAY,fx.set_rates(first))
        assert not fx.save_set(data,source="banca_italia",note="Same",expected_state=first_id)
        revised=fx.ReferenceData(DAY,{"EUR":"1","USD":"2","GBP":"0.8"})
        with pytest.raises(fx.FxReferenceError,match="Confirm replacement"):
            fx.save_set(revised,source="manual",note="Correction",expected_state=first_id)
        with pytest.raises(fx.FxReferenceError,match="10%"):
            fx.save_set(revised,source="manual",note="Correction",expected_state=first_id,replace=True)
        assert fx.state_id() == first_id
        fx.save_set(revised,source="manual",note="Correction",expected_state=first_id,replace=True,acknowledge=True)
        assert fx.set_rates(db.session.get(FxReferenceSet,first_id))["USD"] == "1.2"
        assert resolve_fx("EUR","USD",DAY,stale_days=7).rate == Decimal("2")
        with pytest.raises(fx.FxReferenceError,match="another tab"):
            fx.save_set(data,source="manual",note="Old review",expected_state=first_id,replace=True,acknowledge=True)


def test_settings_get_is_offline_and_download_review_does_not_save(client,app,configured,monkeypatch):
    calls=[]
    def fetch(today):
        calls.append(today)
        return fx.parse_download(download_blob(),today)
    monkeypatch.setattr("app.fx_settings.download_reference_rates",fetch)
    assert client.get('/settings').status_code == 200
    assert calls == []
    page=client.post('/settings/fx/download')
    assert page.status_code == 200
    assert calls == [TODAY]
    with app.app_context(): assert fx.state_id() == 0
    response=client.post('/settings/fx/save',data={"token":token(page)})
    assert response.status_code == 302
    with app.app_context():
        assert fx.latest_set().effective_date == DAY
        assert fx.set_rates(fx.latest_set()) == {"EUR":"1", "USD":"1.2"}
    response=client.post('/settings/fx/download',follow_redirects=True)
    assert b'already saved' in response.data
    assert len(calls) == 2


@pytest.mark.parametrize("reason", ["Could not connect securely", "The download could not be read as JSON", "The JSON format has changed"])
def test_failure_preserves_sources_and_explains_manual_recovery(client,app,configured,monkeypatch,reason):
    with app.app_context(): seed_set(); before=export_backup()
    def fail(today): raise fx.FxReferenceError(reason)
    monkeypatch.setattr("app.fx_settings.download_reference_rates",fail)
    page=client.post('/settings/fx/download')
    assert page.status_code == 502 and reason.encode() in page.data
    assert b'/settings/fx/manual' in page.data and b'saved rates and their dates are unchanged' in page.data
    with app.app_context():
        assert json.loads(export_backup())["tables"] == json.loads(before)["tables"]


def test_manual_new_date_requires_complete_set_and_preserves_errors(client,app,configured):
    page=client.post('/settings/fx/manual',data={"effective_date":"2026-09-13","source_note":"Bank reference", "state":"0"})
    assert page.status_code == 400
    assert b'Enter this rate for a complete set' in page.data
    assert b'Bank reference' in page.data and b'href="#rate_USD"' in page.data
    assert re.search(rb'<input[^>]*autofocus[^>]*id="rate_USD"',page.data)
    good=client.post('/settings/fx/manual',data={"effective_date":"2026-09-13","source_note":"Bank reference", "state":"0","rate_USD":"1.25"})
    assert good.status_code == 200
    saved=client.post('/settings/fx/save',data={"token":token(good)})
    assert saved.status_code == 302
    with app.app_context():
        assert fx.latest_set().source == "manual"
        assert fx.set_rates(fx.latest_set()) == {"EUR":"1","USD":"1.25"}


def test_manual_same_date_correction_retains_other_currencies(client,app,configured):
    with app.app_context(): first_id=seed_set().id
    page=client.post('/settings/fx/manual',data={"effective_date":DAY.isoformat(),"source_note":"Checked source","state":str(first_id),"rate_USD":"1.21"})
    review_token=token(page)
    failure=client.post('/settings/fx/save',data={"token":review_token})
    assert failure.status_code == 400 and b'Confirm replacement' in failure.data
    assert client.post('/settings/fx/save',data={"token":review_token,"replace":"y"}).status_code == 302
    with app.app_context():
        current=fx.latest_set()
        assert fx.set_rates(current)["GBP"] == "0.8"
        assert fx.set_rates(current)["USD"] == "1.21"
        assert db.session.get(FxReferenceSet,first_id) is not None


def test_incomplete_download_cannot_be_saved_even_with_direct_post(client,app,configured,monkeypatch):
    monkeypatch.setattr("app.fx_settings.download_reference_rates",lambda today: fx.ReferenceData(DAY,{"EUR":"1","GBP":"0.8"}))
    page=client.post('/settings/fx/download')
    assert b'Required currencies missing: USD' in page.data
    assert client.post('/settings/fx/save',data={"token":token(page)}).status_code == 400
    with app.app_context(): assert fx.state_id() == 0


def test_review_cannot_be_replayed_after_another_save_or_tampered(client,app,configured,monkeypatch):
    monkeypatch.setattr("app.fx_settings.download_reference_rates",lambda today: fx.parse_download(download_blob(),today))
    pending=token(client.post('/settings/fx/download'))
    with app.app_context(): seed_set()
    page=client.post('/settings/fx/save',data={"token":pending},follow_redirects=True)
    assert b'Saved rates changed after this review' in page.data
    page=client.post('/settings/fx/save',data={"token":"bad"},follow_redirects=True)
    assert b'FX review expired or is invalid' in page.data


def test_raw_pair_forms_cannot_shadow_reference_set(client,app,configured):
    with app.app_context(): seed_set()
    page=client.post('/values/fx',data={"effective_date":DAY.isoformat(),"base_currency_code":"USD","quote_currency_code":"EUR","quote_per_base_amount":"0.8"})
    assert b'individual pairs cannot override' in page.data
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(FxRate)) == 0
        from app.services.routine_updates import save_fx_updates, RoutineUpdateValidationError
        with pytest.raises(RoutineUpdateValidationError):
            save_fx_updates(db.session.get(Portfolio,configured), DAY, {("EUR","USD"):Decimal("5")})
    assert b'Review or update FX rates' in client.get('/values?as_of=2026-09-13').data


def test_overview_bounds_ignore_extra_downloaded_currencies(client,app,configured):
    with app.app_context(): seed_set({"EUR":"1","USD":"1.2","THB":"38"})
    page=client.get('/overview?as_of=2026-09-13&ccy=THB')
    assert b'Choose a currency used by your accounts or holdings' in page.data
    select_html=re.search(rb'<select id="ccy".*?</select>',page.data,re.S).group()
    assert b'THB' not in select_html
    assert b'reporting in USD' in page.data


def test_reference_sets_backup_roundtrip_and_version9_compatibility(app,configured):
    with app.app_context():
        first=seed_set()
        seed_set({"EUR":"1","USD":"1.22"},day=DAY)
        backup=export_backup()
        assert json.loads(backup)["version"] == 11
        validated=validate_backup(backup)
        restore_backup(validated)
        assert db.session.scalar(select(func.count()).select_from(FxReferenceSet)) == 2
        assert resolve_fx("EUR","USD",DAY,stale_days=7).rate == Decimal("1.22")
        legacy=json.loads(backup)
        legacy["version"]=9
        legacy["tables"].pop("decimal_conversions", None)
        legacy["tables"].pop("fx_reference_sets")
        restore_backup(validate_backup(json.dumps(legacy).encode()))
        assert fx.state_id() == 0
        bad=json.loads(backup)
        bad["tables"]["fx_reference_sets"][0]["rates_json"]='{"EUR":"1","USD":"NaN"}'
        with pytest.raises(BackupValidationError): validate_backup(json.dumps(bad).encode())


def test_previous_schema_upgrade_preserves_legacy_rates(unmigrated_app):
    runner=unmigrated_app.test_cli_runner()
    assert runner.invoke(args=['db','upgrade','c6d0e4f8a2b3']).exit_code == 0
    with unmigrated_app.app_context():
        db.session.execute(text("INSERT INTO fx_rates (effective_date,base_currency_code,quote_currency_code,quote_per_base_amount,created_at,updated_at) VALUES ('2026-09-01','USD','GBP',0.8,'2026-09-01','2026-09-01')"))
        db.session.commit()
    result=runner.invoke(args=['db','upgrade'])
    assert result.exit_code == 0,result.output
    with unmigrated_app.app_context():
        assert fx.state_id() == 0
        assert resolve_fx('USD','GBP',DAY,stale_days=30).rate == Decimal('0.8')


@pytest.mark.parametrize("exception,message", [
    (TimeoutError(),"timed out"),
    (URLError("dns"),"connect securely"),
    (HTTPError(fx.SOURCE_URL,503,"Unavailable",{},None),"HTTP 503"),
])
def test_network_failures_are_classified_without_retries(monkeypatch,exception,message):
    class Failing:
        def open(self,*args,**kwargs): raise exception
    monkeypatch.setattr(fx,'build_opener',lambda *args:Failing())
    with pytest.raises(fx.FxReferenceError,match=message): fx.download_reference_rates(TODAY)


def test_download_makes_one_fixed_public_request(monkeypatch):
    calls=[]
    class Download:
        def open(self,request,timeout):
            calls.append((request.full_url,dict(request.header_items()),timeout))
            return io.BytesIO(download_blob())
    monkeypatch.setattr(fx,'build_opener',lambda *args: Download())
    assert fx.download_reference_rates(TODAY).rates['USD']=='1.2'
    assert len(calls)==1 and calls[0][0]==fx.SOURCE_URL
    assert 'Authorization' not in calls[0][1]


def test_write_endpoints_require_csrf(client,app,configured):
    app.config['WTF_CSRF_ENABLED']=True
    for path in ['/settings/fx/download','/settings/fx/save','/settings/fx/manual']:
        assert client.post(path,data={}).status_code==400


def test_reference_review_is_bound_to_individual_database(client,app,configured,monkeypatch,tmp_path):
    from app import create_app
    monkeypatch.setattr("app.fx_settings.download_reference_rates",lambda today: fx.parse_download(download_blob(),today))
    pending=token(client.post('/settings/fx/download'))
    other=create_app({'TESTING':True,'SECRET_KEY':app.secret_key,'WTF_CSRF_ENABLED':False,
                      'SQLALCHEMY_DATABASE_URI':f'sqlite:///{tmp_path}/other.sqlite3',
                      'CURRENT_DATE_PROVIDER':lambda:TODAY})
    try:
        with other.app_context(): db.create_all()
        _portfolio(other)
        page=other.test_client().post('/settings/fx/save',data={'token':pending},follow_redirects=True)
        assert b'belongs to another database' in page.data
        with other.app_context(): assert fx.state_id()==0
        with app.app_context(): assert fx.state_id()==0
    finally:
        with other.app_context(): db.session.remove();db.engine.dispose()


def test_actual_holding_currency_fd_and_planning_dependencies_are_disclosed(app):
    from app.models import Instrument, PositionRegistration, Transaction, Posting
    from app.services.retirement_plans import save_income
    from test_two_tier_retirement import seed, income_data
    with app.app_context():
        portfolio,account,plan=seed(terminal_legacy_target_amount=Decimal('100'),terminal_legacy_target_currency_code='CHF')
        save_income(portfolio.id,income_data(currency_code='SGD'))
        fd=Instrument(portfolio_id=portfolio.id,name='EUR deposit',instrument_type='fixed_deposit',valuation_currency_code='EUR',is_active=True)
        db.session.add(fd);db.session.flush()
        db.session.add(PositionRegistration(account_id=account.id,instrument_id=fd.id,tracking_mode='transaction_tracked',opening_date=DAY))
        tx=Transaction(portfolio_id=portfolio.id,effective_date=DAY,transaction_type='opening_position',note='Synthetic deposit')
        db.session.add(tx);db.session.flush()
        db.session.add(Posting(transaction_id=tx.id,account_id=account.id,posting_kind='instrument',instrument_id=fd.id,currency_code='EUR',quantity_delta=Decimal('1000')))
        db.session.commit()
        uses=fx.currency_uses(portfolio,TODAY)
        assert {'EUR','USD','CHF','SGD'} <= uses.keys()
        assert 'Holdings and cash' in uses['EUR']
        assert 'Retirement income' in uses['SGD'] and 'Legacy goal' in uses['CHF']
        choices=fx.reporting_choices(portfolio,TODAY)
        assert 'EUR' in choices and 'CHF' not in choices and 'SGD' not in choices


def test_reference_conversion_flows_through_summary_spending_and_relationship(app):
    from test_relationship_fx_updates import _bank
    from app.models import RelationshipRule
    from app.services.portfolio_summary import build_portfolio_summary
    from app.services.relationships import evaluate_relationship_rule
    from app.services.spending import build_spending_value
    with app.app_context():
        portfolio=Portfolio(name='Reference totals',reporting_currency_code='USD',annual_spending_amount=Decimal('100'),annual_spending_currency_code='GBP')
        db.session.add(portfolio);db.session.flush()
        _bank(portfolio,'GBP threshold','GBP',source='EUR')
        db.session.commit()
        seed_set()
        summary=build_portfolio_summary(portfolio,TODAY)
        assert summary['included_reporting_amount']==Decimal('1200')
        spending=build_spending_value(portfolio,TODAY,'USD')
        assert spending['reporting_amount']==Decimal('150')
        rule=db.session.scalar(select(RelationshipRule))
        result=evaluate_relationship_rule(portfolio,rule,TODAY)
        assert result.eligible_value_amount==Decimal('800')
        assert result.status=='current'
