"""Keepersnieuws: voorstellen uit het gewogen nieuws, beeld, Telegram-knoppen, plaatsen in WordPress en leren.

Claude, de beeldmaker, Telegram en WordPress worden nagebootst. De database-tests draaien alleen met
TEST_DATABASE_URL en TEST_ZEROHERO_DATABASE_URL. Draaien in een eigen proces (zie tests/alles.sh).
"""
import io
import json
import os
import pathlib

BM, ZH = os.environ.get("TEST_DATABASE_URL"), os.environ.get("TEST_ZEROHERO_DATABASE_URL")
os.environ["DATABASE_URL"] = BM or "postgresql://niemand@localhost/niets"
os.environ["ZEROHERO_DATABASE_URL"] = ZH or "postgresql://niemand@localhost/niets"
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["ZEROHERO_ACHTERGROND"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"
os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
os.environ["TELEGRAM_CHAT_ID"] = "4242"
os.environ["ZEROHERO_ANTHROPIC_API_KEY"] = "sk-test"
os.environ["ZEROHERO_WP_USER"] = "wp"
os.environ["ZEROHERO_CONCEPT_SECRET"] = "conceptgeheim"
os.environ["ZEROHERO_WP_APP_PASSWORD"] = "geheim"
os.environ.pop("ZEROHERO_OPENAI_API_KEY", None)
os.environ.pop("ZEROHERO_GEMINI_API_KEY", None)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app.kern import telegram  # noqa: E402
from app.zerohero import main as _zh  # noqa: E402,F401  (eerst main: die laadt keepersnieuws)
from app.zerohero import ai_beeld, keepersnieuws as kn  # noqa: E402

DB = bool(BM and ZH)
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}

ANTWOORD = {"geschikt": True, "oordeel": "Officiële transfer van een Nederlandse keeper.", "categorie": "transfer",
            "titel": "Bart Verbruggen tekent bij Ajax — voor vier jaar", "intro": "De keeper keert terug naar Nederland.",
            "tekst": "Ajax heeft Bart Verbruggen vastgelegd, meldt de club.\n\nHij tekent voor vier jaar.", "kernzin": "Terug in Nederland",
            "keepers": ["Bart Verbruggen"], "sport": "voetbal", "beeld_scene": "an empty goal in a stadium at night",
            "bronnen": [{"titel": "Ajax.nl", "url": "https://www.ajax.nl/nieuws"}, {"titel": "kwaad", "url": "javascript:alert(1)"}]}


# ---------------------------------------------------------------- zonder database

def test_schoon_antwoord():
    a = kn.schoon_antwoord(ANTWOORD | {"categorie": "gerucht"})
    assert a["geschikt"] and a["categorie"] == "groot" and "—" not in a["titel"] and a["titel"] == "Bart Verbruggen tekent bij Ajax, voor vier jaar"
    assert a["bronnen"] == [{"titel": "Ajax.nl", "url": "https://www.ajax.nl/nieuws"}]
    assert not kn.schoon_antwoord(ANTWOORD | {"tekst": ""})["geschikt"]
    assert not kn.schoon_antwoord(ANTWOORD | {"geschikt": False})["geschikt"]
    with pytest.raises(ValueError):
        kn.schoon_antwoord([1])
    m = kn.schoon_antwoord(ANTWOORD | {"categorie": "memoriam", "titel": "Oud-keeper Jan Jansen (78) overleden"})
    assert m["geschikt"] and m["categorie"] == "memoriam" and m["gevoelig"]  # overlijden is altijd gevoelig
    assert not kn.schoon_antwoord(ANTWOORD)["gevoelig"] and kn.schoon_antwoord(ANTWOORD | {"gevoelig": True})["gevoelig"]
    assert "Het overlijden van een bekende (oud-)keeper hoort er altijd bij" in kn.VOORSELECTIE and "memoriam" in kn.SYSTEEM
    assert "113" in kn.SYSTEEM and "niet zelf schrijven" not in kn.SYSTEEM


def test_json_met_meer_objecten():
    from app.zerohero import claude_api  # pylint: disable=import-outside-toplevel
    twee = '{\n  "geschikt": false,\n  "categorie": "groot",\n  "oordeel": "Bericht 1"\n}\n\n{\n  "geschikt": true,\n  "categorie": "transfer",\n  "oordeel": "Bericht 2"\n}'
    assert claude_api.json_uit(twee)["oordeel"] == "Bericht 1"  # vroeger: "Extra data"
    assert kn.samenvoegen_selectie(claude_api.json_uit(twee, alle=True)) == (True, "transfer", "Bericht 2")
    assert kn.samenvoegen_selectie([{"geschikt": False, "categorie": "x", "oordeel": "Nee"}]) == (False, None, "Nee")
    tekst = 'Ik zoek het even na. {"geschikt": true, "titel": "Kort"} En nu het echte: {"geschikt": true, "titel": "T", "tekst": "A"} {naam}'
    assert kn.kies_antwoord(claude_api.json_uit(tekst, alle=True))["tekst"] == "A"
    assert claude_api.json_uit('x {"titels": [1]} {"a": 1}', "a") == {"a": 1}
    with pytest.raises(ValueError):  # afgebroken: de aanroeper moet het weten (en kan laten repareren)
        claude_api.json_uit('{"a": {"b": 1}, "c": [1, 2')
    with pytest.raises(ValueError):
        claude_api.json_uit("geen json")


