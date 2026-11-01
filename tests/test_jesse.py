"""Jesse luistert mee op de socials: bronnen lezen (Reddit, Mastodon, Bluesky, YouTube), wegen, tonen en Vandaag.

Het web en Claude zijn nagebootst. De database- en schermtests draaien alleen met TEST_DATABASE_URL. De reacties op
je eigen posts (via de Doellijn) staan in test_portal_zerohero.py.
Draaien in een eigen proces: python -m pytest tests/test_jesse.py (zie tests/alles.sh).
"""
import datetime as dt
import json
import os
import re

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.jesse import luisteren  # noqa: E402
from app.kern import config  # noqa: E402

TZ = config.TZ
DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
NU = dt.datetime.now(dt.timezone.utc)


def rss(items):
    regels = "".join(f"""<entry><title>{t}</title><link href="{l}"/><id>{l}</id><author><name>{a}</name></author>
                         <content type="html">{d}</content><updated>{(NU - dt.timedelta(hours=u)).strftime('%Y-%m-%dT%H:%M:%SZ')}</updated></entry>"""
                     for t, l, a, d, u in items)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>{regels}</feed>'.encode()


def bluesky(posts):
    return json.dumps({"posts": [{"uri": f"at://did:plc:x/app.bsky.feed.post/{rkey}", "author": {"handle": h},
                                  "record": {"text": t, "createdAt": (NU - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
                                  "likeCount": 4, "replyCount": 1} for rkey, h, t in posts]}).encode()


def test_adressen():
    assert luisteren.adres({"soort": "reddit", "adres": "Goalkeeping"}) == "https://www.reddit.com/r/Goalkeeping/new/.rss"
    assert luisteren.adres({"soort": "reddit", "adres": "r/hockeygoalies"}) == "https://www.reddit.com/r/hockeygoalies/new/.rss"
    assert "search.rss" in luisteren.adres({"soort": "reddit", "adres": "search:Helmond"}) and "q=Helmond" in luisteren.adres({"soort": "reddit", "adres": "search:Helmond"})
    assert luisteren.adres({"soort": "mastodon", "adres": "mastodon.social/#keeper"}) == "https://mastodon.social/tags/keeper.rss"
    b = luisteren.adres({"soort": "bluesky", "adres": "keeper lang:nl"})
    assert b.startswith("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?") and "q=keeper&" in b and "lang=nl" in b
    assert "type=video" in luisteren.adres({"soort": "youtube", "adres": "doelman"})


def test_lezen():
    r = luisteren.lees_feed(rss([("Best keeper drill?", "https://reddit.com/r/Goalkeeping/1", "/u/jan", "&lt;p&gt;Looking for drills&lt;/p&gt; submitted by /u/jan", 2)]), "reddit")
    assert r[0]["tekst"] == "Best keeper drill?: Looking for drills" and r[0]["auteur"] == "u/jan" and r[0]["tijd"]
    m = luisteren.lees_feed(rss([("", "https://mastodon.social/@a/1", "a", "&lt;p&gt;Wat een redding van #Bijlow&lt;/p&gt;", 1)]), "mastodon")
    assert m[0]["tekst"] == "Wat een redding van #Bijlow"
    b = luisteren.lees_bluesky(bluesky([("3k", "piet.bsky.social", "Sporthal in Helmond weer dicht")]))
    assert b[0]["url"] == "https://bsky.app/profile/piet.bsky.social/post/3k" and b[0]["auteur"] == "@piet.bsky.social" and b[0]["interacties"] == 5
    y = luisteren.lees_youtube(json.dumps({"items": [{"id": {"videoId": "v1"}, "snippet": {"title": "Keeperstraining", "description": "Oefeningen",
                                                                                          "channelTitle": "KNVB", "publishedAt": "2026-11-01T10:00:00Z"}}]}).encode())
    assert y[0]["url"] == "https://www.youtube.com/watch?v=v1" and y[0]["tekst"] == "Keeperstraining: Oefeningen" and y[0]["auteur"] == "KNVB"


def test_selectie():
    def b(i, stroom, score, gelezen=False, titel=None):
        koppen = {1: "Redding van de week in Rotterdam", 2: "Handschoenen te koop maat negen", 3: "Hoe blijf je scherp als er niets gebeurt",
                  4: "IJshockeygoalie breekt clubrecord", 5: "Waar kan ik het boek kopen", 6: "Mooie aflevering van de podcast"}
        return {"id": i, "stroom": stroom, "score": score, "gelezen": gelezen, "titel": titel or koppen[i],
                "bron": "x", "tijd": NU - dt.timedelta(hours=i)}
    lijst = [b(1, "keepers", 90, gelezen=True), b(2, "keepers", 30), b(3, "keepers", 70), b(4, "keepers", None)]
    assert [x["id"] for x in luisteren.selectie(lijst)] == [3, 4, 1]  # ongelezen eerst, onder de 40 weg
    assert [x["id"] for x in luisteren.selectie([b(5, "reacties", 10), b(6, "reacties", 5)])] == [5, 6]  # reacties vallen nooit weg


@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        yield k


def test_luisteren_wegen_tonen(klant, monkeypatch):
    from app.kern import claude, db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        namen = {r["naam"]: r for r in c.execute("SELECT * FROM jesse_bronnen")}
        assert namen["Reddit r/Goalkeeping"]["stroom"] == "keepers" and namen["Bluesky: Helmond"]["stroom"] == "politiek"  # klaargezet
        c.execute("DELETE FROM jesse_berichten")
        c.execute("DELETE FROM jesse_gelezen")
        c.execute("UPDATE jesse_bronnen SET actief = (naam IN ('Reddit r/Goalkeeping', 'Bluesky: Helmond', 'Mastodon #goalkeeper'))")
    web = {"https://www.reddit.com/r/Goalkeeping/new/.rss": rss([("How do you stay focused when nothing happens for 80 minutes?", "https://reddit.com/1", "/u/a", "", 1),
                                                                 ("Selling gloves size 9", "https://reddit.com/2", "/u/b", "", 2),
                                                                 ("Heel oud bericht", "https://reddit.com/3", "/u/c", "", 100)]),
           "https://mastodon.social/tags/goalkeeper.rss": rss([("", "https://mastodon.social/@x/9", "x", "How do you stay focused when nothing happens for 80 minutes?", 1)])}

    def haal(url, kop=None):
        if url.startswith("https://public.api.bsky.app"):
            return bluesky([("3k", "piet.bsky.social", "Waarom gaat de sporthal in Helmond-Noord weer dicht? Belachelijk.")])
        if url not in web:
            raise OSError("HTTP Error 403: Blocked")
        return web[url]
    with db.pool.connection() as c:
        nieuw, fouten = luisteren.ophalen(c, haal)
        assert (nieuw, fouten) == (3, [])  # oud bericht en het dubbele Mastodon-bericht vallen weg
        assert luisteren.ophalen(c, haal)[0] == 0
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    gezien = []

    def nep(c, persona, systeem, berichten, model=None, **kw):
        gezien.append((persona, "raadswerk" in systeem, model))
        regels = re.findall(r"^\[(\d+)\] [^\n]*\n(.+)$", berichten[0]["content"], re.M)
        return json.dumps([{"id": int(n), "score": 20 if "Selling" in t else 80, "toon": "kritisch" if "Belachelijk" in t else "neutraal",
                            "waarom": "Goede haak voor een verhaal — over focus." if "focused" in t else "Inwoners boos over de sporthal."} for n, t in regels])
    monkeypatch.setattr(claude, "gesprek", nep)
    with db.pool.connection() as c:
        assert luisteren.wegen(c) == "3 gewogen"
    assert [x[0] for x in gezien] == ["jesse", "jesse"] and [x[1] for x in gezien] == [False, True] and gezien[0][2] == config.CLAUDE_MODEL_LICHT
    pagina = klant.get("/jesse", headers=KOP).text
    assert 'href="/jesse" class="actief">Socials' in pagina
    assert "How do you stay focused" in pagina and "Goede haak voor een verhaal, over focus." in pagina and "Selling gloves" not in pagina
    assert "Waarom gaat de sporthal in Helmond-Noord weer dicht?" in pagina and 'class="jtoon kritisch"' in pagina
    assert "Geen nieuwe reacties op je posts." in pagina and "Nog 1 laten zien" in pagina
    with db.pool.connection() as c:
        bid = c.execute("SELECT id FROM jesse_berichten WHERE tekst LIKE 'Waarom gaat de sporthal%'").fetchone()["id"]
        sid = c.execute("SELECT id FROM jesse_berichten WHERE tekst LIKE 'Selling%'").fetchone()["id"]
    r = klant.get(f"/jesse/lees/{bid}", headers=KOP, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://bsky.app/profile/piet.bsky.social/post/3k"
    assert 'class="nitem gelezen"' in klant.get("/jesse?stroom=politiek", headers=KOP).text
    assert klant.post(f"/jesse/gelezen/{bid}", headers=FORM | {"X-Jesse": "1"}, data={"aan": "0"}).json() == {"gelezen": False}
    assert klant.post(f"/jesse/verberg/{sid}", headers=KOP).status_code == 403
    klant.post(f"/jesse/verberg/{sid}", headers=FORM, data={"terug": "/jesse"})
    assert "Selling gloves" not in klant.get("/jesse?stroom=keepers&alles=1", headers=KOP).text
    # Vandaag: Socials over de volle breedte, per stroom, klikbaar.
    blok = klant.get("/", headers=KOP).text.split('id="socials"')[1].split("</section>")[0]
    assert "Reacties op jou" in blok and "Keepers" in blok and "Helmond en politiek" in blok
    assert f'href="/jesse/lees/{bid}"' in blok and "kritisch" in blok
    with db.pool.connection() as c:
        c.execute("UPDATE jesse_bronnen SET actief = true")
        c.execute("DELETE FROM jesse_berichten")


def test_bronnen_beheren(klant):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    pagina = klant.get("/jesse/bronnen", headers=KOP).text
    assert "Reddit r/Goalkeeping" in pagina and "Reacties op je eigen posts" in pagina and "LinkedIn en X" in pagina
    assert "fout=" in klant.post("/jesse/bronnen", headers=FORM, follow_redirects=False, data={"naam": "x", "adres": "y", "soort": "tiktok", "stroom": "keepers"}).headers["location"]
    klant.post("/jesse/bronnen", headers=FORM, data={"naam": "Reddit r/handball", "adres": "handball", "soort": "reddit", "stroom": "keepers"})
    with db.pool.connection() as c:
        b = c.execute("SELECT * FROM jesse_bronnen WHERE naam = 'Reddit r/handball'").fetchone()
        assert b["soort"] == "reddit" and b["stroom"] == "keepers"
    klant.post(f"/jesse/bronnen/{b['id']}/uit", headers=FORM)
    klant.post(f"/jesse/bronnen/{b['id']}/weg", headers=FORM)
    with db.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM jesse_bronnen WHERE id = %s", (b["id"],)).fetchone()
