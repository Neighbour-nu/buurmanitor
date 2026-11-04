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
    # Afgerond: wat nog moest komen gaat uit de agenda, wat liep stopt, wat voorbij was blijft staan met een vinkje.
    af = lambda tijd, **kw: web.afspraak(t(status="afgerond", afgerond=tijd, **kw))  # noqa: E731
    week = dict(startdatum=D, einddatum=D + dt.timedelta(days=4))  # ma 9 t/m vr 13 november
    assert af(dt.datetime(2026, 11, 6, 15, tzinfo=TZ), **week) is None  # eerder af dan begonnen: weg
    a = af(dt.datetime(2026, 11, 10, 15, tzinfo=TZ), **week)  # dinsdag af: stopt na dinsdag
    assert a["titel"] == "✓ Rapport" and a["start"].date() == D and a["eind"] == dt.datetime(2026, 11, 11, tzinfo=TZ) and a["heledag"]
    a = af(dt.datetime(2026, 11, 20, 9, tzinfo=TZ), **week)  # pas later afgevinkt: blijft heel staan, met vinkje
    assert a["titel"] == "✓ Rapport" and a["eind"] == dt.datetime(2026, 11, 14, tzinfo=TZ)
    blok = dict(startdatum=D, starttijd=dt.time(9), eindtijd=dt.time(12))
    a = af(dt.datetime(2026, 11, 9, 10, 37, tzinfo=TZ), **blok)  # om 10.37 klaar: blok tot 10.40
    assert a["eind"] == dt.datetime(2026, 11, 9, 10, 40, tzinfo=TZ)
    assert af(dt.datetime(2026, 11, 9, 8, 0, tzinfo=TZ), **blok) is None
    assert af(dt.datetime(2026, 11, 3, 8, 0, tzinfo=TZ), einddatum=D) is None  # deadline nog niet bereikt: weg
    assert af(dt.datetime(2026, 11, 9, 16, 0, tzinfo=TZ), einddatum=D)["titel"] == "✓ Rapport"  # op de dag zelf af: blijft
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
    # Afgerond voordat het blok begint (over drie dagen): de afspraak gaat uit de agenda.
    voor = (len(gemaakt), len(weg))
    r = klant.post(f"/taken/{tid}/status", headers=FORM, data={"status": "afgerond"}, follow_redirects=False)
    assert "Afgerond" in r.headers["location"] and len(gemaakt) == voor[0] and len(weg) == voor[1] + 1 and weg[-1] == f"ev{len(gemaakt)}"
    with db.pool.connection() as c:
        assert c.execute("SELECT agenda_extern FROM takenbord WHERE id = %s", (tid,)).fetchone()["agenda_extern"] == ""
    # Een taak die al afgerond was voordat dit zo werkte (afspraak met vinkje in de toekomst): de planner zet het recht.
    with db.pool.connection() as c:
        oud = web.nieuw(c, {"titel": "Oude taak", "beschrijving": "", "prio": "laag", "status": "afgerond", "startdatum": s, "doel": f"{k}|primary"})
        c.execute("UPDATE takenbord SET agenda_koppeling = %s, agenda_id = 'primary', agenda_extern = 'oud-ev', agenda_spec = 'oud' WHERE id = %s", (k, oud))
        assert web.taak_takenagenda(c) == "1 afgeronde taken nagekeken, 1 afspraken aangepast of weggehaald" and weg[-1] == "oud-ev"
        assert web.taak_takenagenda(c) == "0 afgeronde taken nagekeken, 0 afspraken aangepast of weggehaald"
        c.execute("DELETE FROM takenbord WHERE id = %s", (oud,))
    # Toch niet af: terug naar Actief, en de afspraak komt terug.
    klant.post(f"/taken/{tid}/status", headers=FORM, data={"status": "actief"})
    assert len(gemaakt) == voor[0] + 1 and gemaakt[-1][1] == "📋 Presentatie congres"
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


