"""Signal: berichten uit de envelop halen, alleen bewaren waar meelezen aan staat, voorstellen maken (Claude nagebootst),
de schermen en het uitvoeren van een voorstel. De Signal-container wordt nagebootst. De database- en schermtests draaien
alleen met TEST_DATABASE_URL. Draaien in een eigen proces (zie tests/alles.sh).
"""
import datetime as dt
import json
import os

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.voorbeeld.nl"
os.environ["TOEGESTANE_HOSTS"] = "buurmanitor.voorbeeld.nl"
os.environ["SIGNAL_URL"] = "http://signal:8080"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.kern import config  # noqa: E402
from app.signaal import bron, verwerken  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.voorbeeld.nl"}
T0 = 1_763_380_800_000  # ms


def env(**kw):
    e = {"source": "+31600000001", "sourceNumber": "+31600000001", "sourceUuid": "uuid-anna", "sourceName": "Anna", "timestamp": T0}
    e.update(kw)
    return {"envelope": e, "account": "+31600000000"}


def dm(tekst="", ts=T0, **kw):
    return {"timestamp": ts, "message": tekst, **kw}


# ---------------------------------------------------------------- zonder database

def test_uit_envelop():
    b = verwerken.uit_envelop(env(dataMessage=dm("Kun je donderdag de notulen sturen?")))
    assert b["sleutel"] == "persoon:uuid-anna" and b["soort"] == "persoon" and b["chatnaam"] == "Anna"
    assert b["van"] == "Anna" and not b["van_mij"] and b["ts"] == T0 and b["tijd"].year == 2025

    g = verwerken.uit_envelop(env(dataMessage=dm("Vergadering om acht", groupInfo={"groupId": "grp1"})), {"grp1": "Fractie"})
    assert g["sleutel"] == "groep:grp1" and g["soort"] == "groep" and g["chatnaam"] == "Fractie" and g["van"] == "Anna"

    eigen = verwerken.uit_envelop(env(sourceUuid="uuid-ik", sourceNumber="+31600000000", source="+31600000000", sourceName="Martijn",
                                      syncMessage={"sentMessage": dm("Doe ik", destinationUuid="uuid-anna")}))
    assert eigen["sleutel"] == "persoon:uuid-anna" and eigen["van"] == "jij" and eigen["van_mij"] and eigen["chatnaam"] == ""

    # Notitie aan jezelf (Note to Self): met of zonder bestemming, of met je eigen nummer als bestemming
    for m in (dm("notitie aan mezelf"), dm("notitie", destinationNumber="+31600000000", destinationUuid="uuid-ik")):
        n = verwerken.uit_envelop(env(sourceUuid="uuid-ik", sourceNumber="+31600000000", syncMessage={"sentMessage": m}))
        assert n["sleutel"] == "persoon:notities" and n["chatnaam"] == "Notities aan jezelf" and n["van_mij"]
    assert verwerken.soorten([env(dataMessage=dm("a")), env(dataMessage=dm("b", groupInfo={"groupId": "g"})), env(receiptMessage={}),
                              env(syncMessage={"sentMessage": dm("c")}), env(syncMessage={"contacts": {}}), env(typingMessage={}),
                              env(dataMessage=dm(reaction={"emoji": "x"})), env(dataMessage=dm()), env()]) == {
        "bericht": 1, "bericht in groep": 1, "leesbevestiging": 1, "zelf verstuurd": 1, "synchronisatie": 1, "aan het typen": 1,
        "reactie": 1, "leeg bericht": 1, "overig": 1}
    assert verwerken.uit_envelop(env(dataMessage=dm(reaction={"emoji": "👍"}))) is None
    assert verwerken.uit_envelop(env(dataMessage=dm("x", remoteDelete={"timestamp": 1}))) is None
    assert verwerken.uit_envelop(env(receiptMessage={"isDelivery": True})) is None
    assert verwerken.uit_envelop(env(typingMessage={"action": "STARTED"})) is None

    bijlage = verwerken.uit_envelop(env(dataMessage=dm(attachments=[{"filename": "agenda.pdf"}, {"contentType": "image/jpeg"}])))
    assert bijlage["tekst"] == "[bijlage: agenda.pdf, image/jpeg]"
    bewerkt = verwerken.uit_envelop(env(editMessage={"dataMessage": dm("Toch vrijdag")}))
    assert bewerkt["tekst"] == "Toch vrijdag (bewerkt)"


