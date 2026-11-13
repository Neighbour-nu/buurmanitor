"""Databaseverbinding: één pool, eenvoudige hulpfuncties, migraties bij het opstarten."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

# Twee pools, zodat achtergrondwerk de schermen nooit laat wachten:
# - pool: voor de schermen (een verbinding is daar meestal maar een paar milliseconden bezet; een gesprek met Claude
#   houdt er wel een vast zolang het antwoord duurt);
# - achtergrond: voor de planner (ophalen van mail, agenda, nieuws en socials), waar een taak een verbinding vasthoudt
#   zolang hij met Gmail, Outlook of Claude bezig is.
pool = ConnectionPool(config.DATABASE_URL, min_size=2, max_size=12, timeout=20, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": True}, name="schermen")
achtergrond = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8, timeout=60, open=False,
                             kwargs={"row_factory": dict_row, "autocommit": True}, name="achtergrond")


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