def test_afgerond_compact_en_archief(klant):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        c.execute("DELETE FROM takenbord")
        ids = {}
        for titel, status, dagen in (("Open klus", "actief", None), ("Rapport ingeleverd", "afgerond", 2), ("Oud werk", "afgerond", 45),
                                     ("Factuur betaald", "afgerond", 1)):
            ids[titel] = c.execute("""INSERT INTO takenbord (titel, beschrijving, status, afgerond) VALUES (%s, 'details hier', %s,
                                      CASE WHEN %s::int IS NOT NULL THEN now() - make_interval(days => %s::int) END) RETURNING id""",
                                   (titel, status, dagen, dagen)).fetchone()["id"]
        assert web.taak_takenarchief(c) == "1 afgeronde taak gearchiveerd"  # ouder dan 30 dagen
    pagina = klant.get("/taken", headers=KOP).text
    kolom = pagina.split('id="kolom-afgerond"')[1].split("</section>")[0]
    assert 'class="taak klaar"' in kolom and "Rapport ingeleverd" in kolom and "Oud werk" not in pagina
    assert kolom.index("Factuur betaald") < kolom.index("Rapport ingeleverd")  # laatst afgerond bovenaan
    assert "label prio" not in kolom and "taakverplaats" not in kolom  # alleen de titel
    assert 'href="/taken/archief">📦 Takenarchief</a> <span class="noot">1 taak</span>' in pagina
    # Eén taak archiveren; een open taak kan niet.
    r = klant.post(f"/taken/{ids['Rapport ingeleverd']}/archief", headers=FORM, data={"actie": "in"}, follow_redirects=False)
    assert "archief" in r.headers["location"]
    assert "fout=" in klant.post(f"/taken/{ids['Open klus']}/archief", headers=FORM, data={"actie": "in"}, follow_redirects=False).headers["location"]
    archief = klant.get("/taken/archief", headers=KOP).text
    assert "Rapport ingeleverd" in archief and "Oud werk" in archief and "Open klus" not in archief and "2 afgeronde taken" in archief
    assert archief.index("Rapport ingeleverd") < archief.index("Oud werk")
    zoek = klant.get("/taken/archief?q=rapport", headers=KOP).text
    assert "Rapport ingeleverd" in zoek and "Oud werk" not in zoek
    # Alles archiveren, en terug op het bord.
    klant.post("/taken/archiveren", headers=FORM)
    assert "Factuur betaald" not in klant.get("/taken", headers=KOP).text.split('id="kolom-afgerond"')[1].split("</section>")[0]
    klant.post(f"/taken/{ids['Oud werk']}/archief", headers=FORM, data={"actie": "uit"})
    assert "Oud werk" in klant.get("/taken", headers=KOP).text
    # Een gearchiveerde taak heropenen haalt hem ook uit het archief; verwijderen vanuit het archief gaat terug naar het archief.
    klant.post(f"/taken/{ids['Factuur betaald']}/status", headers=FORM, data={"status": "actief"})
    r = klant.post(f"/taken/{ids['Rapport ingeleverd']}/weg", headers=FORM, data={"terug": "/taken/archief"}, follow_redirects=False)
    assert r.headers["location"].startswith("/taken/archief?")
    with db.pool.connection() as c:
        rijen = {x["titel"]: x["gearchiveerd"] is not None for x in c.execute("SELECT titel, gearchiveerd FROM takenbord").fetchall()}
        assert rijen == {"Open klus": False, "Oud werk": False, "Factuur betaald": False}
        c.execute("DELETE FROM takenbord")


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
    reeks = [taak("Week voorbereiden", status="gepland", startdatum=D, einddatum=D, reeks_id=1),
             taak("Week voorbereiden", status="gepland", startdatum=D + dt.timedelta(days=7), einddatum=D + dt.timedelta(days=7), reeks_id=1, prio="hoog")]
    assert [t["reden"] for t in noor.voor_vandaag(reeks, D)] == ["vandaag"]  # volgende week niet, ook niet met hoge prioriteit


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


# ---------------------------------------------------------------- herhalende taken

