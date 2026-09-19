"""Build historical fixtures with historical Numeric bindings, never new integer bindings."""
from contextlib import contextmanager
from decimal import Decimal
from sqlalchemy import Numeric
from app.extensions import db
from app.decimal_policy import DECIMAL_FIELDS


@contextmanager
def legacy_decimal_types():
    saved=[]
    def clear():
        db.engine.clear_compiled_cache()
        for mapper in db.Model.registry.mappers:
            mapper._compiled_cache.clear()
    clear()
    try:
        for table, fields in DECIMAL_FIELDS.items():
            for name, (_, precision, scale, _) in fields.items():
                column=db.metadata.tables[table].c[name]
                saved.append((column,column.type))
                column.type=Numeric(precision,scale)
        yield
    finally:
        db.session.remove()
        for column, datatype in saved:
            column.type=datatype
        clear()


def unscale_rows(table, rows, *, legacy=False):
    """Compare SQL fixture snapshots semantically after an integer-storage migration."""
    result=[]
    for row in rows:
        row=dict(row)
        for field, (places,*_) in DECIMAL_FIELDS.get(table,{}).items():
            if field in row and row[field] is not None:
                row[field]=Decimal(str(row[field])) if legacy else Decimal(row[field]).scaleb(-places)
        result.append(row)
    return result
