"""CRM: relaties uit de mail, kansen per activiteit (met de volgende stap als taak), projecten met sjablonen, en kansen
maken vanuit een mail, nieuwsbericht of socialpost (ook ingevuld door Iris, en als voorstel in het gesprek met Iris).

Claude is nagebootst. De database- en schermtests draaien alleen met TEST_DATABASE_URL.
Draaien in een eigen proces: python -m pytest tests/test_crm.py (zie tests/alles.sh).
"""
import datetime as dt
import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.crm import model, relaties, voorstel  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}


# ---------------------------------------------------------------- zonder database

def test_bedragen_fases_en_gewogen():
    assert model.bedrag("€ 1.250,50") == Decimal("1250.50") and model.bedrag("2500") == Decimal("2500.00")
    assert model.bedrag("12.500") == Decimal("12500.00") and model.bedrag("12.5") == Decimal("12.50") and model.bedrag("1.000.000") == Decimal("1000000.00")
    assert model.bedrag("") is None and model.bedrag("veel") is None
    assert model.fase_naam("initiatief", "partners") == "Partners zoeken" and model.uitkomst("commercieel", "verloren") == "verloren"
    assert model.eerste_fase("initiatief") == "idee"
    assert model.gewogen({"waarde": Decimal("1000"), "fase": "voorstel", "kans": None}) == Decimal("500.00")
    assert model.gewogen({"waarde": Decimal("1000"), "fase": "voorstel", "kans": 80}) == Decimal("800.00")
    assert model.gewogen({"waarde": None, "fase": "voorstel"}) == 0
    vandaag = dt.date(2026, 11, 13)
    assert model.achter({"volgende_datum": dt.date(2026, 11, 12)}, vandaag)
    assert not model.achter({"volgende_datum": dt.date(2026, 11, 12), "gesloten": dt.datetime.now()}, vandaag)


def test_sjablonen():
    regels = model.sjabloon_regels("Locatie vastleggen | -180\n- Sprekers bevestigen|-90\n\nCongresdag\nEvaluatie | +7\nRaar | x")
    assert regels == [{"titel": "Locatie vastleggen", "dagen": -180}, {"titel": "Sprekers bevestigen", "dagen": -90},
                      {"titel": "Congresdag", "dagen": 0}, {"titel": "Evaluatie", "dagen": 7}, {"titel": "Raar", "dagen": 0}]
    assert model.sjabloon_tekst(regels[:4]) == "Locatie vastleggen | -180\nSprekers bevestigen | -90\nCongresdag\nEvaluatie | +7"
    dag = dt.date(2027, 2, 3)
    taken = model.taken_uit_sjabloon(regels[:2], dag)
    assert taken[0]["einddatum"] == dt.date(2026, 8, 7) and taken[1]["einddatum"] == dt.date(2026, 11, 5)
    assert model.taken_uit_sjabloon(regels[:1], None)[0]["einddatum"] is None
    terug = model.taken_als_sjabloon([{"titel": "B", "einddatum": dag + dt.timedelta(days=7)}, {"titel": "A", "einddatum": dag - dt.timedelta(days=30)},
                                      {"titel": "C", "einddatum": None}], dag)
    assert terug == [{"titel": "A", "dagen": -30}, {"titel": "B", "dagen": 7}, {"titel": "C", "dagen": 0}]


def test_afzenders_en_organisaties():
    assert relaties.is_automatisch("noreply@kpn.com") and relaties.is_automatisch("nieuwsbrief@helmond.nl")
    assert relaties.is_automatisch("notifications@github.com") and relaties.is_automatisch("jan@club.nl", nieuwsbrief=True)
    assert relaties.is_automatisch("x@y.nl", "Vodafone No Reply")
    assert not relaties.is_automatisch("jan.jansen@knvb.nl") and not relaties.is_automatisch("info@keeperschool.nl")
    assert relaties.organisatie_domein("jan@mail.upperspire.com") == "upperspire.com"
    assert relaties.organisatie_domein("piet@gmail.com") == "" and relaties.organisatie_domein("a@bbc.co.uk") == "bbc.co.uk"
    assert relaties.organisatie_naam("knvb.nl") == "Knvb"