def test_reeks_datums_en_beschrijving():
    from app.taken import reeksen  # pylint: disable=import-outside-toplevel
    r = {"soort": "week", "dagen": ["ma", "do"], "interval": 1, "begindatum": D, "einddatum": D + dt.timedelta(days=20),
         "starttijd": dt.time(9), "eindtijd": dt.time(10)}
    lijst = reeksen.datums(r, D, D + dt.timedelta(days=60))
    assert [d.isoformat() for d in lijst] == ["2026-11-09", "2026-11-12", "2026-11-16", "2026-11-19", "2026-11-23", "2026-11-26"]
    assert reeksen.beschrijf(r) == "elke maandag en donderdag, 09.00-10.00, vanaf 09-11-2026 tot en met 29-11-2026"
    om = r | {"interval": 2, "dagen": ["wo"], "einddatum": None}
    assert [d.isoformat() for d in reeksen.datums(om, D, D + dt.timedelta(days=30))] == ["2026-11-11", "2026-11-25", "2026-12-09"]
    assert reeksen.beschrijf(om).startswith("om de 2 weken op woensdag, 09.00-10.00, vanaf 09-11-2026")
    werk = r | {"dagen": ["ma", "di", "wo", "do", "vr"], "starttijd": None, "eindtijd": None, "einddatum": None}
    assert reeksen.beschrijf(werk) == "elke werkdag, vanaf 09-11-2026"
    maand = {"soort": "maand", "interval": 1, "begindatum": dt.date(2026, 1, 31), "einddatum": dt.date(2026, 5, 1), "dagen": []}
    assert [d.isoformat() for d in reeksen.datums(maand, dt.date(2026, 1, 1), dt.date(2026, 12, 31))] == \
        ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]  # bestaat de dag niet, dan de laatste van de maand
    assert reeksen.beschrijf(maand) == "elke maand op de 31e, vanaf 31-01-2026 tot en met 01-05-2026"


def test_reeks_formulier():
    from app.taken import reeksen  # pylint: disable=import-outside-toplevel

    class F(dict):
        def getlist(self, k):
            v = self.get(k)
            return v if isinstance(v, list) else ([v] if v else [])
    v, fout = reeksen.uit_formulier(F(titel="Week voorbereiden", dagen=["ma", "zz"], interval="9", begindatum="2026-11-09", starttijd="09:00",
                                      eindtijd="10:00", status="afgerond"), D)
    assert not fout and v["dagen"] == ["ma"] and v["interval"] == 9 and v["status"] == "gepland" and v["starttijd"] == dt.time(9)
    assert reeksen.uit_formulier(F(titel="x", soort="week"), D)[1] == "Kies op welke dagen de taak terugkomt."
    assert "einddatum" in reeksen.uit_formulier(F(titel="x", dagen=["ma"], begindatum="2026-11-09", einddatum="2026-11-01"), D)[1]
    assert reeksen.uit_formulier(F(titel="", dagen=["ma"]), D)[1].startswith("Geef")


