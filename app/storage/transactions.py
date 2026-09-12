"""Composable SQLite transactions: nested services never commit their caller."""

from contextlib import contextmanager
from functools import wraps
from uuid import uuid4


@contextmanager
def transaction(connection):
    nested = connection.in_transaction
    savepoint = "sp_" + uuid4().hex
    connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
        else:
            connection.rollback()
        raise
    else:
        if nested:
            connection.execute(f"RELEASE {savepoint}")
        else:
            connection.commit()


def atomic(function):
    @wraps(function)
    def wrapped(connection, *args, **kwargs):
        with transaction(connection):
            return function(connection, *args, **kwargs)
    return wrapped
