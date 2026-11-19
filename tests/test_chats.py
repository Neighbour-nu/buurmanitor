"""WhatsApp (export inlezen) en Teams (chats lezen) naast Signal: ontleden, inlezen zonder dubbelingen, voorstellen,
het scherm, de regel in Post en het koppelen van Teams. Microsoft wordt nagebootst. De database-tests draaien alleen met
TEST_DATABASE_URL. Draaien in een eigen proces (zie tests/alles.sh).
"""
import datetime as dt
import io
import json
import os
import urllib.parse
import zipfile

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.voorbeeld.nl"
os.environ["TOEGESTANE_HOSTS"] = "buurmanitor.voorbeeld.nl"
os.environ["SIGNAL_URL"] = ""
os.environ["MS_CLIENT_ID"] = "ms-app"
os.environ["MS_CLIENT_SECRET"] = "ms-geheim"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.kern import config  # noqa: E402
from app.signaal import teams, verwerken, whatsapp  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.voorbeeld.nl"}
NU = dt.datetime.now(config.TZ).replace(second=0, microsecond=0)


def export(regels):
    """Een export zoals de iPhone hem maakt, met tijden ten opzichte van nu."""
    return "\n".join(f"‎[{(NU + dt.timedelta(minutes=m)):%d-%m-%Y %H:%M:%S}] {t}" for m, t in regels)


def als_zip(naam, tekst):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("_chat.txt", tekst)
    return naam, b.getvalue()


# ---------------------------------------------------------------- WhatsApp zonder database

def test_whatsapp_ontleden():
    t = ("[19-11-2026 15:26:01] Anna: Kun jij donderdag de notulen sturen?\nGraag voor 20.00\n"
         "[19-11-2026 15:27:12] Piet Jansen: Doe ik\n"
         "‎[19-11-2026 15:28:00] Kees: ‎afbeelding weggelaten\n"
         "[19-11-2026 15:29:00] Berichten en oproepen zijn end-to-end versleuteld.\n"
         "[19-11-2026 15:30:00] Kees: Dit bericht is verwijderd\n"
         "[20-11-2026 09:00:00] Anna: Top")
    b = whatsapp.ontleed(t, ["piet jansen"])
    assert [(x["van"], x["tekst"]) for x in b] == [("Anna", "Kun jij donderdag de notulen sturen?\nGraag voor 20.00"), ("jij", "Doe ik"),
                                                  ("Kees", "[bijlage]"), ("Anna", "Top")]
    assert b[1]["van_mij"] and b[0]["tijd"] == dt.datetime(2026, 11, 19, 15, 26, 1, tzinfo=config.TZ)
    # Android, en Amerikaanse datums met AM/PM
    a = whatsapp.ontleed("19-11-2026 15:26 - Anna: Android\n2026-11-19 15:27 - Bob: iso")
    assert [x["tijd"].minute for x in a] == [26, 27]
    us = whatsapp.ontleed("11/19/26, 3:26 PM - Anna: hoi\n11/20/26, 12:05 AM - You: <Media omitted>", ["You"])
    assert us[0]["tijd"] == dt.datetime(2026, 11, 19, 15, 26, tzinfo=config.TZ) and us[1]["tijd"].hour == 0 and us[1]["tekst"] == "[bijlage]"
    # dezelfde minuut, dezelfde afzender: toch twee berichten, en morgen weer dezelfde sleutel
    r = whatsapp.naar_berichten("Anna", whatsapp.ontleed("19-11-2026 15:26 - Anna: een\n19-11-2026 15:26 - Anna: twee"))
    assert r[1]["ts"] == r[0]["ts"] + 1 and r[0]["sleutel"] == "whatsapp:anna" and r[0]["soort"] == "persoon" and r[0]["archief_standaard"]
    assert whatsapp.naar_berichten("Fractie", b)[0]["soort"] == "groep"


def test_whatsapp_bestanden():
    assert whatsapp.chatnaam_uit_bestand("WhatsApp Chat - Fractie PRO.zip") == "Fractie PRO"
    assert whatsapp.chatnaam_uit_bestand("WhatsApp-chat met Anna.txt") == "Anna"
    assert whatsapp.chatnaam_uit_bestand("WhatsApp Chat with Bob (1).zip") == "Bob"
    assert whatsapp.chatnaam_uit_bestand("_chat.txt") == ""
    naam, tekst = whatsapp.lees_bestand(*als_zip("WhatsApp Chat - Vereniging.zip", "[19-11-2026 15:26:01] Anna: hoi"))
    assert naam == "Vereniging" and "Anna: hoi" in tekst
    assert whatsapp.lees_bestand("x.txt", "[19-11-2026 15:26:01] Anna: hoi".encode("utf-16"))[1].startswith("[19-11")
    with pytest.raises(whatsapp.Fout):
        whatsapp.lees_bestand("leeg.zip", b"PK\x03\x04kapot")


