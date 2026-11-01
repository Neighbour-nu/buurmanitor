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



def test_jesse_reacties_op_eigen_posts(klant, monkeypatch):
    """Jesse haalt de reacties op je eigen posts op via de koppelingen van de Doellijn (Meta en Threads hier nagebootst)."""
    from app.jesse import luisteren  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    from app.zerohero import main as zh, meta, threads  # pylint: disable=import-outside-toplevel
    zh.q("""INSERT INTO meta_posts (id, kanaal, tekst, gepubliceerd, link) VALUES
            ('ig7', 'instagram', 'Bijlow', now() - interval '1 day', 'https://instagram.com/p/ig7'),
            ('fb7', 'facebook', 'Podcast', now() - interval '2 days', 'https://facebook.com/fb7'),
            ('th7', 'threads', 'Keepers', now() - interval '1 day', 'https://threads.net/t/th7')
            ON CONFLICT (id) DO NOTHING RETURNING 1""")
    monkeypatch.setattr(meta, "actief", lambda: True)
    monkeypatch.setattr(meta, "kies_pagina", lambda: {"id": "pag1", "access_token": "pt", "instagram_business_account": {"username": "zerohero.one"}})
    antwoorden = {
        "ig7/comments": {"data": [{"id": "c1", "text": "Waar kan ik je boek kopen?", "username": "keeperfan", "timestamp": "2026-11-01T10:00:00+0000"},
                                  {"id": "c2", "text": "Dank je!", "username": "zerohero.one", "timestamp": "2026-11-01T11:00:00+0000"}]},
        "fb7/comments": {"data": [{"id": "f1", "message": "Mooie aflevering", "from": {"id": "u9", "name": "Kees"}, "created_time": "2026-11-01T09:00:00+0000"}]}}
    monkeypatch.setattr(meta, "_get", lambda pad, params=None, token=None: antwoorden[pad])
    monkeypatch.setattr(threads, "_q_token", lambda q: ("tt", None))
    monkeypatch.setattr(threads, "_get", lambda pad, params, token: {"username": "zerohero_one"} if pad == "me" else
                        {"data": [{"id": "r1", "text": "Geldt dit ook voor handbal?", "username": "anna", "timestamp": "2026-11-01T12:00:00+0000",
                                   "permalink": "https://threads.net/t/r1"}]})
    with db.pool.connection() as c:
        c.execute("DELETE FROM jesse_berichten")
        nieuw, meld = luisteren.reacties_ophalen(c)
        assert (nieuw, meld) == (3, [])  # de eigen reactie van zerohero.one telt niet
        rijen = {r["tekst"]: r for r in c.execute("SELECT * FROM jesse_berichten WHERE stroom = 'reacties'")}
    assert rijen["Waar kan ik je boek kopen?"]["platform"] == "instagram" and rijen["Waar kan ik je boek kopen?"]["url"] == "https://instagram.com/p/ig7"
    assert rijen["Geldt dit ook voor handbal?"]["url"] == "https://threads.net/t/r1" and rijen["Mooie aflevering"]["auteur"] == "Kees"
    blok = klant.get("/", headers=KOP).text.split('id="socials"')[1].split("</section>")[0]
    assert "Waar kan ik je boek kopen?" in blok and "Geldt dit ook voor handbal?" in blok
    # Zonder toestemming bij Meta: een duidelijke melding, de rest gaat door.
    monkeypatch.setattr(meta, "_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("(#10) Requires instagram_manage_comments")))
    with db.pool.connection() as c:
        _, meld = luisteren.reacties_ophalen(c)
        assert meld and "instagram_manage_comments" in meld[0]
        c.execute("DELETE FROM jesse_berichten")