def test_voorstel_uit_antwoord():
    tekst = 'Prima, hier is mijn voorstel. <crm>{"wat": "kans", "titel": "Workshop KNVB", "activiteit": "zerohero", ' \
            '"volgende_datum": "2026-11-20", "waarde": "1500", "bericht_id": "12"}</crm> <crm>{"wat": "onzin", "activiteit": "mars", ' \
            '"titel": "X", "volgende_datum": "morgen"}</crm><crm>{kapot</crm>'

    class Nep:
        def execute(self, *a):
            return self

        def fetchall(self):
            return [{"code": "zerohero", "naam": "ZEROHERO", "soort": "commercieel"}]
    schoon, lijst = voorstel.uit_antwoord(Nep(), tekst)
    assert schoon.startswith("Prima, hier is mijn voorstel.") and "<crm>" not in schoon.replace("<crm>{kapot</crm>", "")
    assert lijst[0] == {"wat": "kans", "titel": "Workshop KNVB", "activiteit": "zerohero", "volgende_datum": "2026-11-20",
                        "waarde": "1500", "bericht_id": "12"}
    assert lijst[1] == {"wat": "kans", "titel": "X", "volgende_datum": ""} and len(lijst) == 2


# ---------------------------------------------------------------- met database

@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        from app.kern import db  # pylint: disable=import-outside-toplevel
        with db.pool.connection() as c:
            c.execute("TRUNCATE crm_kansen, crm_projecten, crm_contacten, crm_organisaties, crm_sjablonen, takenbord, post_koppelingen CASCADE")
        yield k


def _db():
    from app.kern import db  # pylint: disable=import-outside-toplevel
    return db.pool.connection()


def test_relaties_uit_de_mail(klant):
    with _db() as c:
        kid = c.execute("""INSERT INTO post_koppelingen (soort, naam, account, domein, geheim) VALUES ('microsoft', 'Upper Spire', 'martijn@upperspire.com',
                           'upperspire', 'x') RETURNING id""").fetchone()["id"]
        for i, (naam, adres, nb) in enumerate((("Jan de Vries", "jan@klant.nl", False), ("Jan de Vries", "JAN@klant.nl", False),
                                               ("KPN", "noreply@kpn.com", False), ("Piet", "piet@gmail.com", False),
                                               ("Nieuws", "redactie@krant.nl", True), ("Ikzelf", "martijn@upperspire.com", False))):
            c.execute("""INSERT INTO post_berichten (koppeling_id, extern_id, van_naam, van_adres, onderwerp, ontvangen, nieuwsbrief)
                         VALUES (%s, %s, %s, %s, 'Hallo', now() - make_interval(hours => %s), %s)""", (kid, f"m{i}", naam, adres, i, nb))
        assert relaties.bijwerken(c) == (2, 0)
        jan = c.execute("SELECT p.*, o.naam AS org FROM crm_contacten p LEFT JOIN crm_organisaties o ON o.id = p.organisatie_id WHERE email = 'jan@klant.nl'").fetchone()
        assert jan["mails"] == 2 and jan["org"] == "Klant" and jan["activiteit"] == "upperspire" and jan["naam"] == "Jan de Vries"
        assert c.execute("SELECT organisatie_id FROM crm_contacten WHERE email = 'piet@gmail.com'").fetchone()["organisatie_id"] is None
        c.execute("UPDATE crm_contacten SET naam = 'Jan (inkoop)' WHERE id = %s", (jan["id"],))
        assert relaties.bijwerken(c) == (0, 2)
        assert c.execute("SELECT naam FROM crm_contacten WHERE id = %s", (jan["id"],)).fetchone()["naam"] == "Jan (inkoop)"  # eigen naam blijft
    pagina = klant.get("/crm/relaties", headers=KOP).text
    assert "Jan (inkoop)" in pagina and "noreply@kpn.com" not in pagina and "Klant" in pagina
    assert "jan@klant.nl" in klant.get(f"/crm/contact/{jan['id']}", headers=KOP).text