def test_schoon_voorstel_en_tijden():
    v = verwerken.schoon_voorstel({"soort": "afspraak", "titel": "Overleg — wijkraad", "datum": "2026-11-20", "begintijd": "19.30",
                                   "eindtijd": "25:00", "toelichting": "Anna vroeg het"})
    assert v["titel"] == "Overleg , wijkraad" and v["datum"] == dt.date(2026, 11, 20) and v["begintijd"] == dt.time(19, 30)
    assert v["eindtijd"] is None
    assert verwerken.schoon_voorstel({"soort": "afspraak", "titel": "Ooit koffie"})["soort"] == "taak"  # zonder dag geen afspraak
    assert verwerken.schoon_voorstel({"soort": "idee", "titel": "x"}) is None
    assert verwerken.schoon_voorstel({"soort": "taak", "titel": "  "}) is None
    assert verwerken.schoon_voorstel("onzin") is None
    assert verwerken._json('Hier: {"voorstellen": []} klaar') == {"voorstellen": []}  # pylint: disable=protected-access


def test_bron_zonder_url(monkeypatch):
    monkeypatch.setattr(bron, "URL", "")
    assert not bron.actief()
    with pytest.raises(bron.SignalFout):
        bron.accounts()


def test_accounts_voor_de_eerste_koppeling(monkeypatch):
    """Vóór de eerste koppeling geeft de container fout 500 op de lijst met accounts: dat is 'nog niet gekoppeld'."""
    def vraag(pad, **kw):
        raise bron.SignalFout("Signal gaf fout 500: Couldn't get list of accounts: Failed to read local accounts list", 500)
    monkeypatch.setattr(bron, "_vraag", vraag)
    assert bron.accounts() == []

    def kapot(pad, **kw):
        raise bron.SignalFout("Signal gaf fout 502: iets anders", 502)
    monkeypatch.setattr(bron, "_vraag", kapot)
    with pytest.raises(bron.SignalFout):
        bron.accounts()


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
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'signal_%'")
        c.execute("DELETE FROM takenbord")
    return klant


@pytest.fixture
def container(monkeypatch):
    """De Signal-container nagebootst: een gekoppeld nummer, een groep en een rij binnenkomende berichten."""
    staat = {"accounts": ["+31600000000"], "inbox": [], "groepvragen": 0}

    def groepen(_n):
        staat["groepvragen"] += 1
        return [{"id": "group.abc", "internal_id": "grp1", "name": "Fractie"}]

    def ontvang(_n):
        uit, staat["inbox"] = staat["inbox"], []
        return uit
    monkeypatch.setattr(bron, "accounts", lambda: staat["accounts"])
    monkeypatch.setattr(bron, "groepen", groepen)
    monkeypatch.setattr(bron, "ontvang", ontvang)
    monkeypatch.setattr(bron, "qr_code", lambda naam="": b"\x89PNG-nep")
    monkeypatch.setattr(bron, "URL", "http://signal:8080")
    return staat


def test_opslaan_alleen_bij_meelezen(schoon, container):
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("geheim", ts=T0)), env(dataMessage=dm("hallo groep", ts=T0 + 1, groupInfo={"groupId": "grp1"}))]
        uitslag = verwerken.taak_signal(c)
        assert "2 berichten" in uitslag and "0 bewaard" in uitslag and "2 nieuwe chats" in uitslag
        chats = {r["sleutel"]: r for r in c.execute("SELECT * FROM signal_chats").fetchall()}
        assert chats["groep:grp1"]["naam"] == "Fractie" and chats["persoon:uuid-anna"]["naam"] == "Anna"
        assert c.execute("SELECT count(*) AS n FROM signal_berichten").fetchone()["n"] == 0  # niets van de tekst bewaard

        c.execute("UPDATE signal_chats SET meelezen = true WHERE sleutel = 'groep:grp1'")
        container["inbox"] = [env(dataMessage=dm("Donderdag 20.00 fractieoverleg", ts=T0 + 2, groupInfo={"groupId": "grp1"})),
                              env(dataMessage=dm("nog een geheim", ts=T0 + 3))]
        verwerken.taak_signal(c)
        verwerken.taak_signal(c)  # niets nieuws: niets dubbel
        rijen = c.execute("SELECT tekst FROM signal_berichten").fetchall()
        assert [r["tekst"] for r in rijen] == ["Donderdag 20.00 fractieoverleg"]
        assert c.execute("SELECT aantal FROM signal_chats WHERE sleutel = 'persoon:uuid-anna'").fetchone()["aantal"] == 2

        # een groep die nog niet bekend is: de namen worden opnieuw opgehaald
        vragen = container["groepvragen"]
        container["inbox"] = [env(dataMessage=dm("nieuw", ts=T0 + 4, groupInfo={"groupId": "grp2"}))]
        verwerken.taak_signal(c)
        assert container["groepvragen"] == vragen + 1


