"""Taken als Google Taken in plaats van afspraken: de keuze per taak, wat er naar Google gaat, wat er terugkomt
(afvinken, titel, datum, verwijderen) en het wisselen vanaf afspraken. Google wordt nagebootst. De database-tests
draaien alleen met TEST_DATABASE_URL. Draaien in een eigen proces (zie tests/alles.sh).
"""
import datetime as dt
import itertools
import threading
import os
import urllib.parse

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.voorbeeld.nl"
os.environ["TOEGESTANE_HOSTS"] = "buurmanitor.voorbeeld.nl"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.clara import bronnen  # noqa: E402
from app.taken import google, web as taken_web  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.voorbeeld.nl"}
INST = dict(google.STANDAARD)
D = dt.date(2026, 12, 1)


def taak(**kw):
    return {"id": 7, "titel": "Rapport", "beschrijving": "", "status": "gepland", "prio": "middel", "startdatum": None, "starttijd": None,
            "einddatum": None, "eindtijd": None} | kw


# ---------------------------------------------------------------- zonder database

def test_vorm():
    a = taken_web.afspraak
    assert google.vorm(taak(einddatum=D), INST, a) == "google"
    assert google.vorm(taak(startdatum=D), INST, a) == "google"
    assert google.vorm(taak(), INST, a) is None
    assert google.vorm(taak(), INST | {"taken_google_alle": "1"}, a) == "google"
    met_tijd = taak(startdatum=D, starttijd=dt.time(9), eindtijd=dt.time(12))
    assert google.vorm(met_tijd, INST, a) == "afspraak"  # tijd vrijhouden
    assert google.vorm(met_tijd, INST | {"taken_tijd_afspraak": "0"}, a) == "google"
    assert google.vorm(taak(einddatum=D), INST | {"taken_vorm": "afspraak"}, a) == "afspraak"
    assert google.vorm(taak(), INST | {"taken_vorm": "afspraak"}, a) is None
    assert google.vorm(taak(einddatum=D), INST | {"taken_vorm": "uit"}, a) is None


def test_inhoud_en_wijzigingen():
    i = google.inhoud(taak(einddatum=D, startdatum=dt.date(2026, 11, 28), beschrijving="Hoofdstuk 3", starttijd=dt.time(9)))
    assert i["title"] == "Rapport" and i["due"] == "2026-12-01T00:00:00.000Z" and i["status"] == "needsAction"
    assert i["notes"].startswith("Tijd: 09.00\n\nHoofdstuk 3") and "[taak:7] Uit Buurmanitor: https://buurmanitor.voorbeeld.nl/taken?bewerk=7" in i["notes"]
    assert google.inhoud(taak(status="afgerond"))["status"] == "completed" and "due" not in google.inhoud(taak())

    t = taak(einddatum=D)
    assert google.wijzigingen(t, {"title": "Rapport", "status": "needsAction", "due": "2026-12-01T00:00:00.000Z"}) == {}
    assert google.wijzigingen(t, {"title": "Rapport", "status": "completed"}) == {"status": "afgerond"}
    assert google.wijzigingen(taak(status="afgerond", einddatum=D), {"title": "Rapport", "status": "needsAction"}) == {"status": "gepland"}
    assert google.wijzigingen(t, {"title": "Rapport af", "status": "needsAction", "due": "2026-12-03T00:00:00.000Z"}) == {
        "titel": "Rapport af", "einddatum": dt.date(2026, 12, 3)}
    assert google.wijzigingen(taak(startdatum=D, einddatum=dt.date(2026, 12, 5)), {"title": "Rapport", "due": "2026-11-30T00:00:00.000Z"}) == {
        "einddatum": dt.date(2026, 11, 30), "startdatum": None}
    assert google.wijzigingen(taak(startdatum=D), {"title": "Rapport", "due": "2026-12-02T00:00:00.000Z"}) == {"startdatum": dt.date(2026, 12, 2)}
    assert google.wijzigingen(t, {"title": "Weg", "deleted": True}) == {"status": "afgerond"}