def test_inhoud_en_telegramtekst():
    v = {"id": 7, "titel": "Kop <b>", "intro": "Intro", "tekst": "Een.\n\nTwee & drie.", "bronnen": [{"titel": "Bron", "url": "https://x.nl/a?b=1&c=2"}],
         "categorie": "prestatie", "sport": "hockey", "score": 81, "oordeel": "Uniek."}
    h = kn.inhoud_html(v)
    assert '<p class="zh-intro"><strong>Intro</strong></p>' in h and "<p>Twee &amp; drie.</p>" in h and "https://x.nl/a?b=1&amp;c=2" in h
    t = kn.bericht_tekst(v, "slot")
    assert t.startswith("🧤 <b>Keepersnieuws</b> · Unieke prestatie\n\n<b>Kop &lt;b&gt;</b>") and "hockey · score 81" in t and t.endswith("slot")
    assert "/zerohero/keepersnieuws/7" in t
    assert kn.bericht_tekst(v | {"gevoelig": True}).startswith("🧤 <b>Keepersnieuws</b> · Unieke prestatie\n\n⚠️ <b>Gevoelig onderwerp</b>")
    lang = kn.bericht_tekst(v | {"tekst": "woord " * 1500, "gevoelig": True})
    assert "⚠️ <b>Gevoelig" in lang and "<b>Kop &lt;b&gt;</b>" in lang
    lang = kn.bericht_tekst(v | {"tekst": "woord " * 1500})
    assert len(lang) <= 4096 and "(...)" in lang


def test_beeld_samenstellen_en_terugval(monkeypatch):
    b = io.BytesIO()
    Image.new("RGB", (1536, 1024), (40, 90, 60)).save(b, "PNG")
    png = ai_beeld.samenstellen(b.getvalue(), "Verbruggen houdt drie penalty's tegen", "voetbal")
    img = Image.open(io.BytesIO(png))
    assert img.size == (1920, 1080)  # 16:9, zoals de overzichten op de site
    # het sporticoon rechtsboven: daar staat nu wit, zonder sport niet
    hoek = (1920 - 150 - 190, 120, 1920 - 150, 310)
    wit = lambda i: sum(1 for p in i.crop(hoek).getdata() if min(p[:3]) > 200)  # noqa: E731
    zonder = Image.open(io.BytesIO(ai_beeld.samenstellen(b.getvalue(), "Kop", "")))
    assert wit(img) > 2000 and wit(zonder) < 50
    assert ai_beeld.sport_van("IJshockey") == "ijshockey" and ai_beeld.sport_van("veldhockey") == "hockey" and ai_beeld.sport_van("korfbal") == ""
    # zonder sleutel: het huisstijlbeeld, zonder foutmelding
    png, bron, fout = ai_beeld.maak("Kop", "scene")
    assert bron == "huisstijl" and not fout and Image.open(io.BytesIO(png)).size == (1920, 1080)
    # met sleutel, maar de beeldmaker weigert: huisstijl met de reden
    monkeypatch.setattr(ai_beeld, "OPENAI_KEY", "sk-x")
    monkeypatch.setattr(ai_beeld, "sfeerbeeld", lambda scene, sport="": (_ for _ in ()).throw(ai_beeld.BeeldFout("Beeldmaker gaf fout 400. geweigerd")))
    png, bron, fout = ai_beeld.maak("Kop", "scene")
    assert bron == "huisstijl" and "geweigerd" in fout
    monkeypatch.setattr(ai_beeld, "sfeerbeeld", lambda scene, sport="": b.getvalue())
    assert ai_beeld.maak("Kop", "scene")[1] == "ai-openai"


def test_beeldmaker_verzoek(monkeypatch):
    gezien = {}

    def post(url, body, headers, timeout=180):
        gezien.update(url=url, body=body, headers=headers)
        import base64  # pylint: disable=import-outside-toplevel
        return {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(b"png").decode()}}]}}]}
    monkeypatch.setattr(ai_beeld, "_post", post)
    monkeypatch.setattr(ai_beeld, "GEMINI_KEY", "g-key")
    assert ai_beeld.sfeerbeeld("gloves on wet grass") == b"png"
    assert "generativelanguage" in gezien["url"] and gezien["headers"] == {"x-goog-api-key": "g-key"}
    prompt = gezien["body"]["contents"][0]["parts"][0]["text"]
    assert "NO recognizable people" in prompt and "No text" in prompt and "gloves on wet grass" in prompt
    ai_beeld.sfeerbeeld("", "handbal")  # handbal: geen handschoenen, wel de gestreepte palen
    prompt = gezien["body"]["contents"][0]["parts"][0]["text"]
    assert "do NOT wear gloves" in prompt and "red-and-white" in prompt and "Scene: an empty handball goal" in prompt
    assert "hands in gloves" not in ai_beeld.REGELS
    assert ai_beeld.sport_prompt("korfbal").startswith("SPORT: korfbal") and "helmet with a face cage" in ai_beeld.sport_prompt("veldhockey")