def test_analyseren(schoon, container, monkeypatch):
    gevraagd = []

    def gesprek(c, persona, systeem, berichten, **kw):
        gevraagd.append(berichten[-1]["content"])
        return json.dumps({"voorstellen": [
            {"soort": "afspraak", "titel": "Fractieoverleg", "datum": "2026-11-20", "begintijd": "20:00", "eindtijd": "22:00",
             "locatie": "Stadhuis", "toelichting": "Anna plande het"},
            {"soort": "taak", "titel": "Notulen sturen", "datum": "2026-11-19", "toelichting": "Toegezegd"}]})
    monkeypatch.setattr(verwerken.claude, "gesprek", gesprek)
    nu = dt.datetime.now(config.TZ)
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("start", groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        c.execute("UPDATE signal_chats SET meelezen = true WHERE sleutel = 'groep:grp1'")
        cid = c.execute("SELECT id FROM signal_chats WHERE sleutel = 'groep:grp1'").fetchone()["id"]
        for i, tekst in enumerate(["Donderdag 20.00 fractieoverleg op het stadhuis", "Martijn, stuur jij de notulen?", "Doe ik"]):
            c.execute("INSERT INTO signal_berichten (chat_id, ts, van, van_mij, tekst, tijd) VALUES (%s, %s, %s, %s, %s, %s)",
                      (cid, T0 + 10 + i, "jij" if i == 2 else "Anna", i == 2, tekst, nu - dt.timedelta(minutes=3)))
        assert verwerken.analyseren(c, nu) == 0 and not gevraagd  # het gesprek is nog niet stil
        assert verwerken.analyseren(c, nu + dt.timedelta(minutes=15)) == 2
        assert "Martijn, stuur jij de notulen?" in gevraagd[0] and "jij: Doe ik" in gevraagd[0]
        assert verwerken.analyseren(c, nu + dt.timedelta(minutes=30)) == 0 and len(gevraagd) == 1  # al geanalyseerd

        # nieuwe berichten, Claude stelt hetzelfde nog eens voor: niet dubbel
        c.execute("INSERT INTO signal_berichten (chat_id, ts, van, van_mij, tekst, tijd) VALUES (%s, %s, 'Anna', false, 'Top', %s)",
                  (cid, T0 + 20, nu))
        assert verwerken.analyseren(c, nu + dt.timedelta(minutes=30)) == 0
        assert "Al voorgesteld" in gevraagd[-1]
        v = c.execute("SELECT * FROM signal_voorstellen WHERE soort = 'afspraak'").fetchone()
        assert v["begintijd"] == dt.time(20) and v["locatie"] == "Stadhuis" and v["status"] == "open"

        # voorstellen uit voor deze chat: Claude komt er niet meer aan
        c.execute("UPDATE signal_chats SET voorstellen = false WHERE id = %s", (cid,))
        c.execute("INSERT INTO signal_berichten (chat_id, ts, van, van_mij, tekst, tijd) VALUES (%s, %s, 'Anna', false, 'Nog iets', %s)",
                  (cid, T0 + 21, nu))
        verwerken.analyseren(c, nu + dt.timedelta(hours=1))
        assert len(gevraagd) == 2


def test_schermen(schoon, container, monkeypatch):
    # niet bereikbaar
    def kapot():
        raise bron.SignalFout("De Signal-container is niet bereikbaar (nep).")
    monkeypatch.setattr(bron, "accounts", kapot)
    r = schoon.get("/bram/signal", headers=KOP)
    assert r.status_code == 200 and "nog niet bereikbaar" in r.text

    # niet gekoppeld: QR-code
    monkeypatch.setattr(bron, "accounts", lambda: [])
    r = schoon.get("/bram/signal", headers=KOP)
    assert "Ik heb gescand" in r.text and "/bram/signal/qr" in r.text
    q = schoon.get("/bram/signal/qr", headers=KOP)
    assert q.content == b"\x89PNG-nep" and q.headers["cache-control"] == "no-store"
    r = schoon.post("/bram/signal/gekoppeld", headers=FORM, follow_redirects=False)
    assert "Nog+geen+koppeling" in r.headers["location"]

    # gekoppeld
    monkeypatch.setattr(bron, "accounts", lambda: ["+31600000000"])
    r = schoon.post("/bram/signal/gekoppeld", headers=FORM, follow_redirects=True)
    assert "Gekoppeld met +31600000000" in r.text
    container["inbox"] = [env(dataMessage=dm("hallo", groupInfo={"groupId": "grp1"}))]
    r = schoon.post("/bram/signal/ophalen", headers=FORM, follow_redirects=True)
    assert "1 berichten" in r.text and "Fractie" in r.text
    assert "Chats met meelezen (Bram en Iris), alle bronnen: <b>0</b>" in r.text and "Zet bij Chats hierboven meelezen aan" in r.text
    assert "1 keer iets van Signal" in r.text and "1 bericht in groep" in r.text

    # De container geeft regels als tekst, en de groepen lukken even niet: ophalen gaat door
    def geen_groepen(_n):
        raise bron.SignalFout("groepen kapot")
    monkeypatch.setattr(bron, "groepen", geen_groepen)
    container["inbox"] = [json.dumps(env(dataMessage=dm("als tekst", ts=T0 + 50))), "geen json"]
    r = schoon.post("/bram/signal/ophalen", headers=FORM, follow_redirects=True)
    assert "1 berichten" in r.text

    # Gaat het ophalen mis, dan staat de reden op het scherm
    def stuk(_n):
        raise bron.SignalFout("Signal gaf fout 400: iets ging mis")
    monkeypatch.setattr(bron, "ontvang", stuk)
    schoon.post("/bram/signal/ophalen", headers=FORM)
    r = schoon.get("/bram/signal", headers=KOP)
    assert "Het ophalen ging" in r.text and "Signal gaf fout 400: iets ging mis" in r.text

    # zonder herkomst: geweigerd
    assert schoon.post("/bram/signal/ophalen", headers=KOP, follow_redirects=False).status_code in (400, 403)
    assert "/bram/signal" in schoon.get("/bram", headers=KOP).text


def test_meelezen_uit_wist(schoon, container):
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("bericht-voor-meelezen", groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        cid = c.execute("SELECT id FROM signal_chats").fetchone()["id"]
    r = schoon.post(f"/bram/signal/chat/{cid}", headers=FORM, data={"meelezen": "1", "voorstellen": "1", "domein": "politiek",
                                                                      "eigen_naam": "Fractie PRO"}, follow_redirects=True)
    assert "Bram en Iris lezen vanaf nu mee in Fractie PRO" in r.text
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("bericht-na-aanzetten", ts=T0 + 5, groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        c.execute("""INSERT INTO signal_voorstellen (chat_id, soort, titel) VALUES (%s, 'taak', 'Iets')""", (cid,))
        ch = c.execute("SELECT * FROM signal_chats WHERE id = %s", (cid,)).fetchone()
        assert ch["domein"] == "politiek" and ch["eigen_naam"] == "Fractie PRO"
    r = schoon.get(f"/bram/signal/chat/{cid}", headers=KOP)
    assert "bericht-na-aanzetten" in r.text and "bericht-voor-meelezen" not in r.text  # alleen wat er na het aanzetten binnenkwam
    r = schoon.post(f"/bram/signal/chat/{cid}", headers=FORM, data={"domein": "politiek"}, follow_redirects=True)
    assert "1 bewaarde berichten zijn gewist" in r.text
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM signal_berichten").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM signal_voorstellen WHERE status = 'open'").fetchone()["n"] == 0
    assert schoon.post("/bram/signal/chat/999999", headers=FORM, data={}).status_code == 404


def test_archief_zoeken_en_taak_uit_bericht(schoon, container, monkeypatch):
    """Archief: wel bewaren, niet naar Claude. Meelezen uit met archief aan: berichten blijven. Zoeken en een taak uit een bericht."""
    gevraagd = []
    monkeypatch.setattr(verwerken.claude, "gesprek", lambda *a, **kw: gevraagd.append(a) or '{"voorstellen": []}')
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("start", groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        cid = c.execute("SELECT id FROM signal_chats").fetchone()["id"]
    r = schoon.post(f"/bram/signal/chat/{cid}", headers=FORM, data={"archief": "1", "voorstellen": "1", "domein": "prive"}, follow_redirects=True)
    assert "komen vanaf nu in het archief" in r.text
    nu = dt.datetime.now(config.TZ)
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("Kun jij de zaal reserveren voor de jaarvergadering?\nGraag voor vrijdag.", ts=T0 + 7,
                                                 groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        assert c.execute("SELECT count(*) AS n FROM signal_berichten").fetchone()["n"] == 1
        assert verwerken.analyseren(c, nu + dt.timedelta(hours=1)) == 0 and not gevraagd  # archief gaat niet naar Claude
        assert verwerken.samenvatting(c, nu - dt.timedelta(days=3650)) == ""  # en Iris ziet het ook niet
        bid = c.execute("SELECT id FROM signal_berichten").fetchone()["id"]

    r = schoon.get("/bram/signal/zoek?q=jaarvergadering", headers=KOP)
    assert "1 berichten gevonden" in r.text and "zaal reserveren" in r.text and f"signal={bid}" in r.text
    assert "0 berichten gevonden" in schoon.get("/bram/signal/zoek?q=onvindbaarwoord", headers=KOP).text
    assert "zaal reserveren" in schoon.get(f"/bram/signal/chat/{cid}", headers=KOP).text

    r = schoon.get(f"/taken?nieuw=1&signal={bid}", headers=KOP)
    assert 'value="Kun jij de zaal reserveren voor de jaarvergadering?"' in r.text and "Uit Signal (Fractie), van Anna" in r.text

    # meelezen aan en weer uit, archief blijft aan: berichten blijven bewaard
    schoon.post(f"/bram/signal/chat/{cid}", headers=FORM, data={"meelezen": "1", "archief": "1", "domein": "prive"})
    r = schoon.post(f"/bram/signal/chat/{cid}", headers=FORM, data={"archief": "1", "domein": "prive"}, follow_redirects=True)
    assert "blijven in het archief" in r.text
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM signal_berichten").fetchone()["n"] == 1
    # alles uit: gewist
    r = schoon.post(f"/bram/signal/chat/{cid}", headers=FORM, data={"domein": "prive"}, follow_redirects=True)
    assert "De 1 bewaarde berichten zijn gewist" in r.text


def test_samenvatting_voor_iris(schoon, container):
    nu = dt.datetime.now(config.TZ)
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("x", groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        cid = c.execute("SELECT id FROM signal_chats").fetchone()["id"]
        c.execute("UPDATE signal_chats SET meelezen = true, domein = 'politiek' WHERE id = %s", (cid,))
        for i in range(5):
            t_ = nu - dt.timedelta(hours=30 - i * 10)
            c.execute("INSERT INTO signal_berichten (chat_id, ts, van, van_mij, tekst, tijd, binnen) VALUES (%s, %s, 'Anna', false, %s, %s, %s)",
                      (cid, T0 + 30 + i, f"bericht {i}", t_, t_))
        c.execute("INSERT INTO signal_voorstellen (chat_id, soort, titel) VALUES (%s, 'taak', 'Notulen sturen')", (cid,))
        t = verwerken.samenvatting(c, nu - dt.timedelta(hours=24), per_chat=2)
    assert f"Signal-chat #{cid}: Fractie (groep, politiek), 4 berichten, de laatste 2 hieronder" in t
    assert "bericht 4" in t and "bericht 3" in t and "bericht 2" not in t and "bericht 0" not in t
    assert "- taak: Notulen sturen" in t


def test_voorstel_uitvoeren(schoon, container, monkeypatch):
    from app.clara import web as clara_web  # pylint: disable=import-outside-toplevel
    from app.taken import web as taken_web  # pylint: disable=import-outside-toplevel
    gemaakt = []
    monkeypatch.setattr(clara_web, "_maak", lambda c, doel, titel, start, eind, omschrijving="", **kw: gemaakt.append(
        {"doel": doel, "titel": titel, "start": start, "eind": eind, "omschrijving": omschrijving} | kw))
    monkeypatch.setattr(taken_web, "agenda_op_achtergrond", lambda tids, weg=None: None)
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("x", groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        cid = c.execute("SELECT id FROM signal_chats").fetchone()["id"]
        ids = [c.execute("""INSERT INTO signal_voorstellen (chat_id, soort, titel, datum, begintijd, locatie, toelichting)
                            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""", (cid, *rij)).fetchone()["id"] for rij in [
            ("taak", "Notulen sturen", dt.date(2026, 11, 19), None, "", "Toegezegd"),
            ("afspraak", "Fractieoverleg", dt.date(2026, 11, 20), dt.time(20), "Stadhuis", "Anna plande het"),
            ("taak", "Onzin", None, None, "", ""),
            ("afspraak", "Ergens heen", None, None, "", "")]]
    r = schoon.post(f"/bram/signal/voorstel/{ids[0]}", headers=FORM, data={"actie": "taak", "titel": "Notulen naar de fractie"},
                    follow_redirects=True)
    assert "staat op het takenbord" in r.text
    with _db() as c:
        t = c.execute("SELECT * FROM takenbord").fetchone()
        assert t["titel"] == "Notulen naar de fractie" and t["einddatum"] == dt.date(2026, 11, 19) and "Uit Signal (Fractie)" in t["beschrijving"]
    r = schoon.post(f"/bram/signal/voorstel/{ids[1]}", headers=FORM, data={"actie": "afspraak", "doel": "1|cal", "eindtijd": "21:30"},
                    follow_redirects=True)
    assert "staat in je agenda op 20-11 om 20.00" in r.text
    a = gemaakt[0]
    assert a["doel"] == "1|cal" and a["start"].hour == 20 and a["eind"].hour == 21 and a["eind"].minute == 30
    assert a["locatie"] == "Stadhuis" and not a["heledag"]
    r = schoon.post(f"/bram/signal/voorstel/{ids[3]}", headers=FORM, data={"actie": "afspraak"}, follow_redirects=True)
    assert "Geef de afspraak een datum" in r.text
    r = schoon.post(f"/bram/signal/voorstel/{ids[2]}", headers=FORM, data={"actie": "weg"}, follow_redirects=True)
    assert r.status_code == 200
    r = schoon.post(f"/bram/signal/voorstel/{ids[0]}", headers=FORM, data={"actie": "taak"}, follow_redirects=True)
    assert "al afgehandeld" in r.text
    with _db() as c:
        status = {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM signal_voorstellen").fetchall()}
    assert status == {ids[0]: "gedaan", ids[1]: "gedaan", ids[2]: "weg", ids[3]: "open"}


def test_alles_wissen(schoon, container):
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("x"))]
        verwerken.taak_signal(c)
    r = schoon.post("/bram/signal/vergeten", headers=FORM, follow_redirects=False)
    assert r.status_code == 303
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM signal_chats").fetchone()["n"] == 0
        assert not verwerken.instelling(c, "signal_nummer")


def test_chat_verwijderen_en_terugzetten(schoon, container):
    with _db() as c:
        container["inbox"] = [env(dataMessage=dm("eerste", groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        cid = c.execute("SELECT id FROM signal_chats").fetchone()["id"]
        c.execute("UPDATE signal_chats SET meelezen = true WHERE id = %s", (cid,))
        container["inbox"] = [env(dataMessage=dm("geheim bericht", ts=T0 + 1, groupInfo={"groupId": "grp1"}))]
        verwerken.taak_signal(c)
        c.execute("INSERT INTO signal_voorstellen (chat_id, soort, titel) VALUES (%s, 'taak', 'Iets')", (cid,))
    assert "Chat verwijderen" in schoon.get("/bram/signal", headers=KOP).text
    r = schoon.post(f"/bram/signal/chat/{cid}/verwijderen", headers=FORM, follow_redirects=True)
    assert "Fractie is verwijderd, met 1 bewaarde berichten" in r.text and f'id="chat-{cid}"' not in r.text
    assert "Verwijderde chats (1)" in r.text
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM signal_berichten").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM signal_voorstellen WHERE status = 'open'").fetchone()["n"] == 0
        # nieuwe berichten: niet bewaard, chat blijft weg
        container["inbox"] = [env(dataMessage=dm("nog een", ts=T0 + 2, groupInfo={"groupId": "grp1"}))]
        assert "0 bewaard" in verwerken.taak_signal(c)
        assert c.execute("SELECT verborgen FROM signal_chats WHERE id = %s", (cid,)).fetchone()["verborgen"]
    r = schoon.get("/bram/signal?verborgen=1", headers=KOP)
    assert f'id="chat-{cid}"' in r.text and "Terugzetten" in r.text
    r = schoon.post(f"/bram/signal/chat/{cid}/terugzetten", headers=FORM, follow_redirects=True)
    assert "staat weer bij de chats" in r.text and f'id="chat-{cid}"' in r.text and "Verwijderde chats" not in r.text
    assert schoon.post("/bram/signal/chat/999999/verwijderen", headers=FORM).status_code == 404
    assert schoon.post(f"/bram/signal/chat/{cid}/verwijderen", headers=KOP).status_code in (400, 403)  # zonder herkomst
