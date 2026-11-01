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


def test_koppen_per_stroom():
    nu = dt.datetime(2026, 11, 2, 12, tzinfo=TZ)

    def b(soort, i, score, uur=1, gelezen=False):
        return {"soort": soort, "id": i, "titel": f"{soort} {i}", "score": score, "tijd": nu - dt.timedelta(hours=uur), "gelezen": gelezen}
    stromen = {"keepers": [b("keepers", 1, 70), b("keepers", 2, 90, gelezen=True), b("keepers", 3, 80), b("keepers", 4, 60), b("keepers", 5, 99, 40)],
               "politiek": None,
               "overig": [b("overig", 1, 20), b("overig", 2, None)]}
    k = nieuws.koppen_per_soort(stromen, nu)
    assert [x["id"] for x in k["keepers"]] == [3, 1, 4]  # ongelezen eerst, op belang; te oud valt weg
    assert k["politiek"] is None and [x["id"] for x in k["overig"]] == [2]


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
    # Vandaag: de koppen per stroom; geen blokken Team en Server meer.
    hele = klant.get("/", headers=KOP).text
    vandaag = hele.split('id="nieuws"')[1].split("</section>")[0]
    assert "Bijlow redt penalty in topper" in vandaag and f'href="/vera/lees/overig/{oid}"' in vandaag
    assert vandaag.index("Keepersnieuws") < vandaag.index("Bijlow") < vandaag.index("Politiek nieuws") < vandaag.index("Overig nieuws")
    assert "Promonitor draait hier nog niet" not in vandaag  # Promonitor gaf een fout, maar is er wel
    assert "Het team" not in hele and "Geheugen" not in hele and "<h2>Server" not in hele
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


# ---------------------------------------------------------------- gesprek over bronnen

def test_vera_voorstellen_lezen():
    from app.vera import gesprek  # pylint: disable=import-outside-toplevel
    tekst = ('Ik stel twee bronnen voor.\n'
             '<bron>{"naam": "Computable", "soort": "site", "url": "https://www.computable.nl", "rubriek": "FB/IM"}</bron>\n'
             '<bron>{"naam": "Archiefwet", "soort": "google", "url": "\\"Archiefwet\\" OR Woo"}</bron>\n'
             '<bron>{"naam": "Kapot", "soort": "feed", "url": "javascript:alert(1)"}</bron>\n'     # geen http: vervalt
             '<bron>{"naam": "Raar", "soort": "podcast", "url": "https://x.nl"}</bron>\n'         # onbekende soort: vervalt
             '<bronactie>{"id": 3, "actie": "uit"}</bronactie>\n'
             '<bronactie>{"id": 99, "actie": "weg"}</bronactie>\n'                                 # onbekende bron: vervalt
             '<wijzig>{"id": 3, "url": "nieuwe zoekopdracht"}</wijzig>')
    rest, v = gesprek.voorstellen_uit(tekst, {3})
    assert rest == "Ik stel twee bronnen voor."
    assert [(x["soort"], x["bron_id"]) for x in v] == [("nieuw", None), ("nieuw", None), ("uit", 3), ("wijzig", 3)]
    assert v[1]["gegevens"] == {"naam": "Archiefwet", "soort": "google", "url": '"Archiefwet" OR Woo', "rubriek": "Algemeen"}
    assert gesprek.beschrijf(v[0]) == "Nieuwe bron: Computable (FB/IM) · https://www.computable.nl"
    assert gesprek.beschrijf(v[2], {"naam": "Tweakers"}) == "Tweakers uitzetten"


def test_feed_zoeken_en_testen():
    from app.vera import gesprek  # pylint: disable=import-outside-toplevel
    voer = rss([("Archiefwet aangenomen", "https://computable.nl/a", "", 1)])
    paginas = {"https://www.computable.nl": b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed/nieuws"></head></html>',
               "https://www.computable.nl/feed/nieuws": voer,
               "https://zonderlink.nl": b"<html>geen feed hier</html>", "https://zonderlink.nl/rss": voer}

    def haal(url):
        if url not in paginas:
            raise OSError("404")
        return paginas[url]
    g = gesprek.test_bron({"naam": "Computable", "soort": "site", "url": "https://www.computable.nl"}, haal)
    assert g["werkt"] and g["soort"] == "feed" and g["url"] == "https://www.computable.nl/feed/nieuws" and g["site"] == "https://www.computable.nl"
    assert g["aantal"] == 1 and g["voorbeeld"] == "Archiefwet aangenomen"
    assert gesprek.test_bron({"soort": "site", "url": "https://zonderlink.nl"}, haal)["url"] == "https://zonderlink.nl/rss"
    fout = gesprek.test_bron({"soort": "feed", "url": "https://bestaatniet.nl/rss"}, haal)
    assert not fout["werkt"] and fout["fout"] == "404"