def test_telegram_alleen_eigen_chat(monkeypatch):
    gedaan = []
    bot = telegram.Bot("9:z", "4242", "proef")
    monkeypatch.setitem(telegram.HANDLERS, "tst", lambda data, bericht: gedaan.append(data) or {"toast": "ok", "knoppen": []})
    monkeypatch.setattr(bot, "_vraag", lambda methode, data=None, timeout=30: gedaan.append(methode) or True)
    assert bot.verwerk({"callback_query": {"id": "1", "data": "tst:1", "message": {"message_id": 5, "chat": {"id": 999}}}}) == "niet van mij"
    assert not gedaan
    assert bot.verwerk({"callback_query": {"id": "1", "data": "tst:1", "message": {"message_id": 5, "chat": {"id": 4242}}}}) == "ok"
    assert gedaan == ["tst:1", "answerCallbackQuery", "editMessageReplyMarkup"]
    m = telegram.markup([[("Ja", "x" * 80), ("Open", "https://voorbeeld.nl/a")]])["inline_keyboard"][0]
    assert m[0]["callback_data"] == "x" * 64 and m[1] == {"text": "Open", "url": "https://voorbeeld.nl/a"}
    # één draad per token; een bot zonder token telt niet
    assert telegram.registreer(telegram.Bot("", "1", "leeg")) and "" not in telegram.BOTS
    assert kn.BOT.token == "123:abc" and kn.BOT.token in telegram.BOTS  # zonder ZEROHERO-bot: die van Buurmanitor


# ---------------------------------------------------------------- met database

@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL en TEST_ZEROHERO_DATABASE_URL")
    import psycopg  # pylint: disable=import-outside-toplevel
    with psycopg.connect(ZH, autocommit=True) as c:
        c.execute(pathlib.Path(__file__).with_name("zerohero").joinpath("basis_schema.sql").read_text())
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        yield k


@pytest.fixture
def nep(klant, monkeypatch):
    """Claude, Telegram en WordPress nagebootst; schone tabellen met drie nieuwsberichten."""
    from app.zerohero import claude_api, wp  # pylint: disable=import-outside-toplevel
    kn.q("DELETE FROM nieuws_voorstellen RETURNING 1")
    kn.q("DELETE FROM items WHERE miniflux_id >= 9000 RETURNING 1")
    kn.q("DELETE FROM instellingen WHERE sleutel LIKE 'kn_%%' RETURNING 1")
    kn.q("""INSERT INTO items (miniflux_id, bron, url, titel, binnen, sport, keepers, score, samenvatting, status) VALUES
            (9001, 'NOS', 'https://nos.nl/1', 'Verbruggen naar Ajax', now(), 'voetbal', ARRAY['Bart Verbruggen'], 88, 'Officieel.', 'nieuw'),
            (9002, 'VI', 'https://vi.nl/2', 'Ajax haalt Verbruggen', now(), 'voetbal', ARRAY['Bart Verbruggen'], 75, 'Ook.', 'nieuw'),
            (9003, 'Hockey.nl', 'https://hockey.nl/3', 'Recordaantal shoot-outs gestopt', now(), 'hockey', ARRAY['Anne Veenendaal'], 70, 'Record.', 'nieuw'),
            (9004, 'Blog', 'https://blog.nl/4', 'Iets kleins', now(), 'voetbal', ARRAY['Iemand'], 20, 'Klein.', 'nieuw')
            RETURNING 1""")
    n = {"claude": [], "telegram": [], "wp": [], "bewerkt": []}
    n["snel"] = []

    def gesprek(s, berichten, **kw):
        vraag = berichten[-1]["content"]
        if s == kn.VOORSELECTIE:  # de goedkope voorselectie: het hockeybericht is te klein
            n["snel"].append(kw.get("model"))
            klein = "Veenendaal" in vraag
            return (json.dumps({"geschikt": not klein, "categorie": "statistiek" if klein else "transfer",
                                "oordeel": "Te klein nieuws." if klein else "Officieel."}), {"input_tokens": 1000, "output_tokens": 40}, [])
        n["claude"].append(vraag)
        return json.dumps(ANTWOORD), {"input_tokens": 100, "output_tokens": 50, "zoekopdrachten": 1}, []
    monkeypatch.setattr(claude_api, "gesprek", gesprek)
    teller = iter(range(500, 600))
    monkeypatch.setattr(kn.BOT, "stuur_foto", lambda png, bijschrift="", knoppen=None, bestandsnaam="": n["telegram"].append(("foto", bijschrift)) or {"message_id": next(teller)})
    monkeypatch.setattr(kn.BOT, "stuur", lambda tekst, knoppen=None, antwoord_op=None: n["telegram"].append(("tekst", tekst, knoppen)) or {"message_id": next(teller)})
    monkeypatch.setattr(kn.BOT, "bewerk", lambda bid, tekst=None, knoppen=None: n["bewerkt"].append((bid, tekst, knoppen)))

    def nep_wp(methode, pad, data=None, **kw):
        n["wp"].append((methode, pad, data))
        if pad.startswith("/wp/v2/categories?"):
            return []
        if pad == "/wp/v2/categories":
            return {"id": 31}
        if pad.startswith("/wp/v2/posts"):
            return {"id": 777, "link": "https://zerohero.one/keepersnieuws/verbruggen", "status": (data or {}).get("status")}
        return {}
    monkeypatch.setattr(wp, "_wp", nep_wp)
    monkeypatch.setattr(wp, "upload", lambda inhoud, naam, alt="", bijschrift="", mime="image/png": n["wp"].append(("upload", naam, bijschrift)) or (55, ""))
    monkeypatch.setattr(wp, "beeld_in_artikel", lambda post_id: True)
    monkeypatch.setattr(wp, "zet_uitgelicht", lambda post_id, media_id: n["wp"].append(("uitgelicht", post_id, media_id)))
    monkeypatch.setattr(wp, "naar_prullenbak_als_concept", lambda post_id: n["wp"].append(("prullenbak", post_id)))
    yield n
    # niets achterlaten voor de andere tests (Vera en de portal lezen dezelfde database)
    kn.q("DELETE FROM items WHERE miniflux_id >= 9010 RETURNING 1")


