"""Disposable local chooser fixture. Never opens the owner's instance database."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, create_portfolio_app
from app.extensions import db
from app.services.local_databases import create_database, migrate_file
from retirement_demo import seed_demo

with TemporaryDirectory(prefix='lookthrough-databases-') as directory:
    folder = Path(directory)
    create_database(folder, 'Retirement')
    portfolio_app = create_portfolio_app({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{folder / "Retirement.sqlite3"}'})
    with portfolio_app.app_context():
        seed_demo('funded')
        db.engine.dispose()
    create_database(folder, 'Practice')
    migrate_file(folder / 'Older.sqlite3', 'b5c9d3e7f1a2')
    (folder / 'Unreadable.db').write_text('Synthetic unrelated file')
    application = create_app({'LOCAL_DATABASE_DIRECTORY': directory})
    application.run(host='127.0.0.1', port=5194, debug=False, use_reloader=False)