def test_kans_van_mail_tot_project(klant, monkeypatch):
    with _db() as c:
        bid = c.execute("SELECT id FROM post_berichten WHERE extern_id = 'm0'").fetchone()["id"]
    # Vanuit de mail: vooraf ingevuld met afzender, organisatie en activiteit (de mailbox van Upper Spire).
    pagina = klant.get(f"/crm/nieuw?bron=mail&id={bid}", headers=KOP).text
    assert 'value="jan@klant.nl"' in pagina and 'value="Klant"' in pagina and '<option value="upperspire" data-soort="commercieel" selected>' in pagina
    # Iris vult in (nagebootst).
    from app.crm import voorstel as vs  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(vs.claude, "gesprek", lambda *a, **kw: '{"wat": "kans", "titel": "Migratie archief Klant", "activiteit": "upperspire", '
                                                            '"omschrijving": "Jan vraagt een offerte.", "volgende_stap": "Offerte sturen", '
                                                            '"volgende_datum": "2026-11-20", "waarde": "12500"}')
    monkeypatch.setattr(vs.config, "ANTHROPIC_API_KEY", "x")
    r = klant.post("/crm/nieuw/iris", headers=FORM, data={"qs": f"bron=mail&id={bid}"}, follow_redirects=False)
    ingevuld = klant.get(r.headers["location"], headers=KOP).text
    assert 'value="Migratie archief Klant"' in ingevuld and 'value="Offerte sturen"' in ingevuld and "ingevuld door Iris" in ingevuld
    # Aanmaken: kans met de mail op de tijdlijn, en de volgende stap als taak bij Noor.
    r = klant.post("/crm/nieuw", headers=FORM, follow_redirects=False, data={
        "wat": "kans", "titel": "Migratie archief Klant", "activiteit": "upperspire", "fase": "contact", "organisatie": "Klant",
        "contact_naam": "Jan de Vries", "contact_email": "jan@klant.nl", "waarde": "12.500", "volgende_stap": "Offerte sturen",
        "volgende_datum": "2026-11-20", "bron": "mail", "bericht_id": str(bid), "bron_titel": "Hallo", "bron_url": f"/bram/bericht/{bid}"})
    assert r.status_code == 303 and r.headers["location"].startswith("/crm/kans/")
    kid = int(r.headers["location"].split("/")[3].split("?")[0])
    with _db() as c:
        k = c.execute("SELECT * FROM crm_kansen WHERE id = %s", (kid,)).fetchone()
        taak = c.execute("SELECT * FROM takenbord WHERE id = %s", (k["taak_id"],)).fetchone()
        assert k["waarde"] == Decimal("12500.00") and k["fase"] == "contact" and k["contact_id"]
        assert taak["titel"] == "Offerte sturen (Migratie archief Klant)" and taak["einddatum"] == dt.date(2026, 11, 20) and taak["kans_id"] == kid
        assert c.execute("SELECT count(*) AS n FROM crm_tijdlijn WHERE kans_id = %s AND soort = 'mail' AND bericht_id = %s", (kid, bid)).fetchone()["n"] == 1
    # Het bord en het overzicht.
    bord = klant.get("/crm?activiteit=upperspire", headers=KOP).text
    assert "Migratie archief Klant" in bord and "€ 12.500" in bord and 'data-fase="onderhandeling"' in bord
    overzicht = klant.get("/crm", headers=KOP).text
    assert "Offerte sturen" in overzicht and "€ 12.500 totaal" in overzicht and "€ 3.125 gewogen" in overzicht  # contact: 25%
    # Fase: gewonnen -> de taak is klaar en Buurmanitor vraagt om een project.
    r = klant.post(f"/crm/kans/{kid}/fase", headers=FORM, data={"fase": "gewonnen"}, follow_redirects=False)
    assert "project" in r.headers["location"]
    with _db() as c:
        assert c.execute("SELECT status FROM takenbord WHERE id = %s", (k["taak_id"],)).fetchone()["status"] == "afgerond"
        assert c.execute("SELECT gesloten FROM crm_kansen WHERE id = %s", (kid,)).fetchone()["gesloten"]
    assert "Maak er een project van" in klant.get(f"/crm/kans/{kid}", headers=KOP).text
    r = klant.post(f"/crm/kans/{kid}/project", headers=FORM, follow_redirects=False)
    pid = int(r.headers["location"].split("/")[3].split("?")[0])
    with _db() as c:
        p = c.execute("SELECT * FROM crm_projecten WHERE id = %s", (pid,)).fetchone()
        assert p["budget_opbrengst"] == Decimal("12500.00") and p["kans_id"] == kid and p["activiteit"] == "upperspire"
    # Een notitie en een link op de tijdlijn; een verkeerde link wordt geen link.
    klant.post(f"/crm/project/{pid}/notitie", headers=FORM, data={"tekst": "Kick-off gepland", "url": "javascript:alert(1)"})
    klant.post(f"/crm/project/{pid}/notitie", headers=FORM, data={"tekst": "Offerte", "url": "https://drive.google.com/x"})
    pagina = klant.get(f"/crm/project/{pid}", headers=KOP).text
    assert "Kick-off gepland" in pagina and 'href="https://drive.google.com/x"' in pagina and "javascript:" not in pagina