def test_ronde_voorstel_en_plaatsen_via_telegram(nep, klant):
    assert kn.ronde() == "1 voorgesteld, 1 geen keepersnieuws"
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    ids = {r["miniflux_id"]: r["id"] for r in kn.q("SELECT id, miniflux_id FROM items WHERE miniflux_id >= 9000")}
    assert sorted(v["item_ids"]) == sorted([ids[9001], ids[9002]]) and v["categorie"] == "transfer" and "—" not in v["titel"] and v["beeld"] is None  # nog geen beeld
    assert v["telegram_foto"] is None and v["telegram_tekst"] == 500 and v["tokens_in"] == 100
    assert len(nep["telegram"]) == 1 and nep["telegram"][0][0] == "tekst"  # één bericht per nieuws, zonder foto
    assert nep["telegram"][0][2] == kn.KNOPPEN(v["id"]) and "Bart Verbruggen tekent bij Ajax, voor vier jaar" in nep["telegram"][0][1]
    over = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'overgeslagen'")
    assert over["oordeel"] == "Te klein nieuws." and over["item_ids"] == [ids[9003]] and over["tokens_in_snel"] == 1000
    assert len(nep["claude"]) == 1 and nep["snel"] == [kn.SNEL, kn.SNEL]  # het schrijfmodel alleen voor het geschikte verhaal
    assert "Iets kleins" not in "".join(nep["claude"])  # onder de drempel
    assert kn.ronde() == "geen nieuw keepersnieuws boven de drempel"  # niets dubbel
    # Plaatsen in Telegram: live in de categorie Keepersnieuws, met het beeld
    uit = kn.op_knop(f"kn:{v['id']}:p", {"message_id": 500})
    assert uit["toast"] == "Geplaatst op zerohero.one." and uit["knoppen"] == [] and "✅ <b>Geplaatst</b>" in uit["tekst"]
    post = next(d for m, p, d in nep["wp"] if p == "/wp/v2/posts")  # eerst als concept, zoals de blogs
    assert post["status"] == "draft" and post["categories"] == [31, 32] and post["featured_media"] == 55 and "zh-intro" in post["content"]
    assert next(d for m, p, d in nep["wp"] if p == "/wp/v2/posts/777")["status"] == "publish"  # daarna publiceren
    rm = next(d for m, p, d in nep["wp"] if p == "/rankmath/v1/updateMeta")  # Keepersnieuws als hoofdcategorie (breadcrumb)
    assert rm["objectID"] == 777 and rm["meta"] == {"rank_math_primary_category": 31}
    assert kn.taak_hoofdcategorie(None) == "1 van 1 berichten" and kn.taak_hoofdcategorie(None) == "niets te doen"
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (v["id"],))
    assert v["besluit"] == "geplaatst" and v["wp_post_id"] == 777 and v["besloten_via"] == "telegram"
    assert v["beeld_bron"] == "huisstijl" and any(x[0] == "upload" for x in nep["wp"])  # het beeld kwam bij het plaatsen
    assert kn.inst()["kn_categorie_id"] == "31"
    assert kn.op_knop(f"kn:{v['id']}:n", {})["toast"] == "Dit voorstel is al afgehandeld."
    # en de pagina's
    r = klant.get("/zerohero/keepersnieuws", headers=KOP)
    assert r.status_code == 200 and "Bart Verbruggen tekent bij Ajax" in r.text and "Te klein nieuws." in r.text
    assert 'href="/zerohero/keepersnieuws/' in r.text and 'src="/zerohero/keepersnieuws/' in r.text
    r = klant.get(f"/zerohero/keepersnieuws/{v['id']}", headers=KOP)
    assert r.status_code == 200 and "Opslaan en op de site bijwerken" in r.text
    assert klant.get(f"/zerohero/keepersnieuws/{v['id']}/beeld.png", headers=KOP).headers["content-type"] == "image/png"


