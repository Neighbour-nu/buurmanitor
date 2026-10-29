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
