"""SQLite write reservation for the existing atomic ledger commands."""

from contextlib import contextmanager
import sqlite3

from sqlalchemy.exc import OperationalError

from app.extensions import db


BUSY_MESSAGE = (
    "Another save is using this database. Nothing from this action was saved. "
    "Wait for it to finish, then review and submit again."
)


@contextmanager
def ledger_write(validation_error, field):
    """Reserve the writer before validation; the command still owns its commit.

    Route reads may have populated the identity map before this boundary. Refresh
    those objects after taking the reservation so validation cannot reuse stale
    status, access or settlement facts. Existing staged work is flushed under the
    same reservation, never discarded to start a new transaction.
    """
    try:
        connection = db.session.connection()
        if connection.connection.driver_connection.in_transaction:
            # A caller may already have a real transaction. A zero-row write
            # obtains its SQLite write reservation without changing any records;
            # a read snapshot that cannot be promoted fails safely as busy.
            connection.exec_driver_sql("UPDATE transactions SET id = id WHERE 0")
        else:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        db.session.flush()
        db.session.expire_all()
        yield
    except OperationalError as error:
        db.session.rollback()
        code = getattr(error.orig, "sqlite_errorcode", None)
        if code is not None and (code & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise validation_error(field, BUSY_MESSAGE) from error
        raise
    except Exception:
        db.session.rollback()
        raise