def test_niet_plaatsen_met_reden_en_leren(nep, klant):
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    uit = kn.op_knop(f"kn:{v['id']}:n", {})
    assert [r[0][1] for r in uit["knoppen"]][:2] == [f"kn:{v['id']}:r:geen_nieuws", f"kn:{v['id']}:r:niet_relevant"]
    kn.op_knop(f"kn:{v['id']}:r:oud", {})
    assert kn.op_antwoord(500, "Transfers pas als het officieel is") == "Dank je, je toelichting is bewaard. Daar leer ik van."
    assert kn.op_antwoord(12345, "iets") is None
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (v["id"],))
    assert (v["besluit"], v["reden"], v["toelichting"]) == ("afgewezen", "oud", "Transfers pas als het officieel is")
    leer = kn.leerstof()
    assert "[NIET GEPLAATST] Bart Verbruggen tekent bij Ajax" in leer and "Te laat, al oud nieuws (Transfers pas als het officieel is)" in leer
    assert "Transfer: 0 geplaatst, 0 zelf bekeken, 1 niet geplaatst" in leer
    # richtlijnen en besluiten gaan mee in de volgende opdracht
    klant.post("/zerohero/keepersnieuws/instellingen", headers=FORM, data={"kn_aan": "1", "kn_drempel": "50", "kn_per_dag": "5", "kn_per_ronde": "2",
                                                                          "kn_zoeken": "2", "kn_richtlijnen": "Meer handbal."})
    kn.q("""INSERT INTO items (miniflux_id, bron, url, titel, binnen, sport, keepers, score, status) VALUES
            (9010, 'Handbal.nl', 'https://handbal.nl/1', 'Keeper stopt zeven strafworpen', now(), 'handbal', ARRAY['Rinka Duijndam'], 66, 'nieuw') RETURNING 1""")
    kn.ronde()
    assert "Richtlijnen van Martijn (gaan voor alles):\nMeer handbal." in nep["claude"][-1] and "NIET GEPLAATST" in nep["claude"][-1]


def test_scherm_aanpassen_concept_en_dagmaximum(nep, klant):
    kn.zet("kn_per_dag", 1)
    assert kn.ronde() == "1 voorgesteld, 0 geen keepersnieuws"
    assert kn.ronde() == "dagmaximum bereikt"
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}", headers=FORM, follow_redirects=True, data={
        "titel": "Verbruggen terug in Amsterdam", "intro": "Intro", "tekst": "Alinea een.\n\nAlinea twee – met streepje.", "categorie": "transfer", "actie": "concept"})
    assert "Staat als concept in WordPress." in r.text
    post = next(d for m, p, d in nep["wp"] if p == "/wp/v2/posts")
    assert post["status"] == "draft" and post["title"] == "Verbruggen terug in Amsterdam" and "–" not in post["content"]
    assert nep["bewerkt"] and nep["bewerkt"][-1][2] == []  # de knoppen in Telegram zijn weg
    # nieuw huisstijlbeeld: ook in WordPress
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}/beeld", headers=FORM, data={"soort": "huisstijl"}, follow_redirects=True)
    assert "Nieuw beeld gemaakt." in r.text and ("uitgelicht", 777, 55) in nep["wp"]
    # afwijzen vanaf het scherm: het concept gaat naar de prullenbak
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}", headers=FORM, follow_redirects=True, data={
        "titel": "Verbruggen terug in Amsterdam", "actie": "afwijzen", "reden": "tekst", "toelichting": "Kop te lang"})
    assert "Niet geplaatst." in r.text and ("prullenbak", 777) in nep["wp"]
    assert klant.post(f"/zerohero/keepersnieuws/{v['id']}", headers=KOP, data={"titel": "x"}).status_code == 403  # zonder herkomst


def test_uit_en_zonder_telegram(nep, klant, monkeypatch):
    kn.zet("kn_aan", "0")
    assert kn.ronde() == "staat uit"
    monkeypatch.setattr(kn.BOT, "actief", lambda: False)
    kn.zet("kn_aan", "1")
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    assert v["fout"].startswith("Telegram is niet ingesteld") and not nep["telegram"]


def test_kosten_budget_en_beeldkeuze(nep, klant, monkeypatch):
    monkeypatch.setattr(ai_beeld, "OPENAI_KEY", "sk-x")
    b = io.BytesIO()
    Image.new("RGB", (1536, 1024), (40, 90, 60)).save(b, "PNG")
    monkeypatch.setattr(ai_beeld, "sfeerbeeld", lambda scene, sport="": b.getvalue())
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    assert v["beeld"] is None and v["beelden"] == {}  # geen beeld (en geen kosten) voor het plaatsen
    # Plaatsen in Telegram met een AI-beeld: de tik krijgt meteen antwoord, het bericht wordt bijgewerkt als het klaar is
    monkeypatch.setattr(kn, "_achtergrond", lambda f: f())
    uit = kn.op_knop(f"kn:{v['id']}:p", {"message_id": 500})
    assert uit["toast"] == "Even geduld: beeld maken en plaatsen." and uit["knoppen"] == [] and "⏳ <b>Bezig</b>" in uit["tekst"]
    assert nep["bewerkt"][-1][0] == 500 and "✅ <b>Geplaatst</b>" in nep["bewerkt"][-1][1] and nep["bewerkt"][-1][2] == []
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (v["id"],))
    assert v["beeld_bron"] == "ai-openai" and v["beelden"] == {"openai": 1} and v["besluit"] == "geplaatst"
    euro, aantal = kn.kosten_maand()
    assert aantal == 2 and 0.05 < euro < 0.2  # twee voorselecties, één bericht, één beeld
    r = klant.get("/zerohero/keepersnieuws", headers=KOP)
    assert "Kosten deze maand: € " in r.text and "van € 15" in r.text
    # AI-beelden uit: het huisstijlbeeld, en geen extra kosten
    kn.zet("kn_ai_beeld", "0")
    v = kn.nieuw_beeld(v)
    assert v["beeld_bron"] == "huisstijl" and v["beelden"] == {"openai": 1}
    # budget op: geen nieuwe voorstellen, en een nieuw beeld wordt huisstijl
    kn.zet("kn_ai_beeld", "1")
    kn.zet("kn_budget", "0.01")
    assert kn.ronde().startswith("maandbudget op")
    v = kn.nieuw_beeld(v)
    assert v["beeld_bron"] == "huisstijl" and "maandbudget is op" in v["beeld_fout"]
    assert "Het maandbudget voor keepersnieuws is op" in klant.get("/zerohero/keepersnieuws", headers=KOP).text
    # in het kostenoverzicht van het dashboard
    from app.zerohero import kosten  # pylint: disable=import-outside-toplevel
    regel = next(x for x in kosten._onderdelen(kn.q, kn.q1("SELECT now() - interval '1 day' AS t")["t"], kn.q1("SELECT now() + interval '1 day' AS t")["t"])  # pylint: disable=protected-access
                 if x["naam"].startswith("Keepersnieuws"))
    assert regel["aantal"] == 2 and regel["dollar"] > 0.07