# ---------------------------------------------------------------- Teams zonder database

def test_teams_vertalen():
    assert teams.platte_tekst("<p>Hoi&nbsp;<b>Piet</b></p><p>Kun je dit lezen?</p>") == "Hoi Piet\nKun je dit lezen?"
    assert teams.platte_tekst('<attachment id="1"></attachment>', "html") == "[bijlage]"
    ch = {"id": "19:abc@thread.v2", "chatType": "group", "topic": "", "members": [
        {"userId": "ik", "displayName": "Ik"}, {"userId": "a", "displayName": "Anna"}, {"userId": "b", "displayName": "Bob"}]}
    assert teams.chatnaam(ch, "ik") == "Anna, Bob" and teams.chatnaam(ch | {"topic": "Projectoverleg"}, "ik") == "Projectoverleg"
    m = {"id": "1763560000000", "messageType": "message", "createdDateTime": "2026-11-19T14:26:01.1234567Z",
         "from": {"user": {"id": "a", "displayName": "Anna"}}, "body": {"contentType": "html", "content": "<p>Sturen jullie de offerte?</p>"}}
    b = teams.naar_bericht(m, ch, "Anna, Bob", "ik")
    assert b["ts"] == 1763560000000 and b["van"] == "Anna" and not b["van_mij"] and b["sleutel"] == "teams:19:abc@thread.v2"
    assert b["tijd"] == dt.datetime(2026, 11, 19, 14, 26, 1, tzinfo=dt.timezone.utc) and b["soort"] == "groep" and b["bron"] == "teams"
    assert teams.naar_bericht(m | {"from": {"user": {"id": "ik", "displayName": "Ik"}}}, ch, "x", "ik")["van"] == "jij"
    assert teams.naar_bericht(m | {"messageType": "systemEventMessage"}, ch, "x") is None
    assert teams.naar_bericht(m | {"deletedDateTime": "2026-11-19T15:00:00Z"}, ch, "x") is None
    assert teams.naar_bericht(m | {"body": {"content": ""}}, ch, "x") is None


# ---------------------------------------------------------------- met database

@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        yield k


def _db():
    from app.kern import db  # pylint: disable=import-outside-toplevel
    return db.pool.connection()


@pytest.fixture
def schoon(klant):
    with _db() as c:
        c.execute("DELETE FROM signal_chats")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'whatsapp_%%' OR sleutel LIKE 'teams_%%'")
    yield klant
    with _db() as c:
        c.execute("DELETE FROM signal_chats")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'whatsapp_%%' OR sleutel LIKE 'teams_%%'")


