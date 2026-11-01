"""Werkruimte ZEROHERO (de Doellijn) onder /zerohero in Buurmanitor: schermen, links, htmx, doorverwijzingen en /intern.

Draait alleen met twee lege, wegwerpbare databases:
  TEST_DATABASE_URL (Buurmanitor) en TEST_ZEROHERO_DATABASE_URL (ZEROHERO).
Draaien in een eigen proces: python -m pytest tests/test_portal_zerohero.py (zie tests/alles.sh).
"""
import os
import pathlib
import re

BM, ZH = os.environ.get("TEST_DATABASE_URL"), os.environ.get("TEST_ZEROHERO_DATABASE_URL")
if BM and ZH:
    os.environ["DATABASE_URL"] = BM
    os.environ["ZEROHERO_DATABASE_URL"] = ZH
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["ZEROHERO_ACHTERGROND"] = "0"
os.environ["ZEROHERO_CONCEPT_SECRET"] = "conceptgeheim"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not (BM and ZH), reason="geen TEST_DATABASE_URL en TEST_ZEROHERO_DATABASE_URL")

KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORMULIER = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
SCHERMEN = ["/", "/inbox", "/inbox?status=bewaard", "/artikelen", "/concepten", "/congres", "/dashboard",
            "/dashboard?periode=90", "/dashboard/cijfers", "/dashboard/linkedin", "/dashboard/podcast",
            "/dashboard/profielbezoek", "/dashboard/ssi", "/bereik/youtube", "/bereik/podcast", "/links", "/adviseur",
            "/filter"]
LINK = re.compile(r'\b(?:href|action|src|formaction|hx-(?:get|post|put|patch|delete))="(/[^"]*)"'
                  r"|fetch\(\s*['\"](/[^'\"]*)|location(?:\.href)?\s*=\s*['\"](/[^'\"]*)")


@pytest.fixture(scope="module")
def klant():
    import psycopg  # pylint: disable=import-outside-toplevel
    with psycopg.connect(ZH, autocommit=True) as c:
        c.execute(pathlib.Path(__file__).with_name("zerohero").joinpath("basis_schema.sql").read_text())
        c.execute("""INSERT INTO items (miniflux_id, bron, url, titel, gepubliceerd, sport, keepers, score, samenvatting, status)
                     SELECT g, 'Test', 'https://voorbeeld.nl/' || g, 'Keeper houdt stand ' || g, now() - g * interval '1 hour',
                            'voetbal', ARRAY['Test Keeper'], 40 + g, 'Samenvatting ' || g, 'nieuw'
                     FROM generate_series(1, 5) g ON CONFLICT DO NOTHING""")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    assert main.zerohero is not None, "ZEROHERO is niet gekoppeld"
    with TestClient(main.app) as k:
        yield k


def test_alle_schermen_en_links(klant):
    fout = []
    for scherm in SCHERMEN:
        r = klant.get("/zerohero" + scherm, headers=KOP)
        assert r.status_code == 200, (scherm, r.status_code, r.text[:300])
        for m in LINK.finditer(r.text):
            pad = next(g for g in m.groups() if g)
            if not pad.startswith("//") and not pad.startswith("/zerohero"):
                fout.append((scherm, pad))
    assert not fout, f"links zonder /zerohero: {fout[:10]}"


def test_huisstijl_logo_en_htmx_lokaal(klant):
    css = klant.get("/zerohero/static/style.css", headers=KOP)
    assert css.status_code == 200 and "#2563B8" in css.text and "Anton" not in css.text
    assert klant.get("/zerohero/static/htmx.min.js", headers=KOP).status_code == 200
    assert klant.get("/zerohero/static/fonts/BarlowCondensed-SemiBold.woff", headers=KOP).status_code == 200
    pagina = klant.get("/zerohero/inbox", headers=KOP).text
    assert "BUURMANITOR" in pagina and "/zerohero/static/logo.svg" in pagina and 'href="https://buurmanitor.neighbour.nu/"' in pagina
    assert "fonts.googleapis" not in pagina and "unpkg.com" not in pagina


def test_htmx_actie_en_herkomst(klant):
    item = klant.get("/zerohero/inbox", headers=KOP)
    assert 'hx-post="/zerohero/' in item.text
    r = klant.post("/zerohero/items/status", headers=FORMULIER | {"HX-Request": "true"}, data={"ids": "1", "status": "bewaard"})
    assert r.status_code == 200, r.text[:300]
    kwaad = klant.post("/zerohero/items/status", headers=KOP | {"Origin": "https://cockpit.zerohero.one"},
                       data={"ids": "1", "status": "bewaard"})
    assert kwaad.status_code == 403


def test_doorverwijzing_krijgt_voorvoegsel(klant):
    r = klant.post("/zerohero/filter/regels", headers=FORMULIER, data={"tekst": "testregel"}, follow_redirects=False)
    assert r.status_code in (302, 303, 307), (r.status_code, r.text[:200])
    assert r.headers["location"].startswith("/zerohero/filter"), r.headers["location"]