def test_reeks_maken_wijzigen_stoppen(klant, monkeypatch):
    from app.clara import bronnen, sync  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    from app.taken import reeksen  # pylint: disable=import-outside-toplevel
    gemaakt, weg = [], []
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k, forceer=False, nu=None: None)
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="", kleur=None, heledag=False, **kw:
                        gemaakt.append((titel, s, e, heledag)) or f"ev{len(gemaakt)}")
    monkeypatch.setattr(bronnen, "google_verwijder", lambda tok, a, eid, melden=False: weg.append(eid))
    with db.pool.connection() as c:
        c.execute("DELETE FROM takenbord")
        c.execute("DELETE FROM takenreeksen")
        ag = [{"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True, "aan": True, "bezet": True}]
        k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, schrijven, agendas, schrijfagenda) VALUES ('google', 'Privé', true, %s, 'primary')
                         RETURNING id""", (json.dumps(ag),)).fetchone()["id"]
    vandaag = dt.datetime.now(TZ).date()
    assert 'href="/taken?reeks=nieuw#reeks"' in klant.get("/taken", headers=KOP).text
    assert "Hoe vaak" in klant.get("/taken?reeks=nieuw", headers=KOP).text
    alle_dagen = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    assert "fout=" in klant.post("/taken/reeks/nieuw", headers=FORM, follow_redirects=False, data={"titel": "x"}).headers["location"]
    r = klant.post("/taken/reeks/nieuw", headers=FORM, follow_redirects=False, data={
        "titel": "Week voorbereiden", "soort": "week", "dagen": alle_dagen, "interval": "1", "begindatum": vandaag.isoformat(),
        "einddatum": (vandaag + dt.timedelta(days=13)).isoformat(), "starttijd": "09:00", "eindtijd": "09:30", "doel": f"{k}|primary"})
    assert "14+keer+gepland" in r.headers["location"], r.headers["location"]
    assert len(gemaakt) == 14 and gemaakt[0][0] == "📋 Week voorbereiden" and gemaakt[0][1].hour == 9 and gemaakt[0][2].minute == 30
    pagina = klant.get("/taken", headers=KOP).text
    assert pagina.count("🔁 </span>Week voorbereiden") == 8  # alleen de komende week op het bord
    assert "6 taken uit herhalende taken staan later gepland" in pagina and "elke dag, 09.00-09.30" in pagina and "14 open, 0 afgerond" in pagina
    with db.pool.connection() as c:
        rid = c.execute("SELECT id FROM takenreeksen").fetchone()["id"]
        morgen = c.execute("SELECT id FROM takenbord WHERE reeks_id = %s AND startdatum = %s", (rid, vandaag + dt.timedelta(days=1))).fetchone()["id"]
    # Eén keer weghalen: komt niet terug als de planner aanvult.
    klant.post(f"/taken/{morgen}/weg", headers=FORM)
    with db.pool.connection() as c:
        assert reeksen.aanvullen(c, rid) == 0
        assert c.execute("SELECT count(*) AS n FROM takenbord WHERE reeks_id = %s", (rid,)).fetchone()["n"] == 13
    # Vandaag afronden, dan wijzigen naar alleen maandag: open taken vanaf vandaag opnieuw, de afgeronde blijft.
    with db.pool.connection() as c:
        vid = c.execute("SELECT id FROM takenbord WHERE reeks_id = %s AND startdatum = %s", (rid, vandaag)).fetchone()["id"]
    klant.post(f"/taken/{vid}/status", headers=FORM, data={"status": "afgerond"})
    voor = len(weg)
    r = klant.post(f"/taken/reeks/{rid}", headers=FORM, follow_redirects=False, data={
        "titel": "Week voorbereiden", "soort": "week", "dagen": ["ma"], "interval": "1", "begindatum": vandaag.isoformat(),
        "einddatum": (vandaag + dt.timedelta(days=13)).isoformat(), "starttijd": "09:00", "eindtijd": "09:30", "doel": f"{k}|primary"})
    assert "gewijzigd" in r.headers["location"]
    with db.pool.connection() as c:
        rijen = c.execute("SELECT startdatum, status FROM takenbord WHERE reeks_id = %s ORDER BY startdatum", (rid,)).fetchall()
    maandagen = [d for d in (vandaag + dt.timedelta(days=i) for i in range(14)) if d.weekday() == 0 and d != vandaag]
    assert [x["startdatum"] for x in rijen if x["status"] != "afgerond"] == maandagen
    assert [x["startdatum"] for x in rijen if x["status"] == "afgerond"] == [vandaag] and len(weg) - voor >= 11
    # Stoppen: open taken weg (ook uit de agenda), de reeks blijft zichtbaar als gestopt.
    r = klant.post(f"/taken/reeks/{rid}/stop", headers=FORM, follow_redirects=False)
    assert "gestopt" in r.headers["location"]
    with db.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM takenbord WHERE reeks_id = %s AND status <> 'afgerond'", (rid,)).fetchone()["n"] == 0
        assert not c.execute("SELECT actief FROM takenreeksen WHERE id = %s", (rid,)).fetchone()["actief"]
        assert reeksen.taak_reeksen(c) == "0 taken uit herhalende taken gemaakt"
    # Verwijderen: de reeks weg; de afgeronde taak blijft op het bord (zonder reeks).
    klant.post(f"/taken/reeks/{rid}/weg", headers=FORM)
    with db.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM takenreeksen WHERE id = %s", (rid,)).fetchone()
        assert c.execute("SELECT reeks_id FROM takenbord WHERE id = %s", (vid,)).fetchone()["reeks_id"] is None
        c.execute("DELETE FROM takenbord")
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (k,))
