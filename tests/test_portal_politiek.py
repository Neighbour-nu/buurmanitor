"""Werkruimte Politiek (Promonitor) onder /politiek in Buurmanitor: alle schermen, links en doorverwijzingen.

Draait alleen met twee lege, wegwerpbare databases:
  TEST_DATABASE_URL (Buurmanitor) en TEST_POLITIEK_DATABASE_URL (Politiek).
Draaien in een eigen proces: python -m pytest tests/test_portal_politiek.py (zie tests/alles.sh).
"""
import os
import re

BM, POL = os.environ.get("TEST_DATABASE_URL"), os.environ.get("TEST_POLITIEK_DATABASE_URL")
if BM and POL:
    os.environ["DATABASE_URL"] = BM
    os.environ["POLITIEK_DATABASE_URL"] = POL
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["POLITIEK_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not (BM and POL), reason="geen TEST_DATABASE_URL en TEST_POLITIEK_DATABASE_URL")

KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORMULIER = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
SCHERMEN = ["/", "/inbox", "/agenda", "/bijdragen", "/vragen", "/concepten", "/fractie", "/acties", "/whaller",
            "/dossiers", "/dashboard", "/adviseur", "/instellingen", "/sjablonen", "/filter", "/briefing", "/stemadvies",
            "/begroting", "/bewaking"]
LINK = re.compile(r'\b(?:href|action|src|formaction)="(/[^"]*)"|fetch\(\s*[\'"](/[^\'"]*)')


@pytest.fixture(scope="module")
def klant():
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    assert main.politiek is not None, "Politiek is niet gekoppeld"
    with TestClient(main.app) as k:
        yield k


def test_alle_schermen_en_links(klant):
    fout = []
    for scherm in SCHERMEN:
        r = klant.get("/politiek" + scherm, headers=KOP)
        assert r.status_code == 200, (scherm, r.status_code, r.text[:300])
        for m in LINK.finditer(r.text):
            pad = m.group(1) or m.group(2)
            if not pad.startswith("//") and not pad.startswith("/politiek"):
                fout.append((scherm, pad))
    assert not fout, f"links zonder /politiek: {fout[:10]}"


def test_huisstijl_en_logo(klant):
    r = klant.get("/politiek/static/style.css", headers=KOP)
    assert r.status_code == 200 and "#2563B8" in r.text
    pagina = klant.get("/politiek/", headers=KOP).text
    assert "BUURMANITOR" in pagina and "/politiek/static/logo.svg" in pagina
    assert "fonts.googleapis" not in pagina and "unpkg.com" not in pagina


def test_doorverwijzing_krijgt_voorvoegsel(klant):
    r = klant.post("/politiek/inbox/alles-gezien", headers=FORMULIER, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/politiek/inbox")


def test_formulier_van_andere_site_geweigerd(klant):
    r = klant.post("/politiek/inbox/alles-gezien", headers=KOP | {"Origin": "https://promonitor.neighbour.nu.kwaad.nl"},
                   follow_redirects=False)
    assert r.status_code == 403


def test_word_downloads_ongewijzigd(klant):
    r = klant.get("/politiek/sjablonen/motie/leeg", headers=KOP)
    assert r.status_code == 200 and r.content[:2] == b"PK"  # een .docx is een zip


def test_portal_zelf_werkt_nog(klant):
    for pad in ["/", "/iris", "/meldingen", "/gezondheid"]:
        r = klant.get(pad, headers=KOP)
        assert r.status_code == 200, pad
    assert 'href="/politiek"' in klant.get("/", headers=KOP).text


def test_vera_leest_politiek_nieuws(klant):
    """Vera laat het gewogen politieke nieuws uit Promonitor zien (alleen lezen) en klikt door naar de bron."""
    from app.politiek import main as pm  # pylint: disable=import-outside-toplevel
    with pm.pool.connection() as c:
        c.execute("DELETE FROM stukken WHERE extern_id LIKE 'nieuws-vera-%'")
        sid = c.execute("""INSERT INTO stukken (bron, extern_id, soort, titel, datum, document_url, context, bronnaam, score, reden, onderwerp, actie)
                           VALUES ('nieuws', 'nieuws-vera-1', 'nieuws', 'College schrapt sportsubsidie', current_date, 'https://www.ed.nl/vera1',
                                   'Het college wil bezuinigen op sport.', 'ED Helmond', 84, 'Raakt het dossier sportaccommodaties.', 'sportsubsidie', 'vragen')
                           RETURNING id""").fetchone()["id"]
        c.execute("""INSERT INTO stukken (bron, extern_id, soort, titel, datum, document_url, bronnaam, score, status)
                     VALUES ('nieuws', 'nieuws-vera-2', 'nieuws', 'Genegeerd bericht', current_date, 'https://www.ed.nl/vera2', 'ED', 90, 'genegeerd')""")
    pagina = klant.get("/vera?soort=politiek", headers=KOP).text
    assert "College schrapt sportsubsidie" in pagina and "Raakt het dossier sportaccommodaties." in pagina and "vragen overwegen" in pagina
    assert "Genegeerd bericht" not in pagina and "Promonitor draait hier nog niet" not in pagina
    assert "College schrapt sportsubsidie" in klant.get("/vera", headers=KOP).text.split('class="nuitgelicht"')[1]
    r = klant.get(f"/vera/lees/politiek/{sid}", headers=KOP, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://www.ed.nl/vera1"
    with pm.pool.connection() as c:
        assert not c.execute("SELECT gezien FROM stukken WHERE id = %s", (sid,)).fetchone()["gezien"]  # Promonitor blijft ongemoeid
        c.execute("DELETE FROM stukken WHERE extern_id LIKE 'nieuws-vera-%'")



def test_whaller_indeling_uit_pdf(klant, monkeypatch):
    """De PDF met woordvoerders inlezen bij de commissieavond: voorstel, bevestigen, daarna vast."""
    import datetime as dt  # pylint: disable=import-outside-toplevel
    from app.politiek import main as pol, notubiz  # pylint: disable=import-outside-toplevel
    start = dt.datetime(2026, 11, 10, 19, 0)

    def punt(o, sectie="Adviserend", tijd=""):
        return {"onderwerp": o, "standaard": False, "sectie": sectie, "diepte": 0, "tijd": tijd, "portefeuillehouder": ""}
    avond = {"id": 1, "naam": "Commissieavond", "start": start, "url": "https://helmond.notubiz.nl/x", "commissies": [
        {"naam": "Commissie A", "plek": "Raadzaal", "agenda": [
            {**punt("Opening en mededelingen", ""), "standaard": True},
            punt("Afsluiten grondexploitatie Cacaofabriek e.o.", tijd="19.05-19.50"),
            punt("Aanpak evenementen locatieprofielen Helmond Centrum", "Opinierend", "20.00-21.15"),
            punt("GREX Nieuw Brandevoort", "Technische informatiebijeenkomst", "21.25-22.25")]},
        {"naam": "Commissie B", "plek": "Commissiekamer", "agenda": [
            punt("Bestedingsplan Wonen", tijd="19.05-19.50"),
            punt("Aanvraag krediet tbv aankoop voormalige politiekantoor", tijd="20.00-20.45"),
            punt("Vaststelling Omgevingsplanwijziging Burgemeester Krollaan 101", tijd="20.45-21.15")]}]}
    monkeypatch.setattr(notubiz, "evenementen", lambda van, tot: [
        {"id": 1, "soort": "commissie", "type": "assembly", "naam": "Commissieavond", "start": start, "plek": "", "url": ""}])
    monkeypatch.setattr(notubiz, "commissieavond", lambda nummer: avond)
    pol.uitvoeren("DELETE FROM whaller_invoer WHERE sleutel = 'commissie:2026-11-10'")
    pol.uitvoeren("DELETE FROM toewijzingen WHERE datum = '2026-11-10'")
    week = "/politiek/whaller?week=2026-11-09"
    assert 'action="/politiek/whaller/indeling"' in klant.get(week, headers=KOP).text

    with open(os.path.join(os.path.dirname(__file__), "politiek", "fixtures", "indeling_commissie_10-11-2026.pdf"), "rb") as f:
        r = klant.post("/politiek/whaller/indeling", headers=FORMULIER, data={"datum": "2026-11-10", "week": "2026-11-09"},
                       files={"bestand": ("Commissieavond_10-11-2026_indeling.pdf", f, "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303 and "6+agendapunten+met+woordvoerders" in r.headers["location"], r.headers["location"]
    pagina = klant.get(week, headers=KOP).text
    assert pagina.count("uit de PDF:") == 6 and "uit de PDF: Niek en Petra" in pagina and "Bevestigen en opslaan" in pagina
    assert "Ook in de PDF: Sanae Voorzitter; Deliana met vakantie" in pagina
    assert "Afsluiten grondexploitatie Cacaofabriek e.o. - (19.05-19.50 uur) | @n.engbers &amp; @PetraVerdonk" in pagina
    assert "Bestedingsplan Wonen - (19.05-19.50 uur) | @martijn_buurman &amp; @StijnvanHoek" in pagina
    # Bevestigen = het formulier met de (ingevulde) keuzes opslaan.
    velden = {"soort": "commissie", "datum": "2026-11-10", "week": "2026-11-09"}
    for i, (o, wv, erbij) in enumerate((("Afsluiten grondexploitatie Cacaofabriek e.o.", "n.engbers", "PetraVerdonk"),
                                       ("Bestedingsplan Wonen", "martijn_buurman", "StijnvanHoek")), 1):
        velden |= {f"onderwerp_{i}": o, f"wv_{i}": wv, f"erbij_{i}": erbij}
    klant.post("/politiek/whaller/toewijzingen", headers=FORMULIER, data=velden)
    pagina = klant.get(week, headers=KOP).text
    assert "Bevestigen en opslaan" not in pagina and pagina.count("✓ zoals in de PDF") == 2 and "zijn bevestigd" in pagina
    # Na bevestigen stuurt de PDF niet meer bij (hier zijn er bewust maar twee opgeslagen).
    assert pagina.count("uit de PDF:") == 0
    # Een PDF zonder namen of geen PDF: nette melding.
    r = klant.post("/politiek/whaller/indeling", headers=FORMULIER, data={"datum": "2026-11-10", "week": "2026-11-09"},
                   files={"bestand": ("x.pdf", b"geen pdf", "application/pdf")}, follow_redirects=False)
    assert "fout=Dat+is+geen+PDF" in r.headers["location"]
    klant.post("/politiek/whaller/indeling/weg", headers=FORMULIER, data={"sleutel": "commissie:2026-11-10", "week": "2026-11-09"})
    assert "📄" not in klant.get(week, headers=KOP).text
