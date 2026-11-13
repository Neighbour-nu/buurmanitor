"""De planner: één plek voor alle terugkerende taken van de portal.

- Elke minuut kijkt de planner welke taak aan de beurt is (de berichtenbewaking in fase 4 checkt elke minuut).
- Een taak is ofwel periodiek (elke=timedelta) ofwel dagelijks vanaf een vast tijdstip (om=time).
- Tegen dubbel draaien: per taak een PostgreSQL advisory lock. Draaien er ooit twee processen
  (of tijdens een overstap twee versies), dan voert er maar één de taak uit.
- Wat een taak deed, staat in de tabel taken en op de pagina Gezondheid.
- Elke taak draait in een eigen thread, zodat een trage taak (nieuws met Claude, mail ophalen) de andere niet
  ophoudt; een taak die nog bezig is, start niet nog een keer. De planner gebruikt zijn eigen databasepool
  (db.achtergrond), zodat de schermen nooit op achtergrondwerk hoeven te wachten.
"""
import asyncio
import datetime as dt
import threading
import time
import traceback

from . import config

INTERVAL_SECONDEN = 60
TAKEN = {}


def taak(naam, elke=None, om=None):
    """Registreer een functie(c) -> str als taak. Precies één van elke (timedelta) of om (time)."""
    assert (elke is None) != (om is None), "geef elke of om"

    def registreer(functie):
        TAKEN[naam] = {"elke": elke, "om": om, "functie": functie}
        return functie
    return registreer


def aan_de_beurt(regel, nu, laatst):
    """Moet een taak nu draaien? Pure functie, zodat hij makkelijk te testen is."""
    if regel["elke"] is not None:
        return laatst is None or nu - laatst >= regel["elke"] - dt.timedelta(seconds=5)
    start = dt.datetime.combine(nu.date(), regel["om"], tzinfo=nu.tzinfo)
    return nu >= start and (laatst is None or laatst < start)


def _slot(naam):
    return f"buurmanitor:taak:{naam}"


def draai(pool, naam, forceer=False):
    """Voer één taak uit als hij aan de beurt is (of forceer). Geeft het resultaat terug, of None."""
    regel = TAKEN[naam]
    with pool.connection() as c:
        if not c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (_slot(naam),)).fetchone()["ok"]:
            return None  # een ander proces is er al mee bezig
        try:
            r = c.execute("SELECT laatst FROM taken WHERE naam = %s", (naam,)).fetchone()
            nu = dt.datetime.now(config.TZ)
            if not forceer and not aan_de_beurt(regel, nu, r["laatst"] if r else None):
                return None
            c.execute("""INSERT INTO taken (naam, bezig_sinds) VALUES (%s, now())
                         ON CONFLICT (naam) DO UPDATE SET bezig_sinds = now()""", (naam,))
            begin = time.monotonic()
            try:
                resultaat, fout = str(regel["functie"](c) or "klaar")[:1000], None
            except Exception as e:  # noqa: BLE001
                resultaat, fout = None, f"{e}\n{traceback.format_exc()[-1500:]}"
            c.execute("UPDATE taken SET laatst = %s, resultaat = %s, fout = %s, bezig_sinds = NULL, duur_ms = %s WHERE naam = %s",
                      (nu, resultaat, fout, int((time.monotonic() - begin) * 1000), naam))
            return resultaat if fout is None else f"fout: {fout.splitlines()[0]}"
        finally:
            c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_slot(naam),))


def opruimen(c):
    """Bij het opstarten: taken die tijdens een herstart bezig waren, weer vrijgeven."""
    c.execute("UPDATE taken SET bezig_sinds = NULL, fout = coalesce(fout, 'onderbroken door een herstart') "
              "WHERE bezig_sinds IS NOT NULL")


_bezig = set()
_bezig_slot = threading.Lock()


def _in_thread(pool, naam, forceer=False):
    """Start de taak in een eigen thread, tenzij hij in dit proces al bezig is. Geeft True als hij start."""
    with _bezig_slot:
        if naam in _bezig:
            return False
        _bezig.add(naam)

    def werk():
        try:
            draai(pool, naam, forceer)
        except Exception:  # noqa: BLE001  (de planner mag nooit stoppen)
            traceback.print_exc()
        finally:
            with _bezig_slot:
                _bezig.discard(naam)
    threading.Thread(target=werk, name=f"taak-{naam}", daemon=True).start()
    return True


def aan_de_beurt_lijst(pool, nu=None):
    """Welke taken nu aan de beurt zijn, met één blik in de tabel taken."""
    nu = nu or dt.datetime.now(config.TZ)
    with pool.connection() as c:
        laatst = {r["naam"]: r["laatst"] for r in c.execute("SELECT naam, laatst FROM taken").fetchall()}
    return [n for n, regel in TAKEN.items() if aan_de_beurt(regel, nu, laatst.get(n))]


def bezig():
    with _bezig_slot:
        return set(_bezig)


async def lus(pool):
    await asyncio.sleep(15)
    while True:
        try:
            for naam in await asyncio.to_thread(aan_de_beurt_lijst, pool):
                _in_thread(pool, naam)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        await asyncio.sleep(INTERVAL_SECONDEN)


def nu_draaien(pool, naam):
    """Voor de knop 'Nu uitvoeren': op de achtergrond, zodat de pagina niet wacht."""
    _in_thread(pool, naam, True)