def test_gesprek_bronnen_toevoegen(klant, monkeypatch):
    from app.kern import claude, db  # pylint: disable=import-outside-toplevel
    from app.vera import gesprek  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        tid = c.execute("SELECT id FROM vera_bronnen WHERE naam = 'Tweakers'").fetchone()["id"]
    gezien = {}

    def nep(c, persona, systeem, berichten, **kw):
        gezien.update(persona=persona, systeem=systeem, vraag=berichten[-1]["content"])
        return ("Computable heeft een feed; voor de Archiefwet werkt een zoekopdracht beter. Tweakers zet ik voor je klaar om uit te zetten.\n"
                '<bron>{"naam": "Computable", "soort": "site", "url": "https://www.computable.nl", "rubriek": "FB/IM"}</bron>\n'
                '<bron>{"naam": "Bestaat niet", "soort": "feed", "url": "https://bestaatniet.nl/rss", "rubriek": "FB/IM"}</bron>\n'
                f'<bronactie>{{"id": {tid}, "actie": "uit"}}</bronactie>')
    monkeypatch.setattr(claude, "gesprek", nep)
    voer = rss([("Archiefwet aangenomen", "https://computable.nl/a", "", 1)])
    paginas = {"https://www.computable.nl": b'<link rel="alternate" type="application/rss+xml" href="https://www.computable.nl/rss">',
               "https://www.computable.nl/rss": voer}
    monkeypatch.setattr(gesprek, "_haal_kort", lambda url: paginas[url] if url in paginas else (_ for _ in ()).throw(OSError("niet gevonden")))
    pagina = klant.get("/vera/bronnen", headers=KOP).text
    assert 'id="vera"' in pagina and "Vraag het Vera" in pagina
    r = klant.post("/vera/gesprek/vraag", headers=FORM, follow_redirects=False, data={"vraag": "Voeg Computable toe en zet Tweakers uit"})
    assert r.status_code == 303 and "#vera" in r.headers["location"]
    assert gezien["persona"] == "vera" and f"[id {tid}] Tweakers" in gezien["systeem"] and "Voeg Computable toe" in gezien["vraag"]
    pagina = klant.get(r.headers["location"], headers=KOP).text
    assert "Computable heeft een feed" in pagina
    assert "✓ Werkt: 1 bericht nu" in pagina and "Archiefwet aangenomen" in pagina and "feed gevonden op https://www.computable.nl" in pagina
    assert "⚠ Werkt niet: niet gevonden" in pagina and "Toch toevoegen" in pagina and "Tweakers uitzetten" in pagina
    with db.pool.connection() as c:
        vs = c.execute("SELECT * FROM vera_voorstellen ORDER BY id").fetchall()
        assert [v["soort"] for v in vs] == ["nieuw", "nieuw", "uit"]
        assert not c.execute("SELECT 1 FROM vera_bronnen WHERE naam = 'Computable'").fetchone()  # nog niets veranderd
    # Toevoegen (met het gevonden feedadres), niet doen, en uitzetten.
    r = klant.post(f"/vera/voorstel/{vs[0]['id']}", headers=FORM, data={"actie": "doe"}, follow_redirects=False)
    assert "toegevoegd" in r.headers["location"]
    klant.post(f"/vera/voorstel/{vs[1]['id']}", headers=FORM, data={"actie": "weg"})
    klant.post(f"/vera/voorstel/{vs[2]['id']}", headers=FORM, data={"actie": "doe"})
    with db.pool.connection() as c:
        b = c.execute("SELECT * FROM vera_bronnen WHERE naam = 'Computable'").fetchone()
        assert b["soort"] == "feed" and b["url"] == "https://www.computable.nl/rss" and b["rubriek"] == "FB/IM" and b["actief"]
        assert not c.execute("SELECT 1 FROM vera_bronnen WHERE naam = 'Bestaat niet'").fetchone()
        assert not c.execute("SELECT actief FROM vera_bronnen WHERE id = %s", (tid,)).fetchone()["actief"]
        assert [v["status"] for v in c.execute("SELECT status FROM vera_voorstellen ORDER BY id")] == ["uitgevoerd", "afgewezen", "uitgevoerd"]
        c.execute("DELETE FROM vera_bronnen WHERE naam = 'Computable'")
        c.execute("UPDATE vera_bronnen SET actief = true WHERE id = %s", (tid,))
        c.execute("DELETE FROM gesprekken WHERE persona = 'vera'")
    assert "al+afgehandeld" in klant.post(f"/vera/voorstel/{vs[0]['id']}", headers=FORM, data={"actie": "doe"}, follow_redirects=False).headers["location"]
