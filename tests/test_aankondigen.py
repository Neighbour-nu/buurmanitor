"""Aankondigingen: een melding kort voordat een vergadering, afspraak of taak begint, en 's ochtends voor deadlines.

De database- en schermtests draaien alleen met TEST_DATABASE_URL.
Draaien in een eigen proces: python -m pytest tests/test_aankondigen.py (zie tests/alles.sh).
"""
import datetime as dt
import json
import os

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app import aankondigen  # noqa: E402
from app.kern import config  # noqa: E402

TZ = config.TZ
DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
NU = dt.datetime(2026, 11, 2, 9, 50, tzinfo=TZ)


def a(titel, minuten, **kw):
    s = NU + dt.timedelta(minutes=minuten)
    return {"id": 1, "titel": titel, "start": s, "eind": s + dt.timedelta(hours=1), "heledag": False, "vrij": False,
            "niet_voor_mij": False, "antwoord": "geaccepteerd", "locatie": "", "online": False, "koppeling": "Privé", "agenda_naam": ""} | kw


def test_welke_afspraken():
    lijst = [a("Commissie Ruimte", 10, locatie="Huis voor de Stad"), a("Commissie Ruimte", 10, koppeling="Raad"),   # dubbel: één keer
             a("Later vandaag", 40), a("Al begonnen", -5), a("Hele dag", 5, heledag=True), a("Afgewezen", 5, antwoord="afgewezen"),
             a("Paardrijles", 5, niet_voor_mij=True), a("Focusblok vrij", 5, vrij=True),
             a("🚆 Reistijd naar Utrecht (trein)", 3, vrij=True)]
    uit = aankondigen.te_melden_afspraken(lijst, NU, 15)
    assert [x[1] for x in uit] == ["Over 10 min: Commissie Ruimte", "Over 3 min vertrekken: naar Utrecht (trein)"]
    assert uit[0][2].startswith("10.00-11.00 · Huis voor de Stad") and uit[0][3] == "/clara#a1"
    assert aankondigen.te_melden_afspraken(lijst, NU, 15)[0][0] == uit[0][0]  # dezelfde sleutel: één keer melden


def test_welke_taken():
    d = NU.date()
    taken = [{"id": 1, "titel": "Rapport schrijven", "status": "gepland", "startdatum": d, "starttijd": dt.time(10, 0), "eindtijd": dt.time(12, 0),
              "einddatum": d, "agenda_extern": "", "prio": "middel"},
             {"id": 2, "titel": "Al in de agenda", "status": "gepland", "startdatum": d, "starttijd": dt.time(10, 0), "eindtijd": None,
              "einddatum": d, "agenda_extern": "ev1", "prio": "middel"},
             {"id": 3, "titel": "Motie indienen", "status": "actief", "startdatum": None, "starttijd": None, "eindtijd": None,
              "einddatum": d, "agenda_extern": "", "prio": "hoog"},
             {"id": 4, "titel": "Klaar", "status": "afgerond", "startdatum": d, "starttijd": dt.time(10, 0), "eindtijd": None,
              "einddatum": d, "agenda_extern": "", "prio": "laag"}]
    uit = aankondigen.te_melden_taken(taken, NU, 15, dt.time(8, 0))
    assert [x[1] for x in uit] == ["Over 10 min: Rapport schrijven", "Deadline vandaag: Motie indienen"]
    assert uit[1][2] == "Deze taak moet vandaag af. Prioriteit hoog." and uit[0][3] == "/taken?bewerk=1#formulier"
    assert [x[1] for x in aankondigen.te_melden_taken(taken, NU.replace(hour=7), 15, dt.time(8, 0))] == []  # voor 8.00 nog geen deadline
    assert [x[1] for x in aankondigen.te_melden_taken(taken, NU, 15, dt.time(8, 0), met_deadlines=False)] == ["Over 10 min: Rapport schrijven"]


@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        yield k


def test_planner_en_instellingen(klant):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    nu = dt.datetime.now(TZ).replace(second=0, microsecond=0)
    with db.pool.connection() as c:
        c.execute("DELETE FROM meldingen WHERE bron = 'aankondiging'")
        c.execute("DELETE FROM instellingen WHERE sleutel = 'aankondigen'")
        c.execute("TRUNCATE agenda_koppelingen CASCADE")
        c.execute("DELETE FROM takenbord")
        kid = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, schrijven, agendas, schrijfagenda) VALUES ('google', 'Privé', true, %s, 'primary')
                           RETURNING id""", (json.dumps([{"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True, "aan": True, "bezet": True}]),)).fetchone()["id"]
        c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, locatie, start, eind) VALUES
                     (%s, 'primary', 'x1', 'Fractieoverleg', 'Stadhuis', %s, %s), (%s, 'primary', 'x2', 'Later', '', %s, %s)""",
                  (kid, nu + dt.timedelta(minutes=12), nu + dt.timedelta(minutes=72), kid, nu + dt.timedelta(minutes=50), nu + dt.timedelta(minutes=80)))
        assert aankondigen.taak_aankondigen(c, nu) == "1 aankondiging"
        assert aankondigen.taak_aankondigen(c, nu + dt.timedelta(minutes=1)) == "niets aan te kondigen"  # niet nog een keer
        m = c.execute("SELECT * FROM meldingen WHERE bron = 'aankondiging'").fetchone()
        assert m["niveau"] == "belangrijk" and m["titel"] == "Over 12 min: Fractieoverleg" and "Stadhuis" in m["tekst"]
        # Opruimen: na een paar uur naar het archief, en hij komt niet terug.
        assert aankondigen.taak_aankondigen(c, nu + dt.timedelta(hours=4))
        assert c.execute("SELECT status FROM meldingen WHERE id = %s", (m["id"],)).fetchone()["status"] == "archief"
    # Instellen: 60 minuten van tevoren, zonder vergaderingen.
    pagina = klant.get("/meldingen/instellingen", headers=KOP).text
    assert "Hoe ver van tevoren" in pagina and "Over 12 min: Fractieoverleg" in pagina
    assert 'id="meldingstatus"' in pagina and "meldingen-test" in pagina  # bureaubladmeldingen staan hier, niet meer op Vandaag
    vandaag = klant.get("/", headers=KOP).text
    assert 'href="/meldingen/instellingen"' in vandaag and 'id="meldingstatus"' not in vandaag
    assert klant.get("/meldingen/aankondigingen", headers=KOP, follow_redirects=False).headers["location"] == "/meldingen/instellingen#aankondigingen"
    assert klant.post("/meldingen/aankondigingen", headers=KOP, data={"minuten": "60"}).status_code == 403
    klant.post("/meldingen/aankondigingen", headers=FORM, data={"minuten": "60", "taken": "1", "deadlines": "1", "ochtend": "07:30"})
    with db.pool.connection() as c:
        inst = aankondigen.instellingen(c)
        assert inst == {"vergaderingen": False, "taken": True, "deadlines": True, "minuten": 60, "ochtend": "07:30"}
        assert aankondigen.taak_aankondigen(c, nu) == "niets aan te kondigen"  # vergaderingen staan uit
        c.execute("DELETE FROM meldingen WHERE bron = 'aankondiging'")
        c.execute("DELETE FROM instellingen WHERE sleutel = 'aankondigen'")
        c.execute("TRUNCATE agenda_koppelingen CASCADE")