def test_projecten_sjablonen_en_takenbord(klant, monkeypatch):
    from app.taken import web as taken_web  # pylint: disable=import-outside-toplevel
    naar_agenda = []
    monkeypatch.setattr(taken_web, "agenda_op_achtergrond", lambda tids, weg=None: naar_agenda.extend(tids))
    r = klant.post("/crm/sjablonen", headers=FORM, follow_redirects=False,
                   data={"naam": "Keeperscongres", "activiteit": "zerohero", "taken": "Locatie vastleggen | -180\nSprekers bevestigen | -90\nEvaluatie | +7"})
    assert "3+taken" in r.headers["location"]
    with _db() as c:
        sid = c.execute("SELECT id FROM crm_sjablonen WHERE naam = 'Keeperscongres'").fetchone()["id"]
    r = klant.post("/crm/project/0", headers=FORM, follow_redirects=False,
                   data={"titel": "Keeperscongres 2027", "activiteit": "zerohero", "datum": "2027-02-03", "sjabloon": str(sid), "status": "gepland"})
    pid = int(r.headers["location"].split("/")[3].split("?")[0])
    with _db() as c:
        taken = c.execute("SELECT titel, einddatum FROM takenbord WHERE project_id = %s ORDER BY einddatum", (pid,)).fetchall()
    assert [(t["titel"], t["einddatum"]) for t in taken] == [("Locatie vastleggen", dt.date(2026, 8, 7)), ("Sprekers bevestigen", dt.date(2026, 11, 5)),
                                                              ("Evaluatie", dt.date(2027, 2, 10))]
    assert len(naar_agenda) == 3  # de deadlines gaan (op de achtergrond) ook de agenda in, net als bij Noor
    klant.post(f"/crm/project/{pid}/taak", headers=FORM, data={"titel": "Catering regelen", "einddatum": "2027-01-15"})
    assert len(naar_agenda) == 4
    pagina = klant.get(f"/crm/project/{pid}", headers=KOP).text
    assert "Catering regelen" in pagina and "0/4" in pagina
    # Op het takenbord: met het project erbij, en te filteren.
    bord = klant.get(f"/taken?project={pid}", headers=KOP).text
    assert "Alleen de taken van het project" in bord and "Catering regelen" in bord and "Offerte sturen" not in bord
    assert f'href="/crm/project/{pid}">Keeperscongres 2027</a>' in klant.get("/taken", headers=KOP).text
    # Van dit project een nieuw sjabloon (voor de volgende uitgave), en budget opslaan.
    r = klant.post(f"/crm/project/{pid}/sjabloon", headers=FORM, data={"maak": "1", "naam": "Congres v2"}, follow_redirects=False)
    assert "4+taken" in r.headers["location"]
    with _db() as c:
        assert {"titel": "Catering regelen", "dagen": -19} in c.execute("SELECT taken FROM crm_sjablonen WHERE naam = 'Congres v2'").fetchone()["taken"]
    klant.post(f"/crm/project/{pid}", headers=FORM, data={"titel": "Keeperscongres 2027", "activiteit": "zerohero", "status": "actief",
                                                         "budget_kosten": "8.000", "budget_opbrengst": "15000", "datum": "2027-02-03"})
    pagina = klant.get(f"/crm/project/{pid}", headers=KOP).text
    assert "€ 7.000" in pagina
    lijst = klant.get("/crm/projecten?activiteit=zerohero", headers=KOP).text
    assert "Keeperscongres 2027" in lijst and "Actief" in lijst


