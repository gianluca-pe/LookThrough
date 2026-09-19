"""Synthetic browser workspace; the FX download is a deterministic local fake."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from app.models import Account, Instrument, Portfolio, PositionRegistration, ValuationObservation
from app.services.fx_reference import ReferenceData, FxReferenceError
from test_maintenance_ui import _portfolio
import app.fx_settings as routes


if __name__ == '__main__':
    with TemporaryDirectory(prefix='lookthrough-fx-demo-') as directory:
        app = create_app({'SQLALCHEMY_DATABASE_URI':f'sqlite:///{directory}/demo.sqlite3',
                          'SECRET_KEY':'synthetic-fx-demo',
                          'CURRENT_DATE_PROVIDER':lambda:date(2026,9,13),
                          'RESTORE_STAGING_DIRECTORY':f'{directory}/restore'})
        with app.app_context(): db.create_all()
        _portfolio(app)
        with app.app_context():
            p=db.session.get(Portfolio,1)
            p.name='FX workspace · synthetic portfolio'
            p.reporting_currency_code='EUR'
            account=db.session.get(Account,1)
            account.cash_tracking_mode='included_in_aggregate'
            account.is_multicurrency=True
            asset=Instrument(portfolio_id=1,name='AED statement holding',instrument_type='other',valuation_currency_code='AED',is_active=True)
            db.session.add(asset);db.session.flush()
            reg=PositionRegistration(account_id=1,instrument_id=asset.id,tracking_mode='statement_valued',opening_date=date(2026,8,1))
            db.session.add(reg);db.session.flush()
            db.session.add(ValuationObservation(position_registration_id=reg.id,effective_date=date(2026,9,11),native_value_amount=Decimal('25000'),currency_code='AED'))
            db.session.commit()
        count=[0]
        def synthetic_download(today):
            count[0]+=1
            if count[0]>=3:
                raise FxReferenceError('The JSON format has changed: required currency fields are missing. The provider may have changed its format.')
            return ReferenceData(date(2026,9,11),{'EUR':'1','USD':'1.2','AED':'4.4','THB':'38.329'})
        routes.download_reference_rates=synthetic_download
        @app.context_processor
        def dataset_context():
            return {'dataset_filename':'Synthetic FX.sqlite3', 'dataset_path':f'{directory}/demo.sqlite3',
                    'database_chooser_url':'/settings'}
        app.run(host='127.0.0.1',port=5195,use_reloader=False)