# ---------------------------------------------------------------- met database

class NepGoogle:
    """Google Taken in het geheugen."""

    def __init__(self):
        self.lijsten, self.taken, self.verzoeken, self.ids = {}, {}, [], itertools.count(1)
        self.fout = None

    def __call__(self, toegang, pad, methode=None, body=None):  # zelfde vorm als google._vraag
        self.verzoeken.append((methode or "GET", pad.split("?")[0]))
        if self.fout:
            raise self.fout
        pad, _, q = pad.partition("?")
        delen = [urllib.parse.unquote(x) for x in pad.strip("/").split("/")]
        if delen[:2] == ["users", "@me"]:
            if methode == "POST":
                lid = f"lijst{next(self.ids)}"
                self.lijsten[lid] = body["title"]
                return {"id": lid, "title": body["title"]}
            return {"items": [{"id": k, "title": v} for k, v in self.lijsten.items()]}
        lid = delen[1]
        if len(delen) == 3:
            if methode == "POST":
                tid = f"g{next(self.ids)}"
                self.taken[tid] = {"id": tid, **body, "lijst": lid}
                return self.taken[tid]
            return {"items": [x for x in self.taken.values() if x["lijst"] == lid]}
        tid = delen[3]
        if tid not in self.taken or self.taken[tid].get("deleted"):
            raise bronnen.NietGevonden("Google Taken: niet gevonden (404).")
        if methode == "DELETE":
            del self.taken[tid]
            return {}
        self.taken[tid] |= {k: v for k, v in body.items() if v is not None}
        if body.get("due", 1) is None:
            self.taken[tid].pop("due", None)
        return self.taken[tid]


@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        yield k


def _db():
    from app.kern import db  # pylint: disable=import-outside-toplevel
    return db.pool.connection()