def test_linkknoppen_als_n8n_de_bot_bezet(nep, klant, monkeypatch):
    monkeypatch.setitem(kn.BOT.status, "conflict", True)
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    knoppen = nep["telegram"][0][2]
    assert knoppen[0][0] == ("✅ Plaatsen", f"https://buurmanitor.neighbour.nu/zerohero/keepersnieuws/{v['id']}?snel=plaatsen")
    r = klant.get(f"/zerohero/keepersnieuws/{v['id']}?snel=plaatsen", headers=KOP)
    assert "Je koos in Telegram: <b>Plaatsen</b>" in r.text and "Ja, nu plaatsen" in r.text
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}", headers=FORM, follow_redirects=True,
                   data={"titel": v["titel"], "intro": v["intro"], "tekst": v["tekst"], "categorie": v["categorie"], "actie": "plaatsen"})
    assert "Geplaatst op zerohero.one." in r.text
    assert "Je koos in Telegram" not in klant.get(f"/zerohero/keepersnieuws/{v['id']}?snel=plaatsen", headers=KOP).text  # al besloten
    assert "linkknoppen naar de Doellijn" in klant.get("/zerohero/keepersnieuws", headers=KOP).text


def test_tikken_via_n8n(nep, klant, monkeypatch):
    """n8n heeft de webhook van de Doellijn-bot en stuurt tikken door naar /intern/telegram."""
    import threading  # pylint: disable=import-outside-toplevel
    monkeypatch.setitem(kn.BOT.status, "conflict", True)
    kn.zet("kn_via_n8n", "1")
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    assert nep["telegram"][0][2] == [[("✅ Plaatsen", f"kn:{v['id']}:p"), ("👀 Ik kijk zelf", f"kn:{v['id']}:l")], [("❌ Niet plaatsen", f"kn:{v['id']}:n")]]
    beantwoord = []
    monkeypatch.setattr(kn.BOT, "_vraag", lambda methode, data=None, timeout=30: beantwoord.append((methode, data)) or True)
    update = {"update_id": 1, "callback_query": {"id": "cq1", "data": f"kn:{v['id']}:l", "message": {"message_id": 500, "chat": {"id": 4242}}}}
    assert klant.post("/intern/telegram", json=update).status_code == 403  # zonder geheim
    r = klant.post("/intern/telegram", json=[{"body": update}], headers={"X-Doellijn-Secret": "conceptgeheim"})
    assert r.json() == {"ok": True, "verwerkt": True}
    for th in threading.enumerate():
        if th.name == "telegram-via-n8n":
            th.join(10)
    assert kn.q1("SELECT besluit FROM nieuws_voorstellen WHERE id = %s", (v["id"],))["besluit"] == "later"
    assert beantwoord[0][0] == "answerCallbackQuery" and beantwoord[0][1]["text"] == "Staat als concept in WordPress."
    assert klant.post("/intern/telegram", json={"update_id": 2}, headers={"X-Doellijn-Secret": "conceptgeheim"}).json()["verwerkt"] is False
    monkeypatch.setitem(kn.BOT.status, "fout", "Op deze bot staat een webhook")
    pagina = klant.get("/zerohero/keepersnieuws", headers=KOP).text
    assert "met tikknoppen die n8n doorstuurt" in pagina and "Op deze bot staat een webhook" not in pagina