def test_politiek_initiatief_en_bronnen(klant):
    # Politiek: fases van een initiatief, zonder geld.
    r = klant.post("/crm/nieuw", headers=FORM, follow_redirects=False, data={
        "wat": "kans", "titel": "Burendag 2027 Helmond", "activiteit": "politiek", "fase": "partners", "waarde": "999",
        "organisatie": "Oranje Fonds", "bron": "nieuws", "bron_titel": "Burendag 2027 aangekondigd", "bron_url": "https://www.burendag.nl/x"})
    kid = int(r.headers["location"].split("/")[3].split("?")[0])
    with _db() as c:
        k = c.execute("SELECT * FROM crm_kansen WHERE id = %s", (kid,)).fetchone()
        assert k["fase"] == "partners" and k["waarde"] is None and k["taak_id"] is None
    pagina = klant.get(f"/crm/kans/{kid}", headers=KOP).text
    assert "Partners zoeken" in pagina and "Burendag 2027 aangekondigd" in pagina
    # Een nieuwsbericht vooraf ingevuld (uit Vera), en koppelen aan een bestaande kans.
    nieuws = klant.get("/crm/nieuw?bron=nieuws&titel=Week+van+de+Pleegzorg&url=https://x.nl/a&stroom=politiek&van=Omroep+Brabant", headers=KOP).text
    assert 'value="Week van de Pleegzorg"' in nieuws and '<option value="politiek" data-soort="initiatief" selected>' in nieuws
    r = klant.post("/crm/nieuw", headers=FORM, follow_redirects=False,
                   data={"koppel": f"kans:{kid}", "bron": "nieuws", "bron_titel": "Meer over Burendag", "bron_url": "https://x.nl/b"})
    assert r.headers["location"].startswith(f"/crm/kans/{kid}")
    assert "Meer over Burendag" in klant.get(f"/crm/kans/{kid}", headers=KOP).text
    # Fouten: zonder activiteit of titel terug naar het formulier, met wat al was ingevuld.
    r = klant.post("/crm/nieuw", headers=FORM, follow_redirects=False, data={"wat": "kans", "titel": "Iets"})
    assert "fout=Kies" in r.headers["location"] and "titel=Iets" in r.headers["location"]
    # Een nieuwsbericht in Vera en een post bij Jesse krijgen een knop.
    assert klant.post("/crm/kans/999999/fase", headers=FORM, data={"fase": "idee"}).status_code == 404
    assert klant.post(f"/crm/kans/{kid}/fase", headers=FORM, data={"fase": "gewonnen"}).status_code == 400  # niet bij een initiatief


def test_voorstel_van_iris(klant, monkeypatch):
    from app import iris  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(iris.claude, "gesprek", lambda *a, **kw: 'Goed idee. <crm>{"wat": "project", "titel": "Week van de Pleegzorg 2027", '
                                                              '"activiteit": "politiek", "datum": "2027-11-01", "volgende_stap": "Burgemeester mailen"}</crm>')
    with _db() as c:
        gid = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('iris', 'test') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', 'Maak een project voor de Week van de Pleegzorg')", (gid,))
        assert iris.antwoord(c, gid) == "Goed idee."
        vid = c.execute("SELECT id FROM crm_voorstellen WHERE gesprek_id = %s", (gid,)).fetchone()["id"]
    pagina = klant.get(f"/iris?g={gid}", headers=KOP).text
    assert "📁 Project: <b>Week van de Pleegzorg 2027</b>" in pagina and f"/crm/nieuw?voorstel={vid}" in pagina
    formulier = klant.get(f"/crm/nieuw?voorstel={vid}&wat=project", headers=KOP).text
    assert 'value="Week van de Pleegzorg 2027"' in formulier and 'value="2027-11-01"' in formulier and 'value="project" checked' in formulier
    r = klant.post("/crm/nieuw", headers=FORM, follow_redirects=False, data={
        "wat": "project", "titel": "Week van de Pleegzorg 2027", "activiteit": "politiek", "datum": "2027-11-01",
        "volgende_stap": "Burgemeester mailen", "voorstel": str(vid)})
    pid = int(r.headers["location"].split("/")[3].split("?")[0])
    with _db() as c:
        v = c.execute("SELECT * FROM crm_voorstellen WHERE id = %s", (vid,)).fetchone()
        assert v["status"] == "gemaakt" and v["project_id"] == pid
        assert c.execute("SELECT titel FROM takenbord WHERE project_id = %s", (pid,)).fetchone()["titel"] == "Burgemeester mailen"
    assert "aangemaakt" in klant.get(f"/iris?g={gid}", headers=KOP).text
    # Iris weet wat er loopt.
    with _db() as c:
        tekst = iris.context(c)
    assert "Burendag 2027 Helmond" in tekst and "Week van de Pleegzorg 2027" in tekst
