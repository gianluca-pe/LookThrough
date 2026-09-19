"""Disposable-only acceptance checks for intentional local dataset selection."""
from contextlib import closing
from decimal import Decimal
from html import unescape
from pathlib import Path
import re
import sqlite3

import pytest
from sqlalchemy import select

from app import create_app, create_portfolio_app
from app.extensions import db
from app.models import Portfolio
from app.services import local_databases as local


@pytest.fixture
def launcher(tmp_path):
    return create_app({'TESTING': True, 'LOCAL_DATABASE_DIRECTORY': str(tmp_path)})


def inputs(response):
    return {name: unescape(value) for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', response.get_data(as_text=True))}


def choose(client, filename):
    page = client.get('/').get_data(as_text=True)
    # Select the exact card, including when two portfolios have the same name.
    cards = re.findall(r'<section class="card database-card".*?</section>', page, re.S)
    card = next(c for c in cards if filename in c)
    data = {n: unescape(v) for n, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', card)}
    return client.post('/databases/open', data=data)


def seed(folder, filename, name='Synthetic portfolio'):
    local.create_database(folder, filename.removesuffix('.sqlite3'))
    application = create_portfolio_app({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{folder / filename}'})
    with application.app_context():
        db.session.add(Portfolio(name=name, reporting_currency_code='USD', annual_spending_amount=Decimal('42000')))
        db.session.commit()
        db.engine.dispose()


def test_empty_and_single_database_always_show_chooser_without_writes(launcher, tmp_path):
    client = launcher.test_client()
    assert b'No databases were found' in client.get('/').data
    assert client.get('/health').data == b'ok\n'
    assert list(tmp_path.iterdir()) == []
    seed(tmp_path, 'Real.sqlite3')
    before = local.fingerprint(tmp_path / 'Real.sqlite3')
    response = client.get('/')
    assert b'Choose a database' in response.data and b'Synthetic portfolio' in response.data
    assert b'Create a new database' in response.data
    assert client.get('/overview').location == '/'
    assert local.fingerprint(tmp_path / 'Real.sqlite3') == before
    assert [p.name for p in tmp_path.iterdir()] == ['Real.sqlite3']


def test_creation_preview_collision_and_csrf(launcher, tmp_path):
    client = launcher.test_client()
    assert client.post('/databases/new', data={'name': 'Practice'}).status_code == 400
    data = inputs(client.get('/databases/new')) | {'name': 'Practice'}
    preview = client.post('/databases/new', data=data)
    assert b'Practice.sqlite3' in preview.data and not list(tmp_path.iterdir())
    altered = client.post('/databases/new', data=inputs(preview) | {'name': 'Changed', 'confirm': 'yes'})
    assert b'Changed.sqlite3' in altered.data and not list(tmp_path.iterdir())
    response = client.post('/databases/new', data=inputs(preview) | {'name': 'Practice', 'confirm': 'yes'})
    assert response.status_code == 303
    setup = client.get(response.location, follow_redirects=True)
    assert b'Set up your portfolio' in setup.data and b'Practice.sqlite3' in setup.data
    path = tmp_path / 'Practice.sqlite3'
    before = local.fingerprint(path)
    response = client.post('/databases/new', data=data | {'confirm': 'yes', 'preview_filename': 'Practice.sqlite3'})
    assert response.status_code == 400 and b'filename already exists' in response.data
    assert local.fingerprint(path) == before
    assert local.inspect_database(tmp_path, path.name).status == 'empty'


@pytest.mark.parametrize('name', ['../outside', '/tmp/outside', 'a/b', 'a\\b', 'name.sqlite3', '', '.hidden', 'a' * 65])
def test_filename_rejection(tmp_path, name):
    with pytest.raises(local.DatabaseError):
        local.create_database(tmp_path, name)
    assert list(tmp_path.iterdir()) == []


def test_discovery_rejects_foreign_newer_corrupt_and_broken_schema(tmp_path):
    (tmp_path / 'corrupt.db').write_text('not sqlite')
    with closing(sqlite3.connect(tmp_path / 'foreign.sqlite')) as connection:
        connection.execute('CREATE TABLE unrelated (id INTEGER)')
        connection.commit()
    local.create_database(tmp_path, 'Newer')
    with closing(sqlite3.connect(tmp_path / 'Newer.sqlite3')) as connection:
        connection.execute("UPDATE alembic_version SET version_num='future-version'")
        connection.commit()
    local.create_database(tmp_path, 'Broken')
    with closing(sqlite3.connect(tmp_path / 'Broken.sqlite3')) as connection:
        connection.execute('DROP TABLE prices')
        connection.commit()
    (tmp_path / 'linked.sqlite3').symlink_to(tmp_path / 'Newer.sqlite3')
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested' / 'ignored.db').write_text('ignored')
    before = {p.name: local.fingerprint(p) for p in tmp_path.iterdir() if p.is_file()}
    candidates = local.discover(tmp_path)
    assert len(candidates) == 4
    assert all(c.status == 'unavailable' for c in candidates)
    assert 'newer or unknown' in next(c.reason for c in candidates if c.filename == 'Newer.sqlite3')
    assert before == {p.name: local.fingerprint(p) for p in tmp_path.iterdir() if p.is_file()}


def test_pending_wal_is_not_ignored_or_modified(tmp_path):
    local.create_database(tmp_path, 'Busy')
    wal = tmp_path / 'Busy.sqlite3-wal'
    wal.write_bytes(b'pending')
    candidate = local.inspect_database(tmp_path, 'Busy.sqlite3')
    assert candidate.status == 'unavailable' and 'pending SQLite changes' in candidate.reason
    assert wal.read_bytes() == b'pending'
    assert not (tmp_path / 'Busy.sqlite3-shm').exists()


def test_failed_creation_never_publishes_partial_file(tmp_path, monkeypatch):
    def fail(path):
        path.write_bytes(b'partial')
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(local, 'migrate_file', fail)
    with pytest.raises(local.DatabaseError, match='could not be created'):
        local.create_database(tmp_path, 'Practice')
    assert list(tmp_path.iterdir()) == []


def test_upgrade_preview_backup_and_round_trip(launcher, tmp_path):
    path = tmp_path / 'Older.sqlite3'
    local.migrate_file(path, 'b5c9d3e7f1a2')
    before = local.fingerprint(path)
    client = launcher.test_client()
    preview = choose(client, path.name)
    assert preview.status_code == 200 and b'Back up and upgrade' in preview.data
    assert local.fingerprint(path) == before and not (tmp_path / 'upgrade-backups').exists()
    response = client.post('/databases/upgrade', data=inputs(preview), follow_redirects=True)
    assert b'Your recovery copy is saved at' in response.data
    assert local.inspect_database(tmp_path, path.name).status == 'empty'
    backup, = (tmp_path / 'upgrade-backups').glob('*.sqlite3')
    assert local.inspect_database(backup.parent, backup.name).revision == 'b5c9d3e7f1a2'
    recovered = tmp_path / 'Recovered.sqlite3'
    recovered.write_bytes(backup.read_bytes())
    candidate = local.inspect_database(tmp_path, recovered.name)
    local.upgrade_database(tmp_path, recovered.name, candidate.identity, candidate.revision)
    assert local.inspect_database(tmp_path, recovered.name).status == 'empty'


def test_failed_upgrade_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / 'Older.sqlite3'
    local.migrate_file(path, 'b5c9d3e7f1a2')
    candidate = local.inspect_database(tmp_path, path.name)
    before = path.read_bytes()
    def fail(path):
        path.write_bytes(b'partial migration')
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(local, 'migrate_file', fail)
    with pytest.raises(local.DatabaseError, match='original database is unchanged'):
        local.upgrade_database(tmp_path, path.name, candidate.identity, candidate.revision)
    assert path.read_bytes() == before
    assert len(list((tmp_path / 'upgrade-backups').glob('*.sqlite3'))) == 1


def test_tabs_and_forged_cross_database_forms_are_isolated(launcher, tmp_path):
    seed(tmp_path, 'Real.sqlite3', 'Real portfolio')
    seed(tmp_path, 'Practice.sqlite3', 'Practice portfolio')
    client = launcher.test_client()
    real = choose(client, 'Real.sqlite3').location.rstrip('/')
    real_form = client.get(real + '/setup')
    practice = choose(client, 'Practice.sqlite3').location.rstrip('/')
    practice_form = client.get(practice + '/setup')
    data = inputs(real_form) | {'name': 'Real updated', 'reporting_currency_code': 'USD',
        'annual_spending_amount': '43000', 'annual_spending_currency_code': 'USD', 'save_and_finish_later': '1'}
    rejected = client.post(practice + '/setup/portfolio', data=data)
    assert rejected.status_code == 400 and b'belongs to another database' in rejected.data
    saved = client.post(real + '/setup/portfolio', data=data)
    assert saved.status_code == 302 and saved.location.startswith(real)
    assert b'Real updated' in client.get(real + '/settings').data
    assert b'Practice portfolio' in client.get(practice + '/settings').data
    assert b'Real updated' not in client.get(practice + '/settings').data
    assert real_form.headers['Cache-Control'] == 'no-store'
    apps = launcher.extensions['datasets'].apps
    a, b = [entry[0] for entry in apps.values()]
    assert a.secret_key != b.secret_key
    assert a.config['RESTORE_STAGING_DIRECTORY'] != b.config['RESTORE_STAGING_DIRECTORY']
    assert a.config['SESSION_COOKIE_PATH'] != b.config['SESSION_COOKIE_PATH']


def test_restart_and_file_replacement_reject_old_posts(tmp_path):
    seed(tmp_path, 'Real.sqlite3')
    config = {'TESTING': True, 'LOCAL_DATABASE_DIRECTORY': str(tmp_path)}
    application = create_app(config)
    client = application.test_client()
    selected = choose(client, 'Real.sqlite3').location
    before = local.fingerprint(tmp_path / 'Real.sqlite3')
    restarted = create_app(config).test_client()
    assert b'Choose your database again' in restarted.get(selected).data
    assert restarted.post(selected + 'setup/portfolio').status_code == 400
    assert local.fingerprint(tmp_path / 'Real.sqlite3') == before
    replacement = tmp_path / 'replacement.tmp'
    replacement.write_bytes((tmp_path / 'Real.sqlite3').read_bytes())
    replacement.replace(tmp_path / 'Real.sqlite3')
    assert b'Choose your database again' in client.get(selected).data


def test_cached_apps_are_bounded_and_eviction_preserves_links(launcher, tmp_path):
    launcher.extensions['datasets'].capacity = 1
    seed(tmp_path, 'First.sqlite3')
    seed(tmp_path, 'Second.sqlite3')
    client = launcher.test_client()
    first = choose(client, 'First.sqlite3').location
    client.get(first, follow_redirects=True).close()
    second = choose(client, 'Second.sqlite3').location
    client.get(second, follow_redirects=True).close()
    assert len(launcher.extensions['datasets'].apps) == 1
    assert b'First.sqlite3' in client.get(first, follow_redirects=True).data


def test_restore_preview_cannot_cross_datasets(launcher, tmp_path):
    from io import BytesIO
    seed(tmp_path, 'Real.sqlite3', 'Real portfolio')
    seed(tmp_path, 'Practice.sqlite3', 'Practice portfolio')
    client = launcher.test_client()
    real = choose(client, 'Real.sqlite3').location.rstrip('/')
    practice = choose(client, 'Practice.sqlite3').location.rstrip('/')
    backup = client.get(real + '/backup/export').data
    real_csrf = inputs(client.get(real + '/settings'))['csrf_token']
    preview = client.post(real + '/backup/preview', data={
        'csrf_token': real_csrf, 'backup_file': (BytesIO(backup), 'backup.json')})
    assert preview.status_code == 200
    preview_data = inputs(preview)
    practice_csrf = inputs(client.get(practice + '/settings'))['csrf_token']
    before = local.fingerprint(tmp_path / 'Practice.sqlite3')
    rejected = client.post(practice + '/backup/restore', data=preview_data | {
        'csrf_token': practice_csrf, 'confirm_restore': 'y'}, follow_redirects=True)
    assert b'Backup restored.' not in rejected.data
    assert local.fingerprint(tmp_path / 'Practice.sqlite3') == before
    restored = client.post(real + '/backup/restore', data=preview_data | {
        'confirm_restore': 'y'}, follow_redirects=True)
    assert b'Backup restored.' in restored.data
    assert client.get(real + '/backup/export').json['tables'] == __import__('json').loads(backup)['tables']


def test_monte_carlo_and_affordability_signatures_are_dataset_specific(launcher, tmp_path):
    from app.monte_carlo import _serializer as monte_serializer
    from app.retirement import _affordability_review
    from itsdangerous import BadSignature
    seed(tmp_path, 'Real.sqlite3')
    seed(tmp_path, 'Practice.sqlite3')
    client = launcher.test_client()
    real = choose(client, 'Real.sqlite3').location
    practice = choose(client, 'Practice.sqlite3').location
    client.get(real).close()
    client.get(practice).close()
    first, second = [entry[0] for entry in launcher.extensions['datasets'].apps.values()]
    with first.app_context():
        token = monte_serializer().dumps({'same-source-digest': 'same-record-ids'})
        affordability = _affordability_review().dumps({'same-source-digest': 'same-record-ids'})
        first.extensions['monte_carlo_summaries'] = 'synthetic cache marker'
    with second.app_context():
        with pytest.raises(BadSignature):
            monte_serializer().loads(token)
        with pytest.raises(BadSignature):
            _affordability_review().loads(affordability)
        assert 'monte_carlo_summaries' not in second.extensions


def test_active_stream_is_not_evicted(launcher, tmp_path):
    from flask import Response
    seed(tmp_path, 'First.sqlite3')
    seed(tmp_path, 'Second.sqlite3')
    client = launcher.test_client()
    first_url = choose(client, 'First.sqlite3').location.rstrip('/')
    client.get(first_url + '/').close()
    dispatcher = launcher.extensions['datasets']
    first_token = next(iter(dispatcher.apps))
    first_app = dispatcher.apps[first_token][0]
    # Synthetic WSGI stream exercises the same lifecycle as streamed Monte Carlo.
    original = first_app.wsgi_app
    def streamed(environ, start_response):
        if environ['PATH_INFO'] == '/stream-check':
            return Response(iter([b'part one', b'part two']))(environ, start_response)
        return original(environ, start_response)
    first_app.wsgi_app = streamed
    dispatcher.capacity = 1
    stream = client.get(first_url + '/stream-check', buffered=False)
    second_url = choose(client, 'Second.sqlite3').location
    client.get(second_url).close()
    assert first_token in dispatcher.apps and dispatcher.apps[first_token][1] == 1
    assert b''.join(stream.response) == b'part onepart two'
    stream.close()
    assert len(dispatcher.apps) == 1


def test_same_name_portfolios_and_unknown_filename_do_not_select_by_guess(launcher, tmp_path):
    seed(tmp_path, 'First.sqlite3')
    seed(tmp_path, 'Second.sqlite3')
    client = launcher.test_client()
    page = client.get('/')
    assert b'First.sqlite3' in page.data and b'Second.sqlite3' in page.data
    assert choose(client, 'First.sqlite3').location != choose(client, 'Second.sqlite3').location
    response = client.post('/databases/open', data={'database': '../outside.sqlite3',
        'csrf_token': inputs(page)['csrf_token']}, follow_redirects=True)
    assert b'selection expired' in response.data


def test_upgrade_refuses_busy_source_and_replacement_failure(tmp_path, monkeypatch):
    path = tmp_path / 'Older.sqlite3'
    local.migrate_file(path, 'b5c9d3e7f1a2')
    candidate = local.inspect_database(tmp_path, path.name)
    before = path.read_bytes()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('BEGIN EXCLUSIVE')
        with pytest.raises(local.DatabaseError, match='in use'):
            local.upgrade_database(tmp_path, path.name, candidate.identity, candidate.revision)
        connection.rollback()
    assert path.read_bytes() == before
    def fail_replace(source, target):
        raise OSError('synthetic replacement failure')
    monkeypatch.setattr(local.os, 'replace', fail_replace)
    with pytest.raises(local.DatabaseError, match='original database is unchanged'):
        local.upgrade_database(tmp_path, path.name, candidate.identity, candidate.revision)
    assert path.read_bytes() == before


def test_creation_publish_race_never_overwrites_existing_file(tmp_path, monkeypatch):
    original_link = local.os.link
    def competing_create(source, target):
        target.write_bytes(b'created by a competing action')
        original_link(source, target)
    monkeypatch.setattr(local.os, 'link', competing_create)
    with pytest.raises(local.DatabaseError, match='filename already exists'):
        local.create_database(tmp_path, 'Practice')
    assert (tmp_path / 'Practice.sqlite3').read_bytes() == b'created by a competing action'


def test_upgrade_baseline_and_populated_backup_preserve_source_records(tmp_path):
    # A recognized empty initial revision is a supported unfinished database.
    path = tmp_path / 'Baseline.sqlite3'
    local.migrate_file(path, local.BASELINE)
    candidate = local.inspect_database(tmp_path, path.name)
    local.upgrade_database(tmp_path, path.name, candidate.identity, candidate.revision)
    assert local.inspect_database(tmp_path, path.name).status == 'empty'
    path = tmp_path / 'Populated.sqlite3'
    local.migrate_file(path, 'b5c9d3e7f1a2')
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("INSERT INTO portfolios (id, name, reporting_currency_code, annual_spending_amount, annual_spending_currency_code, price_stale_days, fx_stale_days, statement_value_stale_days, created_at, updated_at) VALUES (1, 'Older synthetic portfolio', 'EUR', 42000.125, 'EUR', 14, 7, 45, '2026-09-13 00:00:00', '2026-09-13 00:00:00')")
        connection.commit()
        before = connection.execute('SELECT * FROM portfolios').fetchall()
    candidate = local.inspect_database(tmp_path, path.name)
    backup = local.upgrade_database(tmp_path, path.name, candidate.identity, candidate.revision)
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute('SELECT * FROM portfolios').fetchall() == before
    application = create_portfolio_app({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}'})
    with application.app_context():
        portfolio = db.session.scalar(select(Portfolio))
        assert portfolio.annual_spending_amount == Decimal('42000.13')
        assert portfolio.name == 'Older synthetic portfolio'
        db.engine.dispose()


def test_read_only_database_has_recoverable_discovery_message(tmp_path, monkeypatch):
    local.create_database(tmp_path, 'Read only')
    real_access = local.os.access
    monkeypatch.setattr(local.os, 'access', lambda path, mode: False if Path(path).name == 'Read only.sqlite3' and mode == local.os.W_OK else real_access(path, mode))
    candidate = local.inspect_database(tmp_path, 'Read only.sqlite3')
    assert candidate.status == 'unavailable' and 'read-only' in candidate.reason


def test_legacy_additional_portfolio_rows_preserve_existing_working_selection(launcher, tmp_path):
    seed(tmp_path, 'Legacy.sqlite3', 'Established working portfolio')
    path = tmp_path / 'Legacy.sqlite3'
    application = create_portfolio_app({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}'})
    with application.app_context():
        db.session.add(Portfolio(name='Additional preserved record', reporting_currency_code='EUR', annual_spending_amount=Decimal('12345')))
        db.session.commit()
        db.engine.dispose()
    before = local.fingerprint(path)
    candidate = local.inspect_database(tmp_path, path.name)
    assert candidate.name == 'Established working portfolio'
    assert candidate.status in {'ready', 'empty'}
    client = launcher.test_client()
    prefix = choose(client, path.name).location.rstrip('/')
    assert b'Established working portfolio' in client.get(prefix + '/settings').data
    payload = client.get(prefix + '/backup/export').json
    assert [row['name'] for row in payload['tables']['portfolios']] == ['Established working portfolio', 'Additional preserved record']
    assert local.fingerprint(path) == before