def test_intern_voor_n8n_zonder_voorvoegsel(klant):
    # n8n roept http://cockpit-app:8000/intern/... aan, zonder proxygeheim en zonder login.
    # Leerpunten: open binnen het servernetwerk, met het geheim als het verzoek via een proxy komt (eigen controle).
    assert klant.get("/intern/weging/leerpunten").status_code == 200
    assert klant.get("/intern/weging/leerpunten", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
    met = klant.get("/intern/weging/leerpunten", headers={"X-Forwarded-For": "1.2.3.4", "X-Doellijn-Secret": "conceptgeheim"})
    assert met.status_code == 200, (met.status_code, met.text[:200])
    # MailerLite-peil: alleen met het geheim.
    assert klant.post("/intern/mailerlite", json={}).status_code in (401, 403)
    # De rest van de portal blijft afgeschermd.
    assert klant.get("/meldingen").status_code == 401


def test_portal_linkt_naar_zerohero(klant):
    assert 'href="/zerohero"' in klant.get("/", headers=KOP).text
    for pad in ["/", "/iris", "/meldingen", "/gezondheid"]:
        assert klant.get(pad, headers=KOP).status_code == 200, pad


def test_vera_leest_keepersnieuws(klant):
    """Vera laat het keepersnieuws uit de Doellijn zien (alleen lezen) en klikt door naar de bron."""
    from app.zerohero import main as zh  # pylint: disable=import-outside-toplevel
    with zh.pool.connection() as c:
        iid = c.execute("SELECT id FROM items WHERE titel = 'Keeper houdt stand 1'").fetchone()["id"]
    pagina = klant.get("/vera?soort=keepers&dagen=7", headers=KOP).text
    assert "Test Keeper" in pagina and "De Doellijn draait hier nog niet" not in pagina
    # Vijf berichten over dezelfde keeper: ontdubbeld tot het belangrijkste; met 'alles laten zien' staan ze er allemaal.
    assert "Keeper houdt stand 5" in pagina and "Keeper houdt stand 1<" not in pagina
    alles = klant.get("/vera?soort=keepers&dagen=7&alles=1", headers=KOP).text
    assert all(f"Keeper houdt stand {n}<" in alles for n in range(1, 6))
    r = klant.get(f"/vera/lees/keepers/{iid}", headers=KOP, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://voorbeeld.nl/1"


def test_jesse_leest_de_doellijn(klant):
    """Jesse leest cijfers, posts en concepten uit de echte tabellen van de Doellijn (alleen lezen)."""
    from app.jesse import socials  # pylint: disable=import-outside-toplevel
    from app.zerohero import main as zh  # pylint: disable=import-outside-toplevel
    zh.q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES
            ('instagram', 'volgers', current_date - 10, 800, 'api'), ('instagram', 'volgers', current_date, 860, 'api'),
            ('instagram', 'weergaven', current_date - 1, 2500, 'api'), ('instagram', 'weergaven', current_date - 9, 2000, 'api')
            ON CONFLICT DO NOTHING RETURNING 1""")
    zh.q("""INSERT INTO meta_posts (id, kanaal, tekst, gepubliceerd, link, likes, reacties, gedeeld, weergaven, interacties)
            VALUES ('ig1', 'instagram', 'Bijlow en de kunst van het wachten', now() - interval '1 day', 'https://instagram.com/p/ig1', 180, 20, 10, 3100, NULL)
            ON CONFLICT (id) DO NOTHING RETURNING 1""")
    zh.q("""INSERT INTO lp_posts (url, publicatie, interacties, impressies) VALUES ('https://linkedin.com/feed/1', current_date - 2, 95, 5400)
            ON CONFLICT (url) DO NOTHING RETURNING 1""")
    zh.q("""INSERT INTO yt_videos (id, titel, gepubliceerd, weergaven, likes, reacties) VALUES ('abc', 'Keepersjournaal 12', now() - interval '3 days', 400, 30, 5)
            ON CONFLICT (id) DO NOTHING RETURNING 1""")
    zh.q("""INSERT INTO concepten (item_ids, status, blog_titel, linkedin, klaar_op) VALUES ('{1}', 'klaar', 'De stilte van Verbruggen', 'Wat doet een keeper', now())
            RETURNING id""")
    k = {x["id"]: x for x in socials.kanalen(7)}
    assert k["instagram"]["volgers"] == 860 and k["instagram"]["volgers_delta"] == 60 and k["instagram"]["hoofd"] == 2500
    posts = socials.posts(14)
    soorten = {p["kanaal"]: p for p in posts}
    assert soorten["instagram"]["interacties"] == 210 and soorten["linkedin"]["weergaven"] == 5400
    assert soorten["youtube"]["link"] == "https://www.youtube.com/watch?v=abc"
    assert socials.beste(posts)["kanaal"] == "instagram"
    assert any(p["titel"] == "De stilte van Verbruggen" for p in socials.pijplijn())
    pagina = klant.get("/jesse", headers=KOP).text
    assert "Bijlow en de kunst van het wachten" in pagina and "De stilte van Verbruggen" in pagina and "Keepersjournaal 12" in pagina
    blok = klant.get("/", headers=KOP).text.split('id="socials"')[1].split("</section>")[0]
    assert "Instagram" in blok and "860" in blok and "+60" in blok
