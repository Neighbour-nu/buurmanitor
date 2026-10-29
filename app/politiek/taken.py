"""Terugkerende taken van Promonitor, in de app zelf (één proces, dus geen dubbel werk).

Elke vijf minuten kijkt de planner welke taak aan de beurt is. Wat een taak deed, staat in de
tabel taken (laatste keer, resultaat, fout) en op het scherm Instellingen.
"""
import asyncio
import datetime as dt
import traceback
import zoneinfo

TZ = zoneinfo.ZoneInfo("Europe/Amsterdam")


def _raadsinformatie(c):
    from . import raadsinfo  # pylint: disable=import-outside-toplevel
    nieuw, bijgewerkt, fouten = raadsinfo.synchroniseer(c)
    tekst = f"{nieuw} nieuw, {bijgewerkt} bijgewerkt"
    return tekst + (f"; problemen: {'; '.join(fouten)[:400]}" if fouten else "")


def _nieuws(c):
    from . import nieuws  # pylint: disable=import-outside-toplevel
    nieuw, fouten = nieuws.ophalen(c)
    gewogen, melding = nieuws.wegen(c)
    tekst = f"{nieuw} nieuw, {gewogen} gewogen" + (f" ({melding})" if melding else "")
    return tekst + (f"; bronnen met problemen: {'; '.join(fouten)[:400]}" if fouten else "")


def _briefing(c):
    from . import briefing  # pylint: disable=import-outside-toplevel
    return briefing.versturen(c)


def _briefing_tijd(c):
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = 'briefing_tijd'").fetchone()
    try:
        u, m = (int(x) for x in (r["waarde"] if r and r["waarde"] else "08.00").replace(":", ".").split("."))
        return dt.time(u, m)
    except ValueError:
        return dt.time(8, 0)


def _weekvooruitblik(c):
    from . import dashboard  # pylint: disable=import-outside-toplevel
    return dashboard.stuur_week(c)


def _maandoverzicht(c):
    from . import dashboard  # pylint: disable=import-outside-toplevel
    return dashboard.stuur_maand(c)


def _zondagavond(_c, nu=None):
    """Zondag vanaf 20.00 uur (tot middernacht); andere dagen niet."""
    nu = nu or dt.datetime.now(TZ)
    return (dt.time(20, 0), 24) if nu.weekday() == 6 else False


def _eerste_werkdag(_c, nu=None):
    """De eerste werkdag van de maand, vanaf 8.30 uur (na de briefing)."""
    from . import dashboard  # pylint: disable=import-outside-toplevel
    nu = nu or dt.datetime.now(TZ)
    return (dt.time(8, 30), 18) if nu.date() == dashboard.eerste_werkdag(nu.date()) else False


# naam: (functie, minuten tussen twee keer of None, (eerste uur, laatste uur) of functie die de vaste tijd geeft:
# een tijd, of (tijd, tot welk uur het nog mag), of False als de taak vandaag niet hoeft)
TAKEN = {
    "raadsinformatie": (_raadsinformatie, 60, (6, 23)),
    "nieuws": (_nieuws, 15, (6, 23)),
    "briefing": (_briefing, None, _briefing_tijd),
    "weekvooruitblik": (_weekvooruitblik, None, _zondagavond),
    "maandoverzicht": (_maandoverzicht, None, _eerste_werkdag),
}


def is_aan_de_beurt(laatst, interval, uren, nu, vaste_tijd=None, tot_uur=12):
    if vaste_tijd is not None:
        # Eén keer per dag, vanaf de vaste tijd; standaard niet meer na het middaguur (dan is het nieuws oud).
        start = nu.replace(hour=vaste_tijd.hour, minute=vaste_tijd.minute, second=0, microsecond=0)
        eind = start.replace(hour=0, minute=0) + dt.timedelta(hours=tot_uur)
        return start <= nu < eind and (laatst is None or laatst < start)
    if not (uren[0] <= nu.hour < uren[1]):
        return False
    return laatst is None or (nu - laatst).total_seconds() >= interval * 60 - 30


def voer_uit(pool, naam):
    functie = TAKEN[naam][0]
    with pool.connection() as c:
        try:
            resultaat, fout = functie(c), None
        except Exception:  # noqa: BLE001 - de fout komt in de tabel
            resultaat, fout = None, traceback.format_exc()[-1500:]
        c.execute("""INSERT INTO taken (naam, laatst, resultaat, fout) VALUES (%s, now(), %s, %s)
                     ON CONFLICT (naam) DO UPDATE SET laatst = now(), resultaat = EXCLUDED.resultaat,
                       fout = EXCLUDED.fout""", (naam, resultaat, fout))
    return resultaat, fout


async def planner(pool):
    await asyncio.sleep(20)  # eerst rustig opstarten
    while True:
        nu = dt.datetime.now(TZ)
        for naam, (_f, interval, uren) in TAKEN.items():
            try:
                with pool.connection() as c:
                    r = c.execute("SELECT laatst FROM taken WHERE naam = %s", (naam,)).fetchone()
                    vast = uren(c) if callable(uren) else None
                if vast is False:
                    continue  # vandaag niet aan de beurt
                tot_uur = 12
                if isinstance(vast, tuple):
                    vast, tot_uur = vast
                if is_aan_de_beurt(r["laatst"] if r else None, interval, uren, nu, vast, tot_uur):
                    await asyncio.to_thread(voer_uit, pool, naam)
            except Exception:  # noqa: BLE001 - de planner mag nooit stoppen
                traceback.print_exc()
        await asyncio.sleep(300)