def test_whatsapp_inlezen_en_voorstellen(schoon, monkeypatch):
    oud = [(-60 * 24 * 40, "Anna: Lang geleden: kun jij de zaal boeken?")]
    vers = [(-90, "Anna: Kun jij donderdag de notulen sturen?"), (-80, "Piet Jansen: Doe ik"), (-70, "Kees: Ik neem koffie mee")]
    r = schoon.post("/bram/whatsapp/inlezen", headers=FORM, follow_redirects=True, data={"eigen": "Piet Jansen"},
                    files=[("bestanden", als_zip("WhatsApp Chat - Fractie PRO.zip", export(oud + vers)))])
    assert "Fractie PRO: 4 berichten in het bestand, 4 nieuw (nieuwe chat, in het archief)" in r.text
    with _db() as c:
        ch = c.execute("SELECT * FROM signal_chats").fetchone()
        assert ch["bron"] == "whatsapp" and ch["archief"] and not ch["meelezen"] and ch["soort"] == "groep" and ch["aantal"] == 4
        assert verwerken.instelling(c, "whatsapp_naam") == "Piet Jansen"
        assert c.execute("SELECT count(*) AS n FROM signal_berichten WHERE van = 'jij' AND van_mij").fetchone()["n"] == 1
    assert "WhatsApp" in r.text and 'class="bronlabel bron-whatsapp"' in r.text

    # morgen dezelfde export plus één nieuw bericht: alleen dat ene erbij
    schoon.post(f"/bram/signal/chat/{ch['id']}", headers=FORM, data={"meelezen": "1", "archief": "1", "voorstellen": "1", "domein": "politiek"})
    meer = vers + [(-30, "Anna: En reserveer jij ook de zaal voor vrijdag?")]
    r = schoon.post("/bram/whatsapp/inlezen", headers=FORM, follow_redirects=True,
                    files=[("bestanden", ("WhatsApp-chat met Fractie PRO.txt", export(oud + meer).encode()))])
    assert "5 berichten in het bestand, 1 nieuw" in r.text
    with _db() as c:
        assert c.execute("SELECT aantal FROM signal_chats WHERE id = %s", (ch["id"],)).fetchone()["aantal"] == 5

    # Bram maakt voorstellen uit het verse deel (het oude deel en wat er voor het meelezen al was, niet)
    gevraagd = []
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(verwerken.claude, "gesprek", lambda c, p, s, berichten, **kw: gevraagd.append((s, berichten[-1]["content"])) or json.dumps(
        {"voorstellen": [{"soort": "taak", "titel": "Zaal reserveren voor vrijdag", "toelichting": "Anna vroeg het"}]}))
    with _db() as c:
        assert verwerken.taak_chatanalyse(c) == "1 voorstellen"
        systeem, vraag = gevraagd[0]
        nieuw = vraag.split("# Nieuwe berichten")[1]
        assert "WhatsApp-chat: Fractie PRO" in vraag and "reserveer jij ook de zaal" in nieuw and "Lang geleden" not in nieuw
        assert "notulen" not in nieuw  # dat was er al voordat meelezen aan ging
        assert "(Signal, WhatsApp of Teams)" in systeem
        t = verwerken.samenvatting(c, dt.datetime.now(config.TZ) - dt.timedelta(hours=24))
        assert f"WhatsApp-chat #{ch['id']}: Fractie PRO" in t and "Lang geleden" not in t

    # verwijderde chat: opnieuw inlezen doet niets
    schoon.post(f"/bram/signal/chat/{ch['id']}/verwijderen", headers=FORM)
    r = schoon.post("/bram/whatsapp/inlezen", headers=FORM, follow_redirects=True,
                    files=[("bestanden", ("WhatsApp-chat met Fractie PRO.txt", export(meer).encode()))])
    assert "deze chat had je verwijderd" in r.text
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM signal_berichten").fetchone()["n"] == 0

    # fouten
    r = schoon.post("/bram/whatsapp/inlezen", headers=FORM, follow_redirects=True, files=[("bestanden", ("iets.txt", b"geen chat hier"))])
    assert "vond ik geen WhatsApp-berichten" in r.text
    r = schoon.post("/bram/whatsapp/inlezen", headers=FORM, follow_redirects=True, files=[("bestanden", ("_chat.txt", export(vers).encode()))])
    assert "kan ik de naam van de chat niet halen" in r.text
    r = schoon.post("/bram/whatsapp/inlezen", headers=FORM, follow_redirects=True, data={"chatnaam": "Buren"},
                    files=[("bestanden", ("_chat.txt", export(vers).encode()))])
    assert "Buren: 3 berichten" in r.text

    # alles van WhatsApp wissen
    schoon.post("/bram/signal/vergeten", headers=FORM, data={"bron": "whatsapp"})
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM signal_chats WHERE bron = 'whatsapp'").fetchone()["n"] == 0


def test_whatsapp_via_regel(schoon):
    from app.bram import regels  # pylint: disable=import-outside-toplevel
    v, a, fout = regels.uit_formulier({"onderwerp": "WhatsApp", "whatsapp": "1", "archiveren": "1"})
    assert not fout and a["whatsapp"] and "WhatsApp-export inlezen" in regels.beschrijf(v, a)[1] and regels.heeft_bijlage_acties(a)
    naam, data = als_zip("WhatsApp Chat - Buren.zip", export([(-10, "Anna: Kun jij de container buitenzetten?")]))
    with _db() as c:
        uit = regels.whatsapp_inlezen(c, [{"naam": naam, "soort": "application/zip", "grootte": len(data), "data": data},
                                          {"naam": "logo.png", "soort": "image/png", "grootte": 10, "data": b"x"}])
        assert uit.startswith("WhatsApp ingelezen: Buren: 1 berichten in het bestand, 1 nieuw")
        with pytest.raises(regels.RegelFout):
            regels.whatsapp_inlezen(c, [{"naam": "factuur.pdf", "soort": "application/pdf", "grootte": 1, "data": b"%PDF"}])


