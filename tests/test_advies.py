"""Iris' adviezen: samenstellen, maken (Claude nagebootst), versturen, de planner, feedback en leren, en de instellingen.
De database- en schermtests draaien alleen met TEST_DATABASE_URL. Draaien in een eigen proces (zie tests/alles.sh).
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
os.environ["POLITIEK_MAIL_AAN"] = "ik@voorbeeld.nl"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.advies import maken  # noqa: E402
from app.kern import config  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.voorbeeld.nl"}
ANTWOORD = {"samenvatting": "Vandaag draait het om de begroting — en om het rapport.",
            "punten": [{"soort": "deadline", "titel": "Rapport is te laat", "tekst": "Taak #1 is twee dagen over tijd.", "bron": "taken",
                        "link": "/taken?bewerk=1"},
                       {"soort": "risico", "titel": "Overlap morgen", "tekst": "Twee afspraken tegelijk.", "link": "https://kwaad.example/x"},
                       {"soort": "onzin", "titel": "Bel de griffie", "tekst": "Over de agenda."}]}


# ---------------------------------------------------------------- zonder database

def test_schoon_en_json():
    a = maken.schoon_advies(maken._json("Hier is het:\n" + json.dumps(ANTWOORD) + "\nSucces!"))  # pylint: disable=protected-access
    assert a["samenvatting"] == "Vandaag draait het om de begroting, en om het rapport."  # geen gedachtestreepje
    assert [p["soort"] for p in a["punten"]] == ["deadline", "risico", "advies"]  # onbekende soort wordt advies
    assert a["punten"][0]["link"] == "/taken?bewerk=1" and a["punten"][1]["link"] == ""  # alleen links binnen Buurmanitor
    with pytest.raises(ValueError):
        maken._json("geen json")  # pylint: disable=protected-access
    assert maken.dag(dt.date(2026, 11, 17)) == "di 17-11"


def test_aan_de_beurt():
    s = {"actief": True, "dagen": [0, 1, 2, 3, 4, 5, 6], "tijd": dt.time(7, 0), "laatst": None}
    nu = dt.datetime(2026, 11, 17, 7, 3, tzinfo=config.TZ)
    assert maken.aan_de_beurt(s, nu) and not maken.aan_de_beurt(s, nu.replace(hour=6))
    assert not maken.aan_de_beurt(s | {"laatst": nu.replace(minute=0)}, nu) and not maken.aan_de_beurt(s | {"actief": False}, nu)


# ---------------------------------------------------------------- met database

@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        with _db() as c:
            c.execute("TRUNCATE iris_adviessoorten, iris_adviezen, iris_feedback, iris_lessen CASCADE")
            c.execute("DELETE FROM takenbord")
            c.execute("DELETE FROM instellingen WHERE sleutel = 'iris_achtergrond'")
        yield k


def _db():
    from app.kern import db  # pylint: disable=import-outside-toplevel
    return db.pool.connection()


@pytest.fixture
def nep(monkeypatch):
    """Claude en de mail nagebootst; Claude onthoudt wat hij kreeg."""
    gevraagd, verstuurd = [], []

    def gesprek(c, persona, systeem, berichten, **kw):
        gevraagd.append({"systeem": systeem, "vraag": berichten[-1]["content"], "model": kw.get("model")})
        if "Bestaande lessen" in berichten[-1]["content"]:  # leren
            return json.dumps({"actie": "nieuw", "tekst": "Noem overlap in de agenda pas als het om vandaag of morgen gaat."})
        return json.dumps(ANTWOORD)
    monkeypatch.setattr(maken.claude, "gesprek", gesprek)
    monkeypatch.setattr(maken, "stuur_mail", lambda **kw: verstuurd.append(kw))
    from app.advies import web  # pylint: disable=import-outside-toplevel

    def direct(fid):  # leren zonder achtergrond, zodat de test het meteen ziet
        with _db() as c:
            maken.leer(c, fid)
    monkeypatch.setattr(web, "_leren_op_achtergrond", direct)
    return gevraagd, verstuurd


def test_maken_versturen_en_planner(klant, nep):
    gevraagd, verstuurd = nep
    vandaag = dt.datetime.now(config.TZ).date()
    with _db() as c:
        maken.standaard_maken(c)
        maken.standaard_maken(c)  # niet dubbel
        soorten = c.execute("SELECT * FROM iris_adviessoorten ORDER BY tijd").fetchall()
        assert [s["naam"] for s in soorten] == ["Ochtendadvies", "Avondadvies"] and all(s["actief"] and s["mail"] for s in soorten)
        assert soorten[0]["mail_aan"] == "ik@voorbeeld.nl" and set(soorten[0]["bronnen"]) == set(maken.BRONNEN)
        laat = c.execute("""INSERT INTO takenbord (titel, status, prio, einddatum) VALUES ('Rapport schrijven', 'actief', 'hoog', %s)
                            RETURNING id""", (vandaag - dt.timedelta(days=2),)).fetchone()["id"]
        c.execute("INSERT INTO takenbord (titel, status, prio, einddatum) VALUES ('Begroting lezen', 'gepland', 'middel', %s)",
                  (vandaag + dt.timedelta(days=3),))
        c.execute("INSERT INTO takenbord (titel, status, prio, wacht_op) VALUES ('Offerte', 'wachtend', 'middel', 'de drukker')")
        c.execute("INSERT INTO instellingen (sleutel, waarde) VALUES ('iris_achtergrond', 'De begroting gaat voor.')")
        c.execute("INSERT INTO iris_lessen (tekst) VALUES ('Mail van de griffie is altijd belangrijk.')")
        gegevens = maken.verzamel(c, soorten[0])
        assert f"taak #{laat}: Rapport schrijven" in gegevens and "2 dagen te laat" in gegevens and "Begroting lezen" in gegevens
        assert "wacht op de drukker" in gegevens and "Promonitor" not in gegevens  # Promonitor draait hier niet: stil weggelaten
        a, uitslag = maken.maak_en_verstuur(c, soorten[0])
    assert uitslag == "3 punten, gemaild" and len(a["punten"]) == 3
    v = gevraagd[-1]
    assert "De begroting gaat voor." in v["vraag"] and "Rapport schrijven" in v["vraag"] and soorten[0]["opdracht"] in v["vraag"]
    assert "Mail van de griffie is altijd belangrijk." in v["systeem"] and "Hooguit 5 punten" in v["systeem"]
    m = verstuurd[-1]
    assert m["aan"] == "ik@voorbeeld.nl" and m["onderwerp"].startswith("Iris: ochtendadvies") and "Rapport is te laat" in m["html"]
    assert f"/iris/advies/{a['id']}" in m["html"] and "kwaad.example" not in m["html"]
    # De planner: het ochtendadvies is aan de beurt (tijd net voorbij), daarna niet nog eens.
    with _db() as c:
        start = (dt.datetime.now(config.TZ) - dt.timedelta(minutes=5))
        if start.date() != vandaag:
            pytest.skip("rond middernacht")
        c.execute("UPDATE iris_adviessoorten SET tijd = %s, laatst = NULL WHERE id = %s", (start.time(), soorten[0]["id"]))
        c.execute("UPDATE iris_adviessoorten SET tijd = '23:59' WHERE id = %s", (soorten[1]["id"],))
        assert maken.taak_irisadvies(c).startswith("Ochtendadvies: 3 punten, gemaild")
        assert maken.taak_irisadvies(c) == "niets aan de beurt"


def test_scherm_feedback_en_leren(klant, nep):
    gevraagd, _ = nep
    pagina = klant.get("/iris", headers=KOP).text
    assert "Rapport is te laat" in pagina and "Was dit advies nuttig?" in pagina and "Nu een advies" in pagina
    with _db() as c:
        a = c.execute("SELECT * FROM iris_adviezen ORDER BY id DESC LIMIT 1").fetchone()
        lessen_voor = c.execute("SELECT count(*) AS n FROM iris_lessen").fetchone()["n"]
    # 👍 zonder opmerking: bewaard, geen les.
    r = klant.post(f"/iris/advies/{a['id']}/feedback", headers=FORM, follow_redirects=False, data={"punt": "0", "oordeel": "1", "terug": "/iris#advies"})
    assert r.headers["location"].startswith("/iris?melding=Dank") and r.headers["location"].endswith("#advies")
    # 👎 met opmerking op punt 2: Iris maakt er een les van.
    klant.post(f"/iris/advies/{a['id']}/feedback", headers=FORM,
               data={"punt": "1", "oordeel": "-1", "opmerking": "Overlap over twee weken boeit me nu niet", "terug": "https://elders.example"})
    with _db() as c:
        fb = c.execute("SELECT * FROM iris_feedback WHERE advies_id = %s ORDER BY punt", (a["id"],)).fetchall()
        lessen = c.execute("SELECT * FROM iris_lessen ORDER BY id").fetchall()
    assert [(f["punt"], f["oordeel"]) for f in fb] == [(0, 1), (1, -1)] and fb[1]["verwerkt"]
    assert len(lessen) == lessen_voor + 1 and lessen[-1]["bron"] == "feedback" and "overlap" in lessen[-1]["tekst"].lower()
    leer = gevraagd[-1]
    assert leer["model"] == config.CLAUDE_MODEL_LICHT and "Overlap over twee weken boeit me nu niet" in leer["vraag"] and "Overlap morgen" in leer["vraag"]
    # Nog eens op hetzelfde punt: de feedback wordt vervangen, niet verdubbeld; een duim houdt de opmerking.
    klant.post(f"/iris/advies/{a['id']}/feedback", headers=FORM, data={"punt": "1", "oordeel": "1"})
    with _db() as c:
        f = c.execute("SELECT * FROM iris_feedback WHERE advies_id = %s AND punt = 1", (a["id"],)).fetchall()
    assert len(f) == 1 and f[0]["oordeel"] == 1 and f[0]["opmerking"] == "Overlap over twee weken boeit me nu niet"
    # Op het geheel.
    klant.post(f"/iris/advies/{a['id']}/feedback", headers=FORM, data={"punt": "", "oordeel": "0", "opmerking": "Meer over de raad graag"})
    with _db() as c:
        assert c.execute("SELECT opmerking FROM iris_adviezen WHERE id = %s", (a["id"],)).fetchone()["opmerking"] == "Meer over de raad graag"
    assert klant.post(f"/iris/advies/{a['id']}/feedback", headers=FORM, data={"punt": "9", "oordeel": "1"}).status_code == 404
    # Het volgende advies krijgt de feedback en de lessen mee.
    with _db() as c:
        maken.maak(c, c.execute("SELECT * FROM iris_adviessoorten ORDER BY tijd LIMIT 1").fetchone())
    assert "niet nuttig over punt 'Overlap morgen'" not in gevraagd[-1]["systeem"]  # vervangen door 'nuttig'
    assert "nuttig over punt 'Overlap morgen': Overlap over twee weken" in gevraagd[-1]["systeem"]
    assert "Meer over de raad graag" in gevraagd[-1]["systeem"] and lessen[-1]["tekst"] in gevraagd[-1]["systeem"]
    assert "Rapport is te laat" in klant.get(f"/iris/advies/{a['id']}", headers=KOP).text
    assert "Ochtendadvies" in klant.get("/iris/advies", headers=KOP).text


def test_instellingen(klant, nep):
    pagina = klant.get("/iris/instellingen", headers=KOP).text
    assert "Ochtendadvies" in pagina and "Wat Iris leerde" in pagina and "Noem overlap" in pagina
    with _db() as c:
        s = c.execute("SELECT * FROM iris_adviessoorten ORDER BY tijd LIMIT 1").fetchone()
    data = {"naam": "Ochtend", "actief": "1", "tijd": "06:45", "horizon": "10", "lengte": "normaal", "dag_0": "1", "dag_1": "1",
            "opdracht": "Wat telt vandaag?", "bron_taken": "1", "bron_agenda": "1", "mail": "1", "mail_aan": "ik@voorbeeld.nl"}
    assert "Opgeslagen" in klant.post(f"/iris/instellingen/soort/{s['id']}", headers=FORM, data=data, follow_redirects=False).headers["location"]
    with _db() as c:
        s2 = c.execute("SELECT * FROM iris_adviessoorten WHERE id = %s", (s["id"],)).fetchone()
    assert (s2["tijd"], s2["horizon"], s2["lengte"], s2["dagen"], s2["bronnen"]) == (dt.time(6, 45), 10, "normaal", [0, 1], ["agenda", "taken"])
    assert "mailadres" in klant.post(f"/iris/instellingen/soort/{s['id']}", headers=FORM, follow_redirects=False,
                                     data=data | {"mail_aan": "geen"}).headers["location"]
    r = klant.post("/iris/instellingen/soort/nieuw", headers=FORM, follow_redirects=False, data=data | {"naam": "Weekvooruitblik", "dag_6": "1"})
    assert "toegevoegd" in r.headers["location"]
    klant.post("/iris/instellingen/achtergrond", headers=FORM, data={"achtergrond": "Congres gaat voor."})
    klant.post("/iris/lessen", headers=FORM, data={"tekst": "Nieuwsbrieven nooit noemen."})
    with _db() as c:
        les = c.execute("SELECT * FROM iris_lessen WHERE tekst = 'Nieuwsbrieven nooit noemen.'").fetchone()
        assert maken.achtergrond(c) == "Congres gaat voor." and les["bron"] == "zelf"
    klant.post(f"/iris/les/{les['id']}", headers=FORM, data={"tekst": "Nieuwsbrieven alleen bij grote nieuwtjes.", "actief": ""})
    with _db() as c:
        assert c.execute("SELECT actief, tekst FROM iris_lessen WHERE id = %s", (les["id"],)).fetchone() == \
            {"actief": False, "tekst": "Nieuwsbrieven alleen bij grote nieuwtjes."}
        assert "Nieuwsbrieven" not in maken.lessen_tekst(c)  # uitgezet: gaat niet mee
    klant.post(f"/iris/les/{les['id']}", headers=FORM, data={"actie": "weg"})
    with _db() as c:
        assert not c.execute("SELECT 1 FROM iris_lessen WHERE id = %s", (les["id"],)).fetchone()


def test_gesprek_kent_de_lessen(klant, nep):
    gevraagd, _ = nep
    from app import iris  # pylint: disable=import-outside-toplevel
    with _db() as c:
        gid = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('iris', 'Test') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', 'Wat speelt er?')", (gid,))
        iris.antwoord(c, gid)
        c.execute("DELETE FROM gesprekken WHERE id = %s", (gid,))
    assert "Wat Martijn je leerde" in gevraagd[-1]["systeem"] and "Mail van de griffie is altijd belangrijk." in gevraagd[-1]["systeem"]
