"""Taken: kanbanbord, afspraken in de agenda bij taken met een datum, en taken uit een mail.

De agenda (Google) is nagebootst. De database- en schermtests draaien alleen met TEST_DATABASE_URL.
Draaien in een eigen proces: python -m pytest tests/test_taken.py (zie tests/alles.sh).
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

from app.kern import config  # noqa: E402
from app.taken import web  # noqa: E402

TZ = config.TZ
DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
D = dt.date(2026, 11, 9)  # maandag


def t(**kw):
    return {"titel": "Rapport", "status": "actief", "startdatum": None, "starttijd": None, "einddatum": None, "eindtijd": None} | kw


def test_afspraak_bij_een_taak():
    assert web.afspraak(t()) is None
    a = web.afspraak(t(startdatum=D, einddatum=D + dt.timedelta(days=3)))
    assert a["heledag"] and a["titel"] == "📋 Rapport" and a["start"] == dt.datetime(2026, 11, 9, tzinfo=TZ) and a["eind"] == dt.datetime(2026, 11, 13, tzinfo=TZ)
    a = web.afspraak(t(einddatum=D))
    assert a["titel"] == "⏰ Deadline: Rapport" and a["heledag"] and a["eind"] - a["start"] == dt.timedelta(days=1)
    a = web.afspraak(t(startdatum=D, starttijd=dt.time(9), eindtijd=dt.time(12)))
    assert not a["heledag"] and a["start"].hour == 9 and a["eind"].hour == 12
    a = web.afspraak(t(startdatum=D, starttijd=dt.time(14)))
    assert a["eind"] - a["start"] == dt.timedelta(hours=1)
    a = web.afspraak(t(einddatum=D, eindtijd=dt.time(17)))
    assert a["start"].hour == 16 and a["eind"].hour == 17
    assert web.afspraak(t(startdatum=D, einddatum=D, status="afgerond"))["titel"] == "✓ Rapport"
    a = web.afspraak(t(startdatum=D + dt.timedelta(days=2), einddatum=D))  # omgedraaid: toch de goede volgorde
    assert a["start"].date() == D


def test_formulier_en_mail():
    v = web.uit_formulier({"titel": "  Presentatie  ", "prio": "onzin", "status": "wachtend", "wacht_op": "griffie", "startdatum": "2026-11-09",
                           "starttijd": "09:30", "einddatum": "", "eindtijd": "kapot"})
    assert v["titel"] == "Presentatie" and v["prio"] == "middel" and v["status"] == "wachtend" and v["wacht_op"] == "griffie"
    assert v["startdatum"] == D and v["starttijd"] == dt.time(9, 30) and v["einddatum"] is None and v["eindtijd"] is None
    assert web.uit_formulier({"status": "weg"})["status"] == "nieuw"
    m = web.uit_bericht({"id": 5, "onderwerp": "Stukken commissie", "van_naam": "Griffie", "van_adres": "griffie@helmond.nl",
                         "regel": "Vraagt om reactie voor vrijdag", "fragment": "Beste raadsleden", "prioriteit": "hoog"})
    assert m["titel"] == "Stukken commissie" and m["prio"] == "hoog" and m["bericht_id"] == 5
    assert "Griffie (griffie@helmond.nl)" in m["beschrijving"] and "Bram: Vraagt om reactie" in m["beschrijving"]


@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        from app.kern import db  # pylint: disable=import-outside-toplevel
        with db.pool.connection() as c:
            c.execute("TRUNCATE takenbord, agenda_koppelingen, post_koppelingen CASCADE")
        yield k


def test_bord_agenda_en_kolommen(klant, monkeypatch):
    from app.clara import bronnen, sync  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    gemaakt, weg = [], []
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k, forceer=False, nu=None: None)
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="", kleur=None, heledag=False, **kw:
                        gemaakt.append((a, titel, s, e, heledag, o)) or f"ev{len(gemaakt)}")
    monkeypatch.setattr(bronnen, "google_verwijder", lambda tok, a, eid, melden=False: weg.append(eid))
    with db.pool.connection() as c:
        ag = [{"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True, "aan": True, "bezet": True},
              {"id": "sanne", "naam": "Sanne", "primair": False, "schrijven": True, "aan": True, "bezet": False}]
        k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, schrijven, agendas, schrijfagenda) VALUES ('google', 'Privé', true, %s, 'primary')
                         RETURNING id""", (json.dumps(ag),)).fetchone()["id"]
    pagina = klant.get("/taken", headers=KOP).text
    assert 'href="/taken" class="actief">Taken' in pagina and pagina.index(">Agenda<") < pagina.index(">Taken<") < pagina.index(">Post<")
    for naam in ("Nieuw", "Triage", "Gepland", "Actief", "Wachtend op", "Afgerond"):
        assert f"<span>{naam}</span>" in pagina
    assert "Martijn (Privé)" in klant.get("/taken?nieuw=1", headers=KOP).text
    # Taak zonder datum: geen afspraak.
    assert klant.post("/taken/nieuw", headers=KOP, data={"titel": "x"}).status_code == 403
    r = klant.post("/taken/nieuw", headers=FORM, follow_redirects=False, data={"titel": "Raadsvragen opstellen", "prio": "hoog"})
    assert r.status_code == 303 and "Nieuw" in r.headers["location"] and not gemaakt
    # Taak met start- en einddatum: afspraak voor die dagen, in Martijns eigen agenda (niet die van Sanne).
    s = dt.date.today() + dt.timedelta(days=3)
    r = klant.post("/taken/nieuw", headers=FORM, follow_redirects=False, data={
        "titel": "Presentatie congres", "beschrijving": "Slides en verhaal", "prio": "middel", "status": "gepland",
        "startdatum": s.isoformat(), "einddatum": (s + dt.timedelta(days=2)).isoformat(), "doel": f"{k}|primary"})
    assert "in+de+agenda+gezet" in r.headers["location"], r.headers["location"]
    a, titel, start, eind, heledag, oms = gemaakt[-1]
    assert a == "primary" and titel == "📋 Presentatie congres" and heledag and start.date() == s and eind.date() == s + dt.timedelta(days=3)
    assert "[taak:" in oms and "Slides en verhaal" in oms
    with db.pool.connection() as c:
        taak = c.execute("SELECT * FROM takenbord WHERE titel = 'Presentatie congres'").fetchone()
        assert taak["agenda_extern"] == "ev1" and taak["status"] == "gepland"
    tid = taak["id"]
    pagina = klant.get("/taken", headers=KOP).text
    assert f'id="taak-{tid}"' in pagina and "📅" in pagina
    # Opslaan zonder wijziging aan de datums: de afspraak blijft.
    klant.post(f"/taken/{tid}", headers=FORM, data={"titel": "Presentatie congres", "beschrijving": "Slides en verhaal", "prio": "hoog",
                                                    "status": "gepland", "startdatum": s.isoformat(), "einddatum": (s + dt.timedelta(days=2)).isoformat(),
                                                    "doel": f"{k}|primary"})
    assert len(gemaakt) == 1 and not weg
    # Tijd erbij: oude afspraak weg, nieuwe gewone afspraak.
    klant.post(f"/taken/{tid}", headers=FORM, data={"titel": "Presentatie congres", "prio": "hoog", "status": "gepland",
                                                    "startdatum": s.isoformat(), "starttijd": "09:00", "eindtijd": "12:00", "doel": f"{k}|primary"})
    assert weg == ["ev1"] and not gemaakt[-1][4] and gemaakt[-1][2].hour == 9 and gemaakt[-1][3].hour == 12
    # Slepen naar Wachtend op zonder 'op wie': dan vraagt het scherm erom.
    r = klant.post(f"/taken/{tid}/status", headers=FORM, data={"status": "wachtend"}, follow_redirects=False)
    assert f"bewerk={tid}&wacht=1" in r.headers["location"] and "Op+wie+wacht+je" in r.headers["location"]
    assert 'name="wacht_op"' in klant.get(f"/taken?bewerk={tid}&wacht=1", headers=KOP).text
    klant.post(f"/taken/{tid}", headers=FORM, data={"titel": "Presentatie congres", "prio": "hoog", "status": "wachtend", "wacht_op": "de organisatie",
                                                    "startdatum": s.isoformat(), "starttijd": "09:00", "eindtijd": "12:00", "doel": f"{k}|primary"})
    assert "Wacht op: <b>de organisatie</b>" in klant.get("/taken", headers=KOP).text
    # Afgerond: de afspraak krijgt een vinkje.
    r = klant.post(f"/taken/{tid}/status", headers=FORM, data={"status": "afgerond"}, follow_redirects=False)
    assert "Afgerond" in r.headers["location"] and gemaakt[-1][1] == "✓ Presentatie congres"
    assert klant.post(f"/taken/{tid}/status", headers=FORM, data={"status": "kwijt"}).status_code == 400
    # Verwijderen: ook uit de agenda.
    laatste = gemaakt and f"ev{len(gemaakt)}"
    r = klant.post(f"/taken/{tid}/weg", headers=FORM, follow_redirects=False)
    assert "ook+uit+de+agenda" in r.headers["location"] and weg[-1] == laatste
    with db.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM takenbord WHERE id = %s", (tid,)).fetchone()
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (k,))
        c.execute("DELETE FROM takenbord")


