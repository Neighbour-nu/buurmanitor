"""Jesse: socials. Cijfers en posts uit de Doellijn (hier nagebootst), het gesprek met postvoorstellen en Vandaag.

De database- en schermtests draaien alleen met TEST_DATABASE_URL. Met de echte Doellijn: zie test_portal_zerohero.py.
Draaien in een eigen proces: python -m pytest tests/test_jesse.py (zie tests/alles.sh).
"""
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.jesse import gesprek, socials  # noqa: E402
from app.kern import config  # noqa: E402

TZ = config.TZ
DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}


def test_postvoorstellen_lezen():
    tekst = ('Deze werkte goed. Hier een voorstel.\\n'
             '<post>{"kanaal": "linkedin", "voor": "zerohero", "tekst": "Een keeper staat er alleen — en toch niet."}</post>\\n'
             '<post>{"kanaal": "tiktok", "tekst": "x"}</post>\\n'        # onbekend kanaal: vervalt
             '<post>{"kanaal": "instagram", "tekst": ""}</post>\\n'      # leeg: vervalt
             '<post>{"kanaal": "Threads", "voor": "onzin", "tekst": "Kort en goed."}</post>')
    rest, v = gesprek.voorstellen_uit(tekst.replace("\\\\n", "\\n"))
    assert rest.startswith("Deze werkte goed.") and "<post>" not in rest
    assert v == [{"kanaal": "linkedin", "voor": "zerohero", "tekst": "Een keeper staat er alleen, en toch niet."},
                 {"kanaal": "threads", "voor": "", "tekst": "Kort en goed."}]


def test_beste_post():
    nu = dt.datetime(2026, 11, 2, 12, tzinfo=TZ)
    p = [{"tijd": nu - dt.timedelta(days=1), "interacties": 12, "weergaven": 300},
         {"tijd": nu - dt.timedelta(days=2), "interacties": 40, "weergaven": 900},
         {"tijd": nu - dt.timedelta(days=9), "interacties": 99, "weergaven": 5000},   # te oud
         {"tijd": nu, "interacties": None, "weergaven": 50}]
    assert socials.beste(p, 7, nu)["interacties"] == 40
    assert socials.beste([], 7, nu) is None and socials.beste(None, 7, nu) is None


def test_zonder_doellijn():
    o = socials.overzicht(7)
    assert o["kanalen"] is None and o["posts"] is None and o["pijplijn"] is None and o["beste"] is None


@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        yield k


def _nep(monkeypatch):
    nu = dt.datetime.now(TZ)
    monkeypatch.setattr(socials, "kanalen", lambda dagen=7: [
        {"id": "linkedin", "naam": "LinkedIn Martijn", "volledig": "LinkedIn: Martijn Buurman", "heeft_cijfers": True, "volgers": 2412, "volgers_delta": 37,
         "hoofd_label": "Impressies", "hoofd": 18450, "hoofd_delta": -1200,
         "regels": [{"sleutel": "volgers", "label": "Volgers", "soort": "stand", "waarde": 2412, "delta": 37, "peil": nu.date()},
                    {"sleutel": "impressies", "label": "Impressies", "soort": "periode", "waarde": 18450, "delta": -1200, "peil": nu.date()}]},
        {"id": "instagram", "naam": "Instagram", "volledig": "Instagram", "heeft_cijfers": True, "volgers": 860, "volgers_delta": -3,
         "hoofd_label": "Weergaven", "hoofd": 5200, "hoofd_delta": 800,
         "regels": [{"sleutel": "volgers", "label": "Volgers", "soort": "stand", "waarde": 860, "delta": -3, "peil": nu.date()}]},
        {"id": "threads", "naam": "Threads", "volledig": "Threads", "heeft_cijfers": False, "volgers": None, "volgers_delta": None,
         "hoofd_label": None, "hoofd": None, "hoofd_delta": None, "regels": []}])
    monkeypatch.setattr(socials, "posts", lambda dagen=14: [
        {"kanaal": "instagram", "naam": "Instagram", "tekst": "Bijlow en de kunst van het wachten", "tijd": nu - dt.timedelta(days=1),
         "link": "https://instagram.com/p/1", "weergaven": 3100, "interacties": 210, "reacties": 14},
        {"kanaal": "linkedin", "naam": "LinkedIn Martijn", "tekst": "", "tijd": nu - dt.timedelta(days=3), "link": "https://linkedin.com/x",
         "weergaven": 5400, "interacties": 95, "reacties": None}])
    monkeypatch.setattr(socials, "pijplijn", lambda: [{"id": 42, "titel": "De stilte van Verbruggen", "linkedin": "Wat doet een keeper als...",
                                                       "instagram": "", "sport": "voetbal", "klaar": nu - dt.timedelta(hours=5)}])
    monkeypatch.setattr(socials, "vermeldingen", lambda dagen=14: [{"id": 1, "titel": "PRO Helmond wil sporthallen terug", "url": "https://ed.nl/1",
                                                                   "bron": "ED Helmond", "tijd": nu - dt.timedelta(days=2)}])


