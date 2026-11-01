"""Vera: nieuws uit drie stromen (keepers uit de Doellijn, politiek uit Promonitor, overig uit eigen bronnen).

De feeds, Claude en de werkruimtes zijn nagebootst. De database- en schermtests draaien alleen met TEST_DATABASE_URL.
Draaien in een eigen proces: python -m pytest tests/test_vera.py (zie tests/alles.sh).
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

from app.kern import config  # noqa: E402
from app.vera import nieuws  # noqa: E402

TZ = config.TZ
DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
UTC = dt.timezone.utc


def rss(items):
    nu = dt.datetime.now(UTC)
    regels = "".join(f"""<item><title>{t}</title><link>{l}</link><guid>{l}</guid><description>{d}</description>
                         <pubDate>{(nu - dt.timedelta(hours=u)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>""" for t, l, d, u in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{regels}</channel></rss>'.encode()


def test_feed_lezen_en_dubbel_herkennen():
    berichten = nieuws.lees_feed(rss([("Kabinet &amp; Kamer eens over Woo", "https://nos.nl/1", "<p>De <b>Woo</b> wordt   aangepast.</p>", 1),
                                      ("Zonder link", "javascript:alert(1)", "", 1)]))
    assert len(berichten) == 1
    b = berichten[0]
    assert b["titel"] == "Kabinet & Kamer eens over Woo" and b["samenvatting"] == "De Woo wordt aangepast." and b["datum"].tzinfo
    assert nieuws.sleutel("Kabinet eens over Woo - NOS") == nieuws.sleutel("Kabinet eens over Woo | Tweakers") == "kabinet eens over woo"
    assert nieuws.json_uit('Hier: [{"id": 1, "score": 70}] klaar') == [{"id": 1, "score": 70}]


def test_selectie_en_uitgelicht():
    nu = dt.datetime(2026, 11, 2, 12, tzinfo=TZ)

    def b(soort, i, score, uur=1, gelezen=False):
        return {"soort": soort, "id": i, "titel": f"{soort} {i}", "score": score, "tijd": nu - dt.timedelta(hours=uur), "gelezen": gelezen}
    lijst = [b("overig", 1, 30), b("overig", 2, 70), b("overig", 3, None, 0), b("overig", 4, 90, 3), b("overig", 5, 55)]
    assert [x["id"] for x in nieuws.selectie(lijst)] == [4, 2, 5, 3]  # onder de 40 weg, ongewogen achteraan
    assert [x["id"] for x in nieuws.selectie(lijst, drempel=0, aantal=2)] == [4, 2]
    assert nieuws.selectie(None) is None
    stromen = {"keepers": [b("keepers", 1, 85), b("keepers", 2, 95, 50)],   # 2 is te oud
               "politiek": [b("politiek", 1, 60)],                         # onder de 65
               "overig": [b("overig", 1, 99, 1, gelezen=True), b("overig", 2, 70)]}
    assert [(x["soort"], x["id"]) for x in nieuws.uitgelicht(stromen, nu)] == [("keepers", 1), ("overig", 2)]


@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        yield k


def test_ophalen_wegen_en_pagina(klant, monkeypatch):
    from app.kern import claude, db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        namen = [r["naam"] for r in c.execute("SELECT naam FROM vera_bronnen ORDER BY id")]
        assert "NOS Nieuws" in namen and "Google: informatiemanagement" in namen  # standaardbronnen klaargezet
        c.execute("DELETE FROM vera_berichten")
        c.execute("DELETE FROM vera_gelezen")
        c.execute("UPDATE vera_bronnen SET actief = (naam IN ('NOS Nieuws', 'Tweakers'))")
    feeds = {"https://feeds.nos.nl/nosnieuwsalgemeen": rss([("Nieuwe Archiefwet aangenomen", "https://nos.nl/a", "De Tweede Kamer stemde in.", 2),
                                                            ("Wielrenner wint etappe", "https://nos.nl/b", "Uitslag.", 3),
                                                            ("Heel oud nieuws", "https://nos.nl/c", "", 80)]),
             "https://feeds.feedburner.com/tweakers/mixed": rss([("Nieuwe Archiefwet aangenomen - Tweakers", "https://tweakers.net/x", "", 1)])}
    with db.pool.connection() as c:
        nieuw, fouten = nieuws.ophalen(c, haal=lambda url: feeds[url])
        assert (nieuw, fouten) == (2, [])
        rijen = c.execute("SELECT titel, bron, ook_bij, rubriek FROM vera_berichten ORDER BY id").fetchall()
        assert [(r["titel"], r["ook_bij"]) for r in rijen] == [("Nieuwe Archiefwet aangenomen", "Tweakers"), ("Wielrenner wint etappe", "")]
        assert nieuws.ophalen(c, haal=lambda url: feeds[url])[0] == 0  # tweede keer: niets nieuws
    # Wegen met Claude (nagebootst), binnen het dagplafond.
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    vragen = []

    def nep(c, persona, systeem, berichten, model=None, **kw):
        vragen.append((persona, model))
        import json, re  # pylint: disable=import-outside-toplevel,multiple-imports
        uit = [{"id": int(n), "score": 82 if "Archiefwet" in t else 12, "waarom": "Raakt je vak — archivering." if "Archiefwet" in t else "Sport."}
               for n, t in re.findall(r"^\[(\d+)\] (.+)$", berichten[0]["content"], re.M)]
        return "Hier is de weging: " + json.dumps(uit)
    monkeypatch.setattr(claude, "gesprek", nep)
    with db.pool.connection() as c:
        assert nieuws.wegen(c) == "2 gewogen" and vragen == [("vera", config.CLAUDE_MODEL_LICHT)]
        scores = {r["titel"]: (r["score"], r["waarom"]) for r in c.execute("SELECT titel, score, waarom FROM vera_berichten")}
        assert scores["Nieuwe Archiefwet aangenomen"] == (82, "Raakt je vak, archivering.") and scores["Wielrenner wint etappe"][0] == 12
    # De werkruimtes nagebootst.
    nu = dt.datetime.now(TZ)
    monkeypatch.setattr(nieuws, "keepers", lambda dagen: [
        {"soort": "keepers", "id": 7, "titel": "Bijlow redt penalty in topper", "url": "https://voetbal.nl/k", "bron": "VI", "tijd": nu - dt.timedelta(hours=2),
         "score": 88, "samenvatting": "Feyenoord-keeper beslist de wedstrijd.", "waarom": "", "labels": ["voetbal", "🧤 Justin Bijlow"]},
        {"soort": "keepers", "id": 8, "titel": "Handbalkeeper stopt", "url": "https://hb.nl/k", "bron": "NOS", "tijd": nu - dt.timedelta(hours=5),
         "score": 61, "samenvatting": "", "waarom": "", "labels": ["handbal"]}])
    monkeypatch.setattr(nieuws, "politiek", lambda dagen: (_ for _ in ()).throw(RuntimeError("Promonitor slaapt")))
    pagina = klant.get("/vera", headers=KOP).text
    assert 'href="/vera" class="actief">Nieuws' in pagina
    uitgelicht = pagina.split('class="nuitgelicht"')[1].split("</section>")[0]
    assert "Bijlow redt penalty in topper" in uitgelicht and "Nieuwe Archiefwet aangenomen" in uitgelicht and "Raakt je vak" in uitgelicht
    assert "Kon dit nieuws niet lezen: Promonitor slaapt" in pagina
    assert "Handbalkeeper stopt" in pagina and "Wielrenner wint etappe" not in pagina  # onder de 40: weg
    assert '&alles=1">Nog 1 laten zien</a>' in pagina.split('class="nkolom overig"')[1]
    assert "laten zien" not in pagina.split('class="nkolom keepers"')[1].split("</section>")[0]  # niets verborgen
    assert "Wielrenner wint etappe" in klant.get("/vera?soort=overig&alles=1", headers=KOP).text
    assert "Handbalkeeper stopt" not in klant.get("/vera?soort=overig", headers=KOP).text
    # Doorklikken: naar de bron, en daarna gelezen.
    with db.pool.connection() as c:
        oid = c.execute("SELECT id FROM vera_berichten WHERE titel = 'Nieuwe Archiefwet aangenomen'").fetchone()["id"]
        wid = c.execute("SELECT id FROM vera_berichten WHERE titel = 'Wielrenner wint etappe'").fetchone()["id"]
    r = klant.get(f"/vera/lees/overig/{oid}", headers=KOP, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://nos.nl/a"
    monkeypatch.setattr(nieuws, "url_van", lambda c, soort, bid, lezers_url=None: "https://voetbal.nl/k" if (soort, bid) == ("keepers", 7) else None)
    assert klant.get("/vera/lees/keepers/7", headers=KOP, follow_redirects=False).headers["location"] == "https://voetbal.nl/k"
    assert klant.get("/vera/lees/keepers/99", headers=KOP, follow_redirects=False).status_code == 404
    assert klant.get("/vera/lees/weetniet/1", headers=KOP, follow_redirects=False).status_code == 404
    pagina = klant.get("/vera", headers=KOP).text
    assert "nuitgelicht" not in pagina or "Bijlow redt" not in pagina.split('class="nuitgelicht"')[1].split("</section>")[0]  # gelezen: niet meer uitgelicht
    assert 'class="nitem gelezen"' in pagina
    # Wegklikken.
    assert klant.post(f"/vera/verberg/{wid}", headers=KOP).status_code == 403
    klant.post(f"/vera/verberg/{wid}", headers=FORM, data={"terug": "/vera?soort=overig&alles=1"})
    assert "Wielrenner wint etappe" not in klant.get("/vera?soort=overig&alles=1", headers=KOP).text
    # Vandaag: de koppen.
    vandaag = klant.get("/", headers=KOP).text.split('id="nieuws"')[1].split("</section>")[0]
    assert "Bijlow redt penalty in topper" in vandaag and f'href="/vera/lees/overig/{oid}"' in vandaag
    with db.pool.connection() as c:
        c.execute("DELETE FROM vera_berichten")
        c.execute("DELETE FROM vera_gelezen")


def test_bronnen_beheren(klant):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    pagina = klant.get("/vera/bronnen", headers=KOP).text
    assert "NOS Nieuws" in pagina and "Bron toevoegen" in pagina
    r = klant.post("/vera/bronnen", headers=FORM, follow_redirects=False, data={"naam": "Fout", "url": "ftp://x", "soort": "feed"})
    assert "https" in r.headers["location"] and "fout=" in r.headers["location"]
    klant.post("/vera/bronnen", headers=FORM, data={"naam": "Archiefwet", "url": '"Archiefwet" OR Woo', "soort": "google", "rubriek": "FB/IM"})
    with db.pool.connection() as c:
        b = c.execute("SELECT * FROM vera_bronnen WHERE naam = 'Archiefwet'").fetchone()
        assert b["soort"] == "google" and b["rubriek"] == "FB/IM" and b["actief"]
        assert "news.google.com" in nieuws.feed_url(b) and "when%3A2d" in nieuws.feed_url(b)
    klant.post(f"/vera/bronnen/{b['id']}/uit", headers=FORM)
    with db.pool.connection() as c:
        assert not c.execute("SELECT actief FROM vera_bronnen WHERE id = %s", (b["id"],)).fetchone()["actief"]
    klant.post(f"/vera/bronnen/{b['id']}/weg", headers=FORM)
    with db.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM vera_bronnen WHERE id = %s", (b["id"],)).fetchone()
    assert klant.post("/vera/bronnen/1/sloop", headers=FORM).status_code == 404
