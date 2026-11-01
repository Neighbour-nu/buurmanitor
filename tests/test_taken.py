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


# ---------------------------------------------------------------- Noor

def test_noor_voorstellen_lezen():
    from app.taken import noor  # pylint: disable=import-outside-toplevel
    tekst = ('Ik zou het rapport donderdag doen.\n'
             '<taak>{"titel": "Rapport: hoofdstuk 1", "prio": "hoog", "status": "gepland", "startdatum": "2026-11-12", "starttijd": "09:00", "eindtijd": "12:00"}</taak>\n'
             '<wijzig>{"id": 4, "status": "wachtend", "wacht_op": "de griffie", "einddatum": ""}</wijzig>\n'
             '<wijzig>{"id": 99, "status": "actief"}</wijzig>\n'          # onbekende taak: vervalt
             '<wijzig>{"id": 4, "prio": "superhoog"}</wijzig>\n'          # ongeldige prioriteit: vervalt
             '<taak>{"titel": "", "prio": "laag"}</taak>\n'                # geen titel: vervalt
             '<taak>{"titel": "Bellen", "startdatum": "morgen"}</taak>\n'  # onleesbare datum: vervalt
             '<wijzig>{"id": 4, "titel": "", "starttijd": "14.30"}</wijzig>')
    rest, v = noor.voorstellen_uit(tekst, {4, 5})
    assert rest == "Ik zou het rapport donderdag doen."
    assert [x["soort"] for x in v] == ["nieuw", "wijzig", "wijzig"]
    assert v[0]["gegevens"] == {"titel": "Rapport: hoofdstuk 1", "prio": "hoog", "status": "gepland", "startdatum": dt.date(2026, 11, 12),
                                "starttijd": dt.time(9), "eindtijd": dt.time(12)}
    assert v[1]["taak_id"] == 4 and v[1]["gegevens"] == {"status": "wachtend", "wacht_op": "de griffie", "einddatum": None}
    assert v[2]["gegevens"] == {"starttijd": dt.time(14, 30)}  # lege titel telt niet als wijziging
    assert noor.beschrijf("nieuw", v[0]["gegevens"]) == "Nieuwe taak: Rapport: hoofdstuk 1 · prio hoog · Gepland · do 12-11 09.00-12.00"
    taak = {"titel": "Motie OV", "startdatum": None, "starttijd": None, "einddatum": dt.date(2026, 11, 20), "eindtijd": None}
    assert noor.beschrijf("wijzig", v[1]["gegevens"], taak) == "Motie OV · naar Wachtend op · zonder datum (uit de agenda) · wacht op de griffie"
    # Opslaan als JSON en weer terug.
    assert noor.gegevens_terug(noor._json(v[0]["gegevens"])) == v[0]["gegevens"]  # pylint: disable=protected-access


def test_noor_wat_vandaag_telt():
    from app.taken import noor  # pylint: disable=import-outside-toplevel
    n = iter(range(1, 100))

    def taak(titel, **kw):
        return {"id": next(n), "titel": titel, "status": "nieuw", "prio": "middel", "startdatum": None, "starttijd": None,
                "einddatum": None, "eindtijd": None, "wacht_op": ""} | kw
    taken = [taak("Ooit", prio="laag"),
             taak("Rapport", status="gepland", startdatum=D, starttijd=dt.time(9), eindtijd=dt.time(12)),
             taak("Te laat", einddatum=D - dt.timedelta(days=1)),
             taak("Bezig", status="actief"),
             taak("Deze week", einddatum=D + dt.timedelta(days=3)),
             taak("Belangrijk", prio="hoog"),
             taak("Klaar", status="afgerond", einddatum=D - dt.timedelta(days=5)),
             taak("Wachten", status="wachtend", wacht_op="Jan", prio="hoog")]
    lijst = noor.voor_vandaag(taken, D)
    assert [(t["titel"], t["reden"]) for t in lijst] == [("Te laat", "te laat"), ("Rapport", "vandaag 09.00"), ("Bezig", "actief"),
                                                         ("Deze week", "af do 12-11"), ("Belangrijk", "hoog")]
    assert noor.moment(taken[1]) == "ma 09-11 09.00-12.00" and noor.moment(taken[2]) == "deadline zo 08-11"