def test_taak_uit_een_mail(klant):
    from app.clara import kluis  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        kid = c.execute("INSERT INTO post_koppelingen (soort, naam, account, geheim) VALUES ('google', 'Neighbour', 'm@n.nu', %s) RETURNING id",
                        (kluis.sluit("r"),)).fetchone()["id"]
        ids = [c.execute("""INSERT INTO post_berichten (koppeling_id, extern_id, van_naam, van_adres, onderwerp, fragment, ontvangen, categorie, prioriteit)
                            VALUES (%s, %s, 'Griffie', 'griffie@helmond.nl', %s, 'Beste raadsleden', now(), 'antwoord', 'hoog') RETURNING id""",
                         (kid, f"m{n}", f"Onderwerp {n}")).fetchone()["id"] for n in range(3)]
    assert f'href="/taken?nieuw=1&bericht={ids[0]}#formulier"' in klant.get("/bram", headers=KOP).text
    assert "Taak maken van deze mail" in klant.get(f"/bram/bericht/{ids[0]}", headers=KOP).text
    r = klant.get(f"/taken?nieuw=1&bericht={ids[0]}", headers=KOP)
    assert 'value="Onderwerp 0"' in r.text and "Uit de mail van Griffie" in r.text and f'name="bericht_id" value="{ids[0]}"' in r.text
    klant.post("/taken/nieuw", headers=FORM, data={"titel": "Onderwerp 0", "prio": "hoog", "bericht_id": str(ids[0])})
    pagina = klant.get("/taken", headers=KOP).text
    assert f'href="/bram/bericht/{ids[0]}">Onderwerp 0' in pagina
    # Meer mails tegelijk vanuit Post: elk een taak in Nieuw.
    r = klant.post("/bram/bulk", headers=FORM, follow_redirects=False, data={"bericht": [str(ids[1]), str(ids[2])], "actie": "taak"})
    assert "2+berichten+als+taak+in+Taken+gezet" in r.headers["location"], r.headers["location"]
    with db.pool.connection() as c:
        rijen = c.execute("SELECT titel, status, prio, bericht_id FROM takenbord ORDER BY id").fetchall()
        assert [(x["titel"], x["status"], x["prio"]) for x in rijen] == [("Onderwerp 0", "nieuw", "hoog"), ("Onderwerp 1", "nieuw", "hoog"),
                                                                        ("Onderwerp 2", "nieuw", "hoog")]
        c.execute("DELETE FROM takenbord")
        c.execute("DELETE FROM post_koppelingen")