@pytest.fixture
def nep(klant, monkeypatch):
    from app.clara import sync as agenda_sync, web as clara_web  # pylint: disable=import-outside-toplevel
    g = NepGoogle()
    monkeypatch.setattr(google, "_vraag", g)
    monkeypatch.setattr(agenda_sync, "toegang", lambda c, k: "token")
    monkeypatch.setattr(agenda_sync, "synchroniseer", lambda *a, **kw: None)
    g.afspraken, g.weg = [], []
    monkeypatch.setattr(clara_web, "_maak", lambda c, doel, titel, start, eind, oms="", **kw: g.afspraken.append(titel) or (g.kid, f"ev{len(g.afspraken)}"))
    monkeypatch.setattr(bronnen, "google_verwijder", lambda toegang, agenda, extern, melden=False: g.weg.append(extern))
    with _db() as c:
        c.execute("TRUNCATE takenbord, agenda_koppelingen CASCADE")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'taken_%%'")
        g.kid = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, account, geheim, schrijven, agendas, schrijfagenda)
                             VALUES ('google', 'Prive', 'ik@voorbeeld.nl', 'x', true,
                                     '[{"id": "primary", "naam": "Agenda", "schrijven": true, "aan": true}]', 'primary') RETURNING id""").fetchone()["id"]
    yield g
    with _db() as c:
        c.execute("TRUNCATE takenbord, agenda_koppelingen CASCADE")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'taken_%%'")


def _maak(c, **kw):
    velden = {"titel": "Rapport", "beschrijving": "", "prio": "middel", "status": "gepland", "wacht_op": "", "startdatum": None, "starttijd": None,
              "einddatum": None, "eindtijd": None, "doel": "", "project_id": None} | kw
    tid = taken_web.nieuw(c, velden)
    taken_web.agenda_bijwerken(c, tid)
    return c.execute("SELECT * FROM takenbord WHERE id = %s", (tid,)).fetchone()


def test_naar_google_en_terug(nep, klant):
    with _db() as c:
        t = _maak(c, titel="Rapport schrijven", einddatum=D, beschrijving="Hoofdstuk 3")
        assert t["agenda_soort"] == "google_taak" and t["agenda_extern"] and not t["agenda_fout"]
        g = nep.taken[t["agenda_extern"]]
        assert nep.lijsten[g["lijst"]] == "Buurmanitor" and g["title"] == "Rapport schrijven" and g["due"].startswith("2026-12-01")
        assert f"[taak:{t['id']}]" in g["notes"] and not nep.afspraken

        blok = _maak(c, titel="Werken aan rapport", startdatum=D, starttijd=dt.time(9), eindtijd=dt.time(12))
        assert blok["agenda_soort"] == "afspraak" and nep.afspraken == ["📋 Werken aan rapport"]  # met tijd: in de agenda
        los = _maak(c, titel="Ooit opruimen")
        assert not los["agenda_extern"]
        voor = len(nep.verzoeken)
        taken_web.agenda_bijwerken(c, t["id"])
        assert len(nep.verzoeken) == voor  # niets veranderd: niets naar Google

    pagina = klant.get("/taken", headers=KOP).text
    assert "staat in Google Taken" in pagina and "Met een datum staat de taak ook in Google Taken" in pagina

    # bewerken en afronden op het bord
    r = klant.post(f"/taken/{t['id']}", headers=FORM, follow_redirects=False, data={
        "titel": "Rapport afmaken", "beschrijving": "", "prio": "hoog", "status": "gepland", "einddatum": "2026-12-02"})
    assert r.status_code == 303
    assert nep.taken[t["agenda_extern"]]["title"] == "Rapport afmaken" and nep.taken[t["agenda_extern"]]["due"].startswith("2026-12-02")
    klant.post(f"/taken/{t['id']}/status", headers=FORM, data={"status": "afgerond"})
    assert nep.taken[t["agenda_extern"]]["status"] == "completed"  # afgevinkt, niet weg

    # terug uit Google: weer open, nieuwe titel en datum
    nep.taken[t["agenda_extern"]] |= {"status": "needsAction", "title": "Rapport (versie 2)", "due": "2026-12-04T00:00:00.000Z"}
    with _db() as c:
        assert "1 op het bord aangepast" in taken_web.taak_takengoogle(c)
        n = c.execute("SELECT * FROM takenbord WHERE id = %s", (t["id"],)).fetchone()
        assert n["status"] == "gepland" and n["titel"] == "Rapport (versie 2)" and n["einddatum"] == dt.date(2026, 12, 4) and n["afgerond"] is None
        # afgevinkt in Google
        nep.taken[t["agenda_extern"]]["status"] = "completed"
        taken_web.taak_takengoogle(c)
        assert c.execute("SELECT status FROM takenbord WHERE id = %s", (t["id"],)).fetchone()["status"] == "afgerond"
        # verwijderd in Google: afgerond, en niet opnieuw aangemaakt
        t2 = _maak(c, titel="Bellen met de wethouder", einddatum=D)
        nep.taken[t2["agenda_extern"]]["deleted"] = True
        taken_web.taak_takengoogle(c)
        n = c.execute("SELECT * FROM takenbord WHERE id = %s", (t2["id"],)).fetchone()
        assert n["status"] == "afgerond" and n["agenda_soort"] == "google_weg" and not n["agenda_extern"]
        taken_web.gelijktrekken(c)
        assert not c.execute("SELECT agenda_extern FROM takenbord WHERE id = %s", (t2["id"],)).fetchone()["agenda_extern"]
        # terug op het bord gezet: weer in Google
        taken_web._naar_kolom(c, t2["id"], "actief")  # pylint: disable=protected-access
        assert c.execute("SELECT agenda_soort FROM takenbord WHERE id = %s", (t2["id"],)).fetchone()["agenda_soort"] == "google_taak"

    # verwijderen op het bord: ook uit Google
    extern = t["agenda_extern"]
    klant.post(f"/taken/{t['id']}/weg", headers=FORM)
    assert extern not in nep.taken


def test_wisselen_vanaf_afspraken(nep, klant):
    with _db() as c:
        c.execute("INSERT INTO instellingen (sleutel, waarde) VALUES ('taken_vorm', 'afspraak'), ('taken_vorm_toegepast', 'afspraak')")
        t = _maak(c, titel="Deadline begroting", einddatum=D)
        assert t["agenda_soort"] == "afspraak" and t["agenda_extern"] == "ev1" and nep.afspraken == ["⏰ Deadline: Deadline begroting"]
    r = klant.get("/taken/instellingen", headers=KOP)
    assert "Taken als afspraak in de agenda: 1" in r.text and "Prive (ik@voorbeeld.nl)" in r.text
    klant.post("/taken/instellingen", headers=FORM, data={"taken_vorm": "google_taken", "taken_tijd_afspraak": "1", "taken_google_lijst": "Mijn werk"})
    for th in threading.enumerate():  # de achtergrondronde na het opslaan
        if th.name == "taken-gelijktrekken":
            th.join(10)
    with _db() as c:
        assert google.instellingen(c)["taken_vorm"] == "google_taken" and google.instellingen(c)["taken_google_lijst"] == "Mijn werk"
        assert c.execute("SELECT waarde FROM instellingen WHERE sleutel = 'taken_vorm_toegepast'").fetchone()["waarde"] == "google_taken"
        assert "nagekeken" not in taken_web.taak_takengoogle(c)  # al gedaan
        n = c.execute("SELECT * FROM takenbord WHERE id = %s", (t["id"],)).fetchone()
    assert nep.weg == ["ev1"] and n["agenda_soort"] == "google_taak" and nep.lijsten[n["agenda_id"]] == "Mijn werk"
    assert "Taken in Google Taken: 1" in klant.get("/taken/instellingen", headers=KOP).text
    assert klant.post("/taken/instellingen", headers=FORM, data={"taken_vorm": "raar"}).status_code == 400


def test_fout_zonder_toestemming(nep, klant):
    nep.fout = google.Fout("Buurmanitor mag nog niet bij je Google Taken. Koppel je Google-account opnieuw.")
    with _db() as c:
        t = _maak(c, titel="Stukken lezen", einddatum=D)
        assert "mag nog niet bij je Google Taken" in t["agenda_fout"]
        for i in range(5):
            _maak(c, titel=f"Taak {i}", einddatum=D)
        voor = len(nep.verzoeken)
        taken_web.taak_takengoogle(c)
        assert len(nep.verzoeken) - voor <= 3  # na drie keer mis niet alles één voor één proberen
    assert "lukte het niet: Buurmanitor mag nog niet" in klant.get("/taken/instellingen", headers=KOP).text
    assert "Google Taken: Buurmanitor mag nog niet" in klant.get(f"/taken?bewerk={t['id']}", headers=KOP).text
    nep.fout = None
    with _db() as c:
        taken_web.taak_takengoogle(c)  # weer toegang: het komt alsnog goed
        assert not c.execute("SELECT agenda_fout FROM takenbord WHERE id = %s", (t["id"],)).fetchone()["agenda_fout"]


def test_echte_foutteksten(monkeypatch):
    def http(url, **kw):
        raise bronnen.BronFout("Google Taken gaf fout 403: Request had insufficient authentication scopes.")
    monkeypatch.setattr(bronnen, "_http", http)
    with pytest.raises(google.Fout, match="Koppel je Google-account opnieuw"):
        google._vraag("t", "/users/@me/lists")  # pylint: disable=protected-access

    def uit(url, **kw):
        raise bronnen.BronFout("Google Taken gaf fout 403: Google Tasks API has not been used in project 123 before or it is disabled.")
    monkeypatch.setattr(bronnen, "_http", uit)
    with pytest.raises(google.Fout, match="Google Tasks API staat nog uit"):
        google._vraag("t", "/users/@me/lists")  # pylint: disable=protected-access
    assert "auth/tasks" in bronnen.GOOGLE_SCOPES