def test_noor_gesprek_en_vandaag(klant, monkeypatch):
    from app.kern import claude, db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        c.execute("DELETE FROM takenbord")
        oud = web.nieuw(c, {"titel": "Motie OV voorbereiden", "beschrijving": "", "prio": "hoog", "status": "nieuw"})
        web.nieuw(c, {"titel": "Bellen met Jan", "beschrijving": "", "prio": "middel", "status": "wachtend", "wacht_op": "Jan"})
    gezien = {}

    def nep(c, persona, systeem, berichten, **kw):
        gezien.update(persona=persona, systeem=systeem)
        return ("Zet de motie in triage met hoge prioriteit, en voeg de presentatie toe.\n"
                f'<wijzig>{{"id": {oud}, "status": "triage"}}</wijzig>\n'
                '<taak>{"titel": "Presentatie congres", "prio": "middel", "status": "nieuw"}</taak>')
    monkeypatch.setattr(claude, "gesprek", nep)
    assert "Vraag Noor" in klant.get("/taken", headers=KOP).text
    r = klant.post("/taken/gesprek/vraag", headers=FORM, data={"vraag": "Wat moet ik eerst doen?"}, follow_redirects=False)
    assert r.status_code == 303 and "#noor" in r.headers["location"]
    assert gezien["persona"] == "noor" and f"[taak {oud}] Motie OV voorbereiden" in gezien["systeem"] and "wacht op Jan" in gezien["systeem"]
    pagina = klant.get(r.headers["location"], headers=KOP).text
    assert "Zet de motie in triage" in pagina and "Motie OV voorbereiden · naar Triage" in pagina
    assert "Nieuwe taak: Presentatie congres · prio middel · Nieuw" in pagina
    with db.pool.connection() as c:
        vs = c.execute("SELECT * FROM taak_voorstellen ORDER BY id").fetchall()
        assert [v["soort"] for v in vs] == ["wijzig", "nieuw"]
        assert c.execute("SELECT status FROM takenbord WHERE id = %s", (oud,)).fetchone()["status"] == "nieuw"  # nog niets veranderd
    # Doorvoeren en niet doen.
    r = klant.post(f"/taken/voorstel/{vs[0]['id']}", headers=FORM, data={"actie": "doe"}, follow_redirects=False)
    assert "bijgewerkt" in r.headers["location"]
    klant.post(f"/taken/voorstel/{vs[1]['id']}", headers=FORM, data={"actie": "weg"})
    with db.pool.connection() as c:
        assert c.execute("SELECT status FROM takenbord WHERE id = %s", (oud,)).fetchone()["status"] == "triage"
        assert not c.execute("SELECT 1 FROM takenbord WHERE titel = 'Presentatie congres'").fetchone()
        assert [v["status"] for v in c.execute("SELECT status FROM taak_voorstellen ORDER BY id")] == ["uitgevoerd", "afgewezen"]
    assert "al+afgehandeld" in klant.post(f"/taken/voorstel/{vs[0]['id']}", headers=FORM, data={"actie": "doe"}, follow_redirects=False).headers["location"]
    # Vandaag: de taken, afvinken en snel toevoegen; daarna terug op Vandaag.
    pagina = klant.get("/", headers=KOP).text
    assert 'id="taken"' in pagina and "Motie OV voorbereiden" in pagina and ">hoog<" in pagina and "Je wacht op Jan." in pagina
    assert '<b>' not in pagina.split('class="noot taakteller">')[1].split("</p>")[0]  # niets actief
    assert 'href="/taken?praat=1#noor">Vraag Noor</a>' in pagina
    r = klant.post("/taken/nieuw", headers=FORM, data={"titel": "Fietsband plakken", "terug": "/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?melding=") and r.headers["location"].endswith("#taken")
    assert "Fietsband plakken" in klant.get(r.headers["location"], headers=KOP).text.split('id="taken"')[1].split("</section>")[0]
    r = klant.post(f"/taken/{oud}/status", headers=FORM, data={"status": "afgerond", "terug": "/"}, follow_redirects=False)
    assert r.headers["location"].endswith("#taken")
    assert "Motie OV voorbereiden" not in klant.get("/", headers=KOP).text.split('id="taken"')[1].split("</section>")[0].split("</p>", 1)[1]
    with db.pool.connection() as c:
        c.execute("DELETE FROM takenbord")
        c.execute("DELETE FROM gesprekken WHERE persona = 'noor'")