def test_mislukt_opnieuw(nep, klant, monkeypatch):
    import threading  # pylint: disable=import-outside-toplevel
    from app.zerohero import claude_api  # pylint: disable=import-outside-toplevel
    echt = claude_api.gesprek
    monkeypatch.setattr(claude_api, "gesprek", lambda s, b, **kw: ("Geen JSON", {}, []) if s == kn.SYSTEEM else echt(s, b, **kw))
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'fout'")
    assert v and "Opnieuw proberen" in klant.get(f"/zerohero/keepersnieuws/{v['id']}", headers=KOP).text
    assert "Mislukte voorstellen opnieuw proberen" in klant.get("/zerohero/keepersnieuws", headers=KOP).text
    monkeypatch.setattr(claude_api, "gesprek", echt)
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}/opnieuw", headers=FORM, follow_redirects=True)
    assert "wordt opnieuw geschreven" in r.text
    for th in threading.enumerate():
        if th.name == "keepersnieuws-opnieuw":
            th.join(20)
    nieuw = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    assert nieuw and sorted(nieuw["item_ids"]) == sorted(v["item_ids"]) and not kn.q1("SELECT 1 AS x FROM nieuws_voorstellen WHERE id = %s", (v["id"],))
    r = klant.post("/zerohero/keepersnieuws/opnieuw", headers=FORM, follow_redirects=True)
    assert r.status_code == 200 and "geen recente mislukte" in r.text


def test_wordpress_fout_bij_publiceren(nep, klant, monkeypatch):
    """Een plugin geeft een 500 bij publiceren: staat het toch live, dan is het goed; anders een duidelijke melding,
    en opnieuw proberen maakt geen tweede beeld of bericht."""
    import io as _io  # pylint: disable=import-outside-toplevel
    import urllib.error  # pylint: disable=import-outside-toplevel
    from app.zerohero import wp  # pylint: disable=import-outside-toplevel
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    staat = {"live": False, "geplaatst": 0}
    oud = wp._wp

    def fout_bij_publiceren(methode, pad, data=None, **kw):
        if pad == "/wp/v2/posts/777" and methode == "POST" and (data or {}).get("status") == "publish":
            staat["geplaatst"] += 1
            e = urllib.error.HTTPError("https://x", 500, "Internal Server Error", {}, None)
            raise wp.WpFout(e, methode, pad, b'{"code":"internal_server_error","message":"<p>Er is een kritieke fout opgetreden.</p>",'
                                                b'"data":{"status":500,"error":{"message":"Uncaught Error in plugin-x.php"}}}')
        if pad.startswith("/wp/v2/posts/777?context=edit"):
            return {"id": 777, "status": "publish" if staat["live"] else "draft", "link": "https://zerohero.one/x"}
        return oud(methode, pad, data, **kw)
    monkeypatch.setattr(wp, "_wp", fout_bij_publiceren)
    uitslag = kn.besluit(v["id"], "geplaatst", via="telegram")
    assert uitslag.startswith("Plaatsen lukte niet: WordPress gaf 500 bij het bericht bijwerken of publiceren: Er is een kritieke fout")
    assert "plugin-x.php" in kn.q1("SELECT fout FROM nieuws_voorstellen WHERE id = %s", (v["id"],))["fout"]
    staat["live"] = True  # de volgende keer staat hij (ondanks de fout) toch live
    assert kn.besluit(v["id"], "geplaatst") == "Geplaatst op zerohero.one."
    assert sum(1 for x in nep["wp"] if x[0] == "upload") == 1 and sum(1 for m, p, d in nep["wp"] if p == "/wp/v2/posts") == 1
    assert staat["geplaatst"] == 2


def test_wp_melding_uit():
    from app.zerohero import wp  # pylint: disable=import-outside-toplevel
    assert wp.melding_uit(b'{"code":"rest_x","message":"Mag niet."}') == "Mag niet. \u00b7 rest_x"
    assert wp.melding_uit(b"<html><body><h1>Fout</h1>  boom</body></html>") == "Fout boom"
    assert wp.stap("POST", "/wp/v2/media") == "het beeld uploaden"


def test_sport_categorie():
    assert [kn.sport_categorie(x) for x in ("voetbal", "Handbal", "veldhockey", "ijshockey", "waterpolo", "lacrosse", "", None)] == \
        [32, 34, 31, 30, 33, 35, 36, 36]


def test_eigen_aanwijzing_voor_het_beeld(nep, klant, monkeypatch):
    b = io.BytesIO()
    Image.new("RGB", (1536, 1024), (40, 90, 60)).save(b, "PNG")
    gevraagd = []
    monkeypatch.setattr(ai_beeld, "GEMINI_KEY", "g-key")
    monkeypatch.setattr(ai_beeld, "sfeerbeeld", lambda scene, sport="": gevraagd.append((scene, sport)) or b.getvalue())
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    assert "Wat het AI-beeld moet laten zien" in klant.get(f"/zerohero/keepersnieuws/{v['id']}", headers=KOP).text
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}/beeld", headers=FORM, follow_redirects=True,
                   data={"soort": "ai", "scene": "Een leeg doel met rode stoeltjes"})
    assert "Nieuw beeld gemaakt." in r.text
    assert gevraagd[-1] == ("Een leeg doel met rode stoeltjes", "voetbal")
    assert kn.q1("SELECT beeld_scene FROM nieuws_voorstellen WHERE id = %s", (v["id"],))["beeld_scene"] == "Een leeg doel met rode stoeltjes"


def test_zelfde_kop_of_link():
    from app.zerohero.main import titel_sleutel, zelfde  # pylint: disable=import-outside-toplevel
    a = {"titel": "New series spotlights the NHL goalie mental health crisis - NHL.com", "url": "https://www.nhl.com/news/a?x=1"}
    b = {"titel": "New series spotlights the NHL goalie mental-health crisis", "url": "https://news.google.com/rss/1"}
    c = {"titel": "Iets heel anders over een keeper", "url": "https://nhl.com/news/a"}
    assert titel_sleutel(a) == titel_sleutel(b) and zelfde(a, b) and zelfde(a, c)  # zelfde kop; zelfde link
    assert not zelfde({"titel": "Kort"}, {"titel": "Kort"})  # te kort om op te vergelijken
    assert not zelfde(b, c)


