"""De planner: één plek voor alle terugkerende taken van de portal.

- Elke minuut kijkt de planner welke taak aan de beurt is (de berichtenbewaking in fase 4 checkt elke minuut).
- Een taak is ofwel periodiek (elke=timedelta) ofwel dagelijks vanaf een vast tijdstip (om=time).
- Tegen dubbel draaien: per taak een PostgreSQL advisory lock. Draaien er ooit twee processen
  (of tijdens een overstap twee versies), dan voert er maar één de taak uit.
- Wat een taak deed, staat in de tabel taken en op de pagina Gezondheid.
"""
import asyncio
import datetime as dt
import threading
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
            try:
                resultaat, fout = str(regel["functie"](c) or "klaar")[:1000], None
            except Exception as e:  # noqa: BLE001
                resultaat, fout = None, f"{e}\n{traceback.format_exc()[-1500:]}"
            c.execute("UPDATE taken SET laatst = %s, resultaat = %s, fout = %s, bezig_sinds = NULL WHERE naam = %s",
                      (nu, resultaat, fout, naam))
            return resultaat if fout is None else f"fout: {fout.splitlines()[0]}"
        finally:
            c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_slot(naam),))


def opruimen(c):
    """Bij het opstarten: taken die tijdens een herstart bezig waren, weer vrijgeven."""
    c.execute("UPDATE taken SET bezig_sinds = NULL, fout = coalesce(fout, 'onderbroken door een herstart') "
              "WHERE bezig_sinds IS NOT NULL")


async def lus(pool):
    await asyncio.sleep(15)
    while True:
        for naam in list(TAKEN):
            try:
                await asyncio.to_thread(draai, pool, naam)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        await asyncio.sleep(INTERVAL_SECONDEN)


def nu_draaien(pool, naam):
    """Voor de knop 'Nu uitvoeren': op de achtergrond, zodat de pagina niet wacht."""
    threading.Thread(target=draai, args=(pool, naam, True), daemon=True).start()
