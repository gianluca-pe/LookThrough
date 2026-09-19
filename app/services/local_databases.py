"""Local file discovery and atomic creation/upgrade. No portfolio calculations."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from threading import RLock
from uuid import uuid4

from alembic.script import ScriptDirectory
from flask_migrate import upgrade
from sqlalchemy.engine import URL

from app.extensions import db

MIGRATIONS = Path(__file__).resolve().parents[2] / 'migrations'
SCRIPTS = ScriptDirectory(str(MIGRATIONS))
HEAD = SCRIPTS.get_current_head()
REVISIONS = {r.revision for r in SCRIPTS.walk_revisions()}
BASELINE = '040c3a312c85'
SUFFIXES = {'.sqlite3', '.sqlite', '.db'}
# Alembic changes process-level context while running, so serialize local upgrades.
MIGRATION_LOCK = RLock()


class DatabaseError(ValueError):
    """A recoverable local database action failure."""


@dataclass(frozen=True)
class Candidate:
    filename: str
    name: str | None
    status: str
    reason: str = ''
    revision: str | None = None
    identity: str | None = None


def identity(path: Path) -> str:
    stat = path.stat(follow_symlinks=False)
    return f'{stat.st_dev}:{stat.st_ino}'


def checked_path(folder: Path, filename: str) -> Path:
    if (not filename or Path(filename).name != filename or '\\' in filename
            or Path(filename).suffix.lower() not in SUFFIXES):
        raise DatabaseError('Choose a database file from the local folder.')
    path = folder / filename
    if path.is_symlink() or not path.is_file():
        raise DatabaseError('This database was moved or removed. Refresh the list and choose again.')
    return path


def read_connection(path: Path):
    # Immutable reads cannot create SQLite journals or shared-memory files.
    # Never use them against pending journals/WAL: those files need their owning
    # application to finish/checkpoint before they can be discovered reliably.
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise DatabaseError('This file has pending SQLite changes. Close the application using it, then refresh.')
    return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=1)


def inspect_database(folder: Path, filename: str) -> Candidate:
    try:
        path = checked_path(folder, filename)
        file_identity = identity(path)
        with closing(read_connection(path)) as connection:
            if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise DatabaseError('SQLite could not verify this file. Recover from a known good backup.')
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'alembic_version' not in tables:
                raise DatabaseError('This is not a recognized LookThrough database.')
            versions = connection.execute('SELECT version_num FROM alembic_version').fetchall()
            if len(versions) != 1:
                raise DatabaseError('The database version cannot be identified.')
            revision = versions[0][0]
            if revision not in REVISIONS:
                raise DatabaseError('This version is newer or unknown. Open it with the matching LookThrough release; this app cannot downgrade it.')
            if not os.access(path, os.W_OK) or not os.access(folder, os.W_OK):
                raise DatabaseError('This database or its folder is read-only. Allow local writes before opening it.')
            name = None
            if revision != BASELINE:
                if not {'portfolios', 'accounts', 'institutions'} <= tables:
                    raise DatabaseError('The LookThrough schema is incomplete. Recover from a known good backup.')
                # Match the established working-portfolio selection in
                # setup._current_portfolio. Older files may retain other rows;
                # opening a dataset must neither discard them nor reinterpret
                # which portfolio the existing application displays.
                rows = connection.execute('SELECT id, name FROM portfolios ORDER BY id LIMIT 1').fetchall()
                name = rows[0][1] if rows else None
            if revision == HEAD:
                for table in db.metadata.sorted_tables:
                    columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{table.name}")')}
                    if not set(table.columns.keys()) <= columns:
                        raise DatabaseError('The LookThrough schema is incomplete. Recover from a known good backup.')
                if connection.execute('PRAGMA foreign_key_check').fetchone():
                    raise DatabaseError('The database has broken record relationships. Recover from a known good backup.')
            finished_setup = bool(name)
            if revision == HEAD and name:
                working_id = rows[0][0]
                has_account = connection.execute('SELECT 1 FROM accounts WHERE portfolio_id=? LIMIT 1', (working_id,)).fetchone()
                has_position = connection.execute('SELECT 1 FROM position_registrations p JOIN instruments i ON i.id=p.instrument_id WHERE i.portfolio_id=? LIMIT 1', (working_id,)).fetchone()
                finished_setup = bool(has_account and has_position)
            status = 'upgrade' if revision != HEAD else ('ready' if finished_setup else 'empty')
            return Candidate(filename, name, status, revision=revision, identity=file_identity)
    except (OSError, sqlite3.Error, DatabaseError) as error:
        reason = str(error) if isinstance(error, DatabaseError) else 'This file could not be read as a LookThrough database. Check permissions or recover from a backup.'
        return Candidate(filename, None, 'unavailable', reason)


def discover(folder: Path) -> list[Candidate]:
    try:
        names = sorted((p.name for p in folder.iterdir()
                        if not p.is_symlink() and p.is_file() and p.suffix.lower() in SUFFIXES), key=str.casefold)
    except OSError as error:
        raise DatabaseError('The local data folder could not be read. Check its permissions and refresh.') from error
    return [inspect_database(folder, name) for name in names]


def new_filename(stem: str) -> str:
    stem = stem.strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,63}', stem):
        raise DatabaseError('Use 1–64 letters, numbers, spaces, hyphens or underscores, starting with a letter or number. Leave out the extension.')
    return stem + '.sqlite3'


def migrate_file(path: Path, revision: str = 'head') -> None:
    from app import create_portfolio_app
    application = create_portfolio_app({'SQLALCHEMY_DATABASE_URI': URL.create('sqlite', database=str(path))})
    with MIGRATION_LOCK, application.app_context():
        try:
            upgrade(directory=str(MIGRATIONS), revision=revision)
        except SystemExit as error:
            # Flask-Migrate's CLI wrapper exits on RuntimeError/CommandError.
            # Keep the chooser alive and leave staged migration recovery intact.
            raise DatabaseError('The database migration did not finish. No upgraded database was opened.') from error
        finally:
            db.session.remove()
            db.engine.dispose()


def create_database(folder: Path, stem: str) -> str:
    filename = new_filename(stem)
    target = folder / filename
    # Case-insensitive collision handling also behaves consistently on macOS.
    if any(p.name.casefold() == filename.casefold() for p in folder.iterdir()):
        raise DatabaseError('That filename already exists. Choose another name.')
    try:
        with tempfile.TemporaryDirectory(prefix='.create-', dir=folder) as temporary:
            staged = Path(temporary) / filename
            migrate_file(staged)
            os.chmod(staged, 0o600)
            candidate = inspect_database(staged.parent, staged.name)
            if candidate.status != 'empty':
                raise DatabaseError('The new database could not be verified. Try again.')
            # Publish a complete database without ever replacing an existing file.
            os.link(staged, target)
        return filename
    except FileExistsError as error:
        raise DatabaseError('That filename already exists. Choose another name.') from error
    except Exception as error:
        if isinstance(error, DatabaseError):
            raise
        raise DatabaseError('The database could not be created. Check folder permissions and available disk space, then retry.') from error


def fingerprint(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def upgrade_database(folder: Path, filename: str, expected_identity: str, expected_revision: str) -> Path:
    candidate = inspect_database(folder, filename)
    if (candidate.status != 'upgrade' or candidate.identity != expected_identity
            or candidate.revision != expected_revision):
        raise DatabaseError('This database changed since the upgrade preview. Refresh and review it again.')
    target = checked_path(folder, filename)
    try:
        source_lock = sqlite3.connect(target.as_uri() + '?mode=rw', uri=True, timeout=0)
        try:
            source_lock.execute('BEGIN EXCLUSIVE')
        except sqlite3.Error:
            source_lock.close()
            raise DatabaseError('This database is in use. Close other applications using it, then retry the upgrade.')
    except sqlite3.Error as error:
        raise DatabaseError('The selected database cannot be opened for upgrade. Check file permissions and retry.') from error
    try:
        original_digest = fingerprint(target)
        backup_folder = folder / 'upgrade-backups'
        if backup_folder.is_symlink():
            raise DatabaseError('The upgrade-backups folder must be a local folder, not a symbolic link.')
        backup_folder.mkdir(mode=0o700, exist_ok=True)
        backup = backup_folder / f'{target.stem}-{uuid4().hex}.sqlite3'
        with tempfile.TemporaryDirectory(prefix='.upgrade-', dir=folder) as temporary:
            staged = Path(temporary) / filename
            with closing(read_connection(target)) as source, closing(sqlite3.connect(staged)) as destination:
                source.backup(destination)
            # Preserve a complete, consistent SQLite recovery file, before migration.
            with closing(sqlite3.connect(staged)) as source, closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
            os.chmod(backup, 0o600)
            migrate_file(staged)
            checked = inspect_database(staged.parent, staged.name)
            if checked.status not in {'ready', 'empty'}:
                raise DatabaseError('The upgraded copy could not be verified.')
            with closing(read_connection(target)):
                if identity(target) != expected_identity or fingerprint(target) != original_digest:
                    raise DatabaseError('The source changed during upgrade. Close other applications using it and review again.')
            os.chmod(staged, target.stat().st_mode & 0o777)
            os.replace(staged, target)
        return backup
    except Exception as error:
        if isinstance(error, DatabaseError):
            raise
        raise DatabaseError('The upgrade did not finish. Your original database is unchanged. Check permissions and disk space, then retry.') from error

    finally:
        source_lock.rollback()
        source_lock.close()