def test_zelfde_bericht_via_andere_feed_niet_opnieuw(nep, klant):
    kn.q("""INSERT INTO items (miniflux_id, bron, url, titel, binnen, sport, keepers, score, samenvatting, status) VALUES
            (9010, 'NHL', 'https://nhl.com/x', 'New series spotlights the NHL goalie mental health crisis', now(), 'ijshockey', '{}', 80, 'Serie.', 'nieuw')
            RETURNING 1""")
    kn.ronde()
    assert kn.q1("SELECT 1 AS x FROM nieuws_voorstellen v JOIN items i ON i.id = ANY(v.item_ids) WHERE i.miniflux_id = 9010")
    kn.q("""INSERT INTO items (miniflux_id, bron, url, titel, binnen, sport, keepers, score, samenvatting, status) VALUES
            (9011, 'Google News', 'https://news.google.com/y', 'New series spotlights the NHL goalie mental health crisis - ESPN', now(), 'ijshockey', '{}', 80, 'Serie.', 'nieuw')
            RETURNING 1""")
    assert not any(9011 in [lid.get("miniflux_id") for lid in v["leden"]] for v in kn.kandidaten())


def test_items_uniek_voor_de_ochtendbriefing(nep, klant):
    from app.zerohero.main import titel_sleutel  # pylint: disable=import-outside-toplevel
    for t in ("New series spotlights the NHL goalie mental health crisis - NHL.com", "Zó hield Bijlów de nul! | NOS", "Kort", ""):
        assert (kn.q1("SELECT doellijn_kop(%s) AS k", (t,))["k"] or "") == titel_sleutel({"titel": t})
    kn.q("""INSERT INTO items (miniflux_id, bron, url, titel, binnen, score, status) VALUES
            (9020, 'NHL', 'https://nhl.com/a', 'New series spotlights the NHL goalie mental health crisis', now() - interval '2 days', 70, 'nieuw'),
            (9021, 'GN', 'https://news.google.com/b', 'New series spotlights the NHL goalie mental health crisis - ESPN', now(), 70, 'nieuw'),
            (9022, 'X', 'https://nhl.com/a', 'Totaal andere kop over dezelfde link hier', now(), 70, 'nieuw'),
            (9023, 'Y', 'https://y.nl/c', 'Een heel ander bericht over een keeper', now(), 70, 'nieuw') RETURNING 1""")
    uniek = {r["miniflux_id"] for r in kn.q("SELECT miniflux_id FROM items_uniek WHERE miniflux_id BETWEEN 9020 AND 9029")}
    assert uniek == {9020, 9023}


def test_archiveren(nep, klant):
    kn.ronde()
    v = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'verstuurd'")
    over = kn.q1("SELECT * FROM nieuws_voorstellen WHERE status = 'overgeslagen'")
    r = klant.get("/zerohero/keepersnieuws", headers=KOP)
    assert "Afgehandelde naar het archief (1)" in r.text and "Archief (0)" in r.text and "Te klein nieuws." in r.text
    # in één keer: alleen wat afgehandeld is (geen keepersnieuws), het voorstel dat op Martijn wacht blijft staan
    r = klant.post("/zerohero/keepersnieuws/archiveren", headers=FORM, follow_redirects=True)
    assert "1 afgehandelde voorstel(len) naar het archief." in r.text and "Te klein nieuws." not in r.text
    assert "Bart Verbruggen tekent bij Ajax" in r.text and "Archief (1)" in r.text
    assert kn.q1("SELECT gearchiveerd FROM nieuws_voorstellen WHERE id = %s", (over["id"],))["gearchiveerd"]
    r = klant.get("/zerohero/keepersnieuws?archief=1", headers=KOP)
    assert "Te klein nieuws." in r.text and "Bart Verbruggen tekent bij Ajax" not in r.text and "Terugzetten" in r.text
    # één voorstel archiveren vanaf het voorstel, en weer terugzetten
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}/archief", headers=FORM, data={"naar": "lijst"}, follow_redirects=True)
    assert "Naar het archief." in r.text and "Bart Verbruggen tekent bij Ajax" not in r.text
    assert "Dit voorstel staat in het archief." in klant.get(f"/zerohero/keepersnieuws/{v['id']}", headers=KOP).text
    r = klant.post(f"/zerohero/keepersnieuws/{v['id']}/archief", headers=FORM, data={"terug": "1", "naar": "voorstel"}, follow_redirects=True)
    assert "Terug in de lijst." in r.text and kn.q1("SELECT gearchiveerd FROM nieuws_voorstellen WHERE id = %s", (v["id"],))["gearchiveerd"] is None
    # gearchiveerd telt nog mee: hetzelfde nieuws komt niet opnieuw
    assert kn.ronde() == "geen nieuw keepersnieuws boven de drempel"
    assert klant.post(f"/zerohero/keepersnieuws/{v['id']}/archief", headers=KOP).status_code == 403  # zonder herkomst
