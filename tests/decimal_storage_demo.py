"""Disposable decimal-upgrade browser fixture. CSRF enabled; no owner files."""
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from test_exact_decimals import make_legacy

if __name__ == '__main__':
    with TemporaryDirectory(prefix='lookthrough-decimal-demo-') as directory:
        root=Path(directory)
        path=root/'Legacy.sqlite3'
        make_legacy(path)
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE portfolios SET reporting_currency_code='OMR'")
            connection.execute("UPDATE accounts SET default_currency_code='OMR'")
            connection.execute("UPDATE instruments SET valuation_currency_code='OMR'")
            connection.execute("UPDATE postings SET currency_code='OMR', price_currency_code=CASE WHEN price_currency_code IS NULL THEN NULL ELSE 'OMR' END")
        shutil.copyfile(path,root/'LegacyNoJS.sqlite3')
        application=create_app({'LOCAL_DATABASE_DIRECTORY':directory, 'SECRET_KEY':'synthetic-decimal-browser-only'})
        print(f'DECIMAL_REVIEW_DIRECTORY={root}',flush=True)
        application.run(host='127.0.0.1',port=5197,debug=False,use_reloader=False)