class NepGraph:
    def __init__(self):
        self.chats, self.berichten = [], {}

    def __call__(self, c, pad):
        if pad.startswith("/me?"):
            return {"id": "ik"}
        if pad.startswith("/me/chats"):
            return {"value": self.chats}
        cid = urllib.parse.unquote(pad.split("/")[2].split("?")[0])
        return {"value": list(reversed(self.berichten.get(cid, [])))}


def test_teams(schoon, monkeypatch):
    from app.clara import web as clara_web  # pylint: disable=import-outside-toplevel
    # koppelen: naar Microsoft met alleen Chat.Read, en terug via het gewone terugkeeradres
    r = schoon.post("/bram/teams/koppel", headers=FORM, follow_redirects=False)
    assert r.status_code == 303 and "Chat.Read" in urllib.parse.unquote(r.headers["location"]) and "Mail" not in r.headers["location"]
    staat = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["state"][0]
    g = NepGraph()
    monkeypatch.setattr(teams, "koppel_code", lambda code: {"refresh": "ververs", "account": "piet@werk.example", "tenant": "t1"})
    monkeypatch.setattr(teams, "_vraag", g)
    r = schoon.get(f"/clara/koppel/microsoft/terug?code=abc&state={staat}", headers=KOP, follow_redirects=True)
    assert "Teams gekoppeld (piet@werk.example)" in r.text and "Teams: gekoppeld met piet@werk.example" in r.text
    with _db() as c:
        assert verwerken.instelling(c, "teams_geheim") not in ("", "ververs")  # versleuteld bewaard

    # nieuwe berichten: chat erbij, tekst pas na meelezen of archief
    tijd = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    ch = {"id": "19:x@unq.gbl.spaces", "chatType": "oneOnOne", "topic": None, "lastMessagePreview": {"createdDateTime": tijd},
          "members": [{"userId": "ik", "displayName": "Piet"}, {"userId": "a", "displayName": "Anna de Vries"}]}
    m = {"id": "1763560000001", "messageType": "message", "createdDateTime": tijd, "from": {"user": {"id": "a", "displayName": "Anna de Vries"}},
         "body": {"contentType": "html", "content": "<p>Kun je de offerte vrijdag sturen?</p>"}}
    g.chats, g.berichten = [ch], {ch["id"]: [m]}
    with _db() as c:
        assert teams.taak_teams(c) == "1 berichten, 0 bewaard, 1 nieuwe chats"
        rij = c.execute("SELECT * FROM signal_chats WHERE bron = 'teams'").fetchone()
        assert rij["naam"] == "Anna de Vries" and rij["soort"] == "persoon"
        c.execute("UPDATE signal_chats SET meelezen = true WHERE id = %s", (rij["id"],))
        c.execute("DELETE FROM instellingen WHERE sleutel = 'teams_sinds'")
        assert teams.taak_teams(c) == "1 berichten, 1 bewaard"
        assert teams.taak_teams(c).endswith("0 bewaard")  # al binnen: niet dubbel
    r = schoon.get("/bram/signal?bron=teams", headers=KOP)
    assert "Anna de Vries" in r.text and 'class="bronlabel bron-teams"' in r.text and 'class="filter aan"' in r.text
    assert "offerte vrijdag" in schoon.get(f"/bram/signal/chat/{rij['id']}", headers=KOP).text

    # ontkoppelen: alles van Teams weg
    schoon.post("/bram/teams/ontkoppel", headers=FORM)
    with _db() as c:
        assert not verwerken.instelling(c, "teams_geheim") and not c.execute("SELECT 1 FROM signal_chats WHERE bron = 'teams'").fetchone()
        assert teams.taak_teams(c) == "Teams is niet gekoppeld"
    assert "Teams koppelen" in schoon.get("/bram/signal", headers=KOP).text
    assert clara_web  # (het terugkeeradres is van Clara)


def test_scherm_zonder_signal(schoon):
    r = schoon.get("/bram/signal", headers=KOP)
    assert r.status_code == 200 and "<h1>Chats</h1>" in r.text and "Signal staat nog niet aan" in r.text
    assert "WhatsApp inlezen" in r.text and "Teams koppelen" in r.text and "Voorstellen" in r.text
    assert "💬 Chats" in schoon.get("/bram", headers=KOP).text
