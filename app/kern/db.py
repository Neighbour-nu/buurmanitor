"""Databaseverbinding: één pool, eenvoudige hulpfuncties, migraties bij het opstarten."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=4, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": True})


def q(sql, params=None):
    with pool.connection() as c:
        return c.execute(sql, params).fetchall()


def q1(sql, params=None):
    with pool.connection() as c:
        return c.execute(sql, params).fetchone()


def uitvoeren(sql, params=None):
    with pool.connection() as c:
        c.execute(sql, params)


def migreer(c, migraties):
    """Alle migraties zijn veilig om opnieuw te draaien (IF NOT EXISTS)."""
    for sql in migraties:
        c.execute(sql)