def test_scherm_gesprek_en_vandaag(klant, monkeypatch):
    from app.kern import claude, db  # pylint: disable=import-outside-toplevel
    _nep(monkeypatch)
    pagina = klant.get("/jesse", headers=KOP).text
    assert 'href="/jesse" class="actief">Socials' in pagina
    assert "2.412" in pagina and "+37" in pagina and "-1.200" in pagina and "nog geen cijfers" in pagina
    beste = pagina.split('class="sbeste"')[1].split("</div>\n      </div>")[0]
    assert "Bijlow en de kunst van het wachten" in beste and "210 interacties" in beste
    assert 'href="/zerohero/concepten/42">De stilte van Verbruggen' in pagina and "PRO Helmond wil sporthallen terug" in pagina
    assert 'href="/zerohero/bereik/linkedin?periode=7"' in pagina
    # Het gesprek: Jesse kent de cijfers en schrijft een voorstel om te kopiëren.
    gezien = {}

    def nep(c, persona, systeem, berichten, **kw):
        gezien.update(persona=persona, systeem=systeem)
        return ('Instagram liep deze week het best.\\n'
                '<post>{"kanaal": "linkedin", "voor": "zerohero", "tekst": "Wat doet een keeper als er niets gebeurt? [plek]"}</post>')
    monkeypatch.setattr(claude, "gesprek", nep)
    r = klant.post("/jesse/gesprek/vraag", headers=FORM, follow_redirects=False, data={"vraag": "Maak een post van het concept"})
    assert r.status_code == 303 and "#jesse" in r.headers["location"]
    assert gezien["persona"] == "jesse" and "LinkedIn: Martijn Buurman: laatste 7 dagen volgers 2.412 (+37" in gezien["systeem"]
    assert "[42] De stilte van Verbruggen" in gezien["systeem"] and "PRO Helmond wil sporthallen terug" in gezien["systeem"]
    pagina = klant.get(r.headers["location"], headers=KOP).text
    assert "Instagram liep deze week het best." in pagina and "Wat doet een keeper als er niets gebeurt? [plek]</textarea>" in pagina
    assert "Kopiëren" in pagina and "voor ZEROHERO" in pagina
    with db.pool.connection() as c:
        vid = c.execute("SELECT id FROM jesse_voorstellen ORDER BY id DESC LIMIT 1").fetchone()["id"]
    assert klant.post(f"/jesse/voorstel/{vid}/post", headers=FORM).status_code == 404  # posten bestaat niet
    klant.post(f"/jesse/voorstel/{vid}/tekst", headers=FORM, data={"tekst": "Aangepaste tekst."})
    klant.post(f"/jesse/voorstel/{vid}/bewaar", headers=FORM)
    pagina = klant.get("/jesse", headers=KOP).text
    assert "<h3>Bewaard</h3>" in pagina and "Aangepaste tekst.</textarea>" in pagina
    assert klant.post(f"/jesse/voorstel/{vid}/weg", headers=FORM | {"X-Jesse": "1"}).json() == {"ok": True}
    assert "Aangepaste tekst." not in klant.get("/jesse", headers=KOP).text
    # Vandaag: Socials in plaats van Werkruimtes.
    vandaag = klant.get("/", headers=KOP).text
    blok = vandaag.split('id="socials"')[1].split("</section>")[0]
    assert "LinkedIn Martijn" in blok and "2.412" in blok and "+37" in blok and "Threads" not in blok  # zonder cijfers: niet op Vandaag
    assert "Beste post deze week" in blok and "1 concept</a> klaar om te posten" in blok
    assert "<h2>Werkruimtes</h2>" not in vandaag
    with db.pool.connection() as c:
        c.execute("DELETE FROM gesprekken WHERE persona = 'jesse'")
        c.execute("DELETE FROM jesse_voorstellen")
