"""Dagelijkse overzichten: opmaak per ontvanger, onderdelen instellen, voorbeeld, versturen (proef en iedereen) en de planner.

De onderdelen uit Promonitor en ZEROHERO en Claude zijn nagebootst; versturen gaat naar een lijstje in plaats van de mail.
De database- en schermtests draaien alleen met TEST_DATABASE_URL. Draaien in een eigen proces (zie tests/alles.sh).
"""
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"
os.environ["POLITIEK_MAIL_AAN"] = "martijn@voorbeeld.nl"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="

import pytest  # noqa: E402

from app.kern import config  # noqa: E402
from app.overzichten import onderdelen as od, opmaak, verzenden, web  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
DAG = dt.date(2026, 11, 13)


# ---------------------------------------------------------------- zonder database

def test_invullen_en_datum():
    assert opmaak.invullen("Goedemorgen {voornaam}, het is {dag}", DAG, {"naam": "Jeanette Hendriks"}) == "Goedemorgen Jeanette, het is vrijdag"
    assert opmaak.invullen("Goedemorgen {voornaam},", DAG, {}) == "Goedemorgen,"
    assert opmaak.invullen("Briefing {datum} {onbekend}", DAG) == "Briefing vrijdag 13 november 2026 {onbekend}"


def test_per_ontvanger_geen_interne_zaken():
    secties = [
        {"code": "nieuws", "titel": "Nieuws", "items": [{"titel": "Bericht <b>", "url": "https://x.nl/a", "intern": "https://buurmanitor/x"}],
         "link": ("Naar de Inbox", "https://buurmanitor.neighbour.nu/politiek/inbox"), "alleen_eigen": False},
        {"code": "taken", "titel": "Taken", "items": [{"titel": "Geheim plan"}], "alleen_eigen": True},
        {"code": "leeg", "titel": "Leeg", "items": [], "alleen_eigen": False},
    ]
    eigen = opmaak.voor_ontvanger(secties, {"rol": "eigen"})
    lid = opmaak.voor_ontvanger(secties, {"rol": "lid"})
    assert [s["code"] for s in eigen] == ["nieuws", "taken"] and eigen[0]["link"]
    assert [s["code"] for s in lid] == ["nieuws"] and lid[0]["link"] is None and "intern" not in lid[0]["items"][0]
    assert "intern" in secties[0]["items"][0]  # het origineel blijft heel (de volgende ontvanger kan Martijn zijn)
    html = opmaak.mail_html({"kop": "PROMONITOR", "kleur": "#FF0000", "intro": "Hoi {voornaam},"}, lid, DAG, {"naam": "Niek Engbers"}, voet="Afmelden")
    assert "Bericht &lt;b&gt;" in html and "Hoi Niek," in html and "Geheim plan" not in html and "buurmanitor" not in html.lower()
    tekst = opmaak.platte_tekst({"kop": "PROMONITOR"}, eigen, DAG)
    assert "NIEUWS" in tekst and "https://x.nl/a" in tekst and "Geheim plan" in tekst
    assert "Niets nieuws" in opmaak.telegram({"kop": "X"}, [], DAG)


def test_aan_de_beurt():
    tz = config.TZ
    o = {"actief": True, "dagen": [0, 1, 2, 3, 4], "tijd": dt.time(8, 0), "laatst": None}
    vrijdag = dt.datetime(2026, 11, 13, 8, 5, tzinfo=tz)
    assert verzenden.aan_de_beurt(o, vrijdag)
    assert not verzenden.aan_de_beurt(o, vrijdag.replace(hour=7))  # te vroeg
    assert not verzenden.aan_de_beurt(o, vrijdag.replace(hour=13))  # te laat (na een lange storing niet meer)
    assert not verzenden.aan_de_beurt(o | {"laatst": vrijdag.replace(minute=1)}, vrijdag)  # vandaag al verstuurd
    assert verzenden.aan_de_beurt(o | {"laatst": vrijdag - dt.timedelta(days=1)}, vrijdag)
    assert not verzenden.aan_de_beurt(o, vrijdag + dt.timedelta(days=1))  # zaterdag
    assert not verzenden.aan_de_beurt(o | {"actief": False}, vrijdag)


def test_onderdelen_uit_formulier():
    bestaand = od.standaard_onderdelen("politiek")
    f = {"code_0": "samenvatting", "aan_0": "1", "titel_0": "Kort", "max_0": "99",
         "code_1": "deadlines", "titel_1": "", "opt_1_dagen": "3", "verplaats": "1:op"}
    uit = web.onderdelen_uit_formulier(f, "politiek", bestaand)
    assert [u["code"] for u in uit[:2]] == ["deadlines", "samenvatting"]
    assert uit[1]["aan"] and uit[1]["titel"] == "Kort" and uit[1]["max"] == 30
    assert not uit[0]["aan"] and uit[0]["titel"] == "Jouw deadlines" and uit[0]["opties"] == {"dagen": 3}
    # Een onderdeel dat er later bij kwam, staat er (uit) bij; een onbekend of verkeerd domein valt weg.
    oud = [{"code": "nieuws", "aan": True}, {"code": "keepersnieuws", "aan": True}, {"code": "bestaat-niet"}]
    codes = [o["code"] for o in od.aangevuld("politiek", oud)]
    assert codes[0] == "nieuws" and "keepersnieuws" not in codes and "bestaat-niet" not in codes and "deadlines" in codes
    assert od.optie({"opties": {"drempel": "x"}}, od.ONDERDELEN["nieuws"], "drempel") == 60
    assert web.dagen_tekst([0, 1, 2, 3, 4]) == "werkdagen" and web.dagen_tekst([2, 0]) == "ma, wo"


# ---------------------------------------------------------------- met database

@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        with _db() as c:
            c.execute("TRUNCATE overzichten CASCADE")
        yield k


def _db():
    from app.kern import db  # pylint: disable=import-outside-toplevel
    return db.pool.connection()


@pytest.fixture
def nep(monkeypatch):
    """Onderdelen met vaste inhoud, en mail en Claude nagebootst."""
    def nieuws(omg, inst, ontvanger=None):
        return {"items": [{"label": "80", "titel": f"Nieuws {n}", "url": f"https://nieuws.nl/{n}", "bron": "ED"} for n in range(12)],
                "link": ("Naar de Inbox", "https://buurmanitor.neighbour.nu/politiek/inbox")}

    def deadlines(omg, inst, ontvanger=None):
        ik = od._ik(omg, ontvanger)  # pylint: disable=protected-access
        return {"items": [{"label": "vandaag", "titel": f"Bijdrage van {ik}"}]} if ik else {"items": []}

    def taken(omg, inst, ontvanger=None):
        return {"items": [{"titel": "Privétaak", "intern": "https://buurmanitor.neighbour.nu/taken?bewerk=1"}]}
    monkeypatch.setattr(od.ONDERDELEN["nieuws"], "functie", nieuws)
    monkeypatch.setattr(od.ONDERDELEN["deadlines"], "functie", deadlines)
    monkeypatch.setattr(od.ONDERDELEN["taken"], "functie", taken)
    monkeypatch.setattr(od, "politiek_instellingen", lambda omg: {"agenda_ik": "Martijn"})
    verstuurd = []
    monkeypatch.setattr(verzenden, "stuur_mail", lambda **kw: verstuurd.append(kw))
    gevraagd = []
    monkeypatch.setattr(verzenden.claude, "gesprek", lambda c, p, s, b, **kw: gevraagd.append(b[0]["content"]) or "Vandaag telt Nieuws 0 — echt.")
    verzenden._samenvattingen.clear()  # pylint: disable=protected-access
    return verstuurd, gevraagd


def _instellen(klant, oid, aan):
    """Het formulier van een overzicht invullen met deze onderdelen aan (de rest uit)."""
    with _db() as c:
        o = c.execute("SELECT * FROM overzichten WHERE id = %s", (oid,)).fetchone()
    data = {"naam": o["naam"], "tijd": "07:45", "kop": "PROMONITOR", "kleur": "#FF0000", "intro": "Goedemorgen {voornaam},",
            "onderwerp": "Briefing {datum}", "afzender": "Promonitor", "leeg_versturen": "1"}
    data |= {f"dag_{d}": "1" for d in range(5)}
    for i, inst in enumerate(od.aangevuld(o["werkruimte"], o["onderdelen"])):
        data[f"code_{i}"] = inst["code"]
        data[f"max_{i}"] = "3" if inst["code"] == "nieuws" else str(inst["max"])
        if inst["code"] in aan:
            data[f"aan_{i}"] = "1"
    return klant.post(f"/overzichten/{oid}", headers=FORM, data=data, follow_redirects=False)


def test_standaard_en_instellen(klant, nep):
    pagina = klant.get("/overzichten", headers=KOP).text
    assert "Promonitor ochtendbriefing" in pagina and "ZEROHERO dagoverzicht" in pagina
    with _db() as c:
        lijst = c.execute("SELECT * FROM overzichten ORDER BY id").fetchall()
        assert [o["actief"] for o in lijst] == [False, False]  # niets gaat vanzelf weg voordat Martijn het aanzet
        assert c.execute("SELECT email, rol FROM overzicht_ontvangers WHERE overzicht_id = %s", (lijst[0]["id"],)).fetchall() == \
            [{"email": "martijn@voorbeeld.nl", "rol": "eigen"}]
    klant.get("/overzichten", headers=KOP)  # de tweede keer niet nog eens
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM overzichten").fetchone()["n"] == 2
    oid = lijst[0]["id"]
    assert "Jouw deadlines" in klant.get(f"/overzichten/{oid}", headers=KOP).text
    r = _instellen(klant, oid, {"samenvatting", "deadlines", "nieuws", "raadsinformatie", "taken"})
    assert r.status_code == 303 and "Opgeslagen" in r.headers["location"]
    # Ontvangers: een fractielid met haar eigen deadlines, een ongeldig adres en een dubbele.
    r = klant.post(f"/overzichten/{oid}/ontvangers", headers=FORM, follow_redirects=False,
                   data={"naam": "Jeanette Hendriks", "email": "Jeanette@Voorbeeld.nl", "rol": "lid", "persoon": "Jeanette"})
    assert "toegevoegd" in r.headers["location"]
    assert "geldig" in klant.post(f"/overzichten/{oid}/ontvangers", headers=FORM, follow_redirects=False,
                                  data={"email": "geen-adres"}).headers["location"]
    assert "staat+al" in klant.post(f"/overzichten/{oid}/ontvangers", headers=FORM, follow_redirects=False,
                                    data={"email": "jeanette@voorbeeld.nl"}).headers["location"]
    # Verschuiven: nieuws omhoog.
    with _db() as c:
        o = c.execute("SELECT * FROM overzichten WHERE id = %s", (oid,)).fetchone()
    codes = [x["code"] for x in o["onderdelen"]]
    i = codes.index("nieuws")
    data = {f"code_{n}": c_ for n, c_ in enumerate(codes)} | {f"aan_{n}": "1" for n, x in enumerate(o["onderdelen"]) if x["aan"]}
    data |= {"naam": o["naam"], "tijd": "07:45", "dag_0": "1", "dag_1": "1", "dag_2": "1", "dag_3": "1", "dag_4": "1", "kleur": "#FF0000",
             "leeg_versturen": "1", f"max_{i}": "3", "verplaats": f"{i}:op", "intro": "Goedemorgen {voornaam},", "kop": "PROMONITOR",
             "onderwerp": "Briefing {datum}", "afzender": "Promonitor"}
    assert klant.post(f"/overzichten/{oid}", headers=FORM, data=data, follow_redirects=False).headers["location"].endswith("#onderdelen")
    with _db() as c:
        nieuw = [x["code"] for x in c.execute("SELECT onderdelen FROM overzichten WHERE id = %s", (oid,)).fetchone()["onderdelen"]]
    assert nieuw.index("nieuws") == i - 1


def test_voorbeeld_en_versturen(klant, nep):
    verstuurd, gevraagd = nep
    with _db() as c:
        oid = c.execute("SELECT id FROM overzichten WHERE werkruimte = 'politiek' ORDER BY id LIMIT 1").fetchone()["id"]
        jeanette = c.execute("SELECT id FROM overzicht_ontvangers WHERE email = 'jeanette@voorbeeld.nl'").fetchone()["id"]
    # Voorbeeld voor Martijn: alles, met de taak en de link naar Buurmanitor; Claude schrijft niet mee.
    eigen = klant.get(f"/overzichten/{oid}/voorbeeld", headers=KOP).text
    assert "Privétaak" in eigen and "Bijdrage van Martijn" in eigen and "Nieuws 2" in eigen and "Nieuws 3" not in eigen
    assert "Hier schrijft Claude" in eigen and not gevraagd and '<base target="_blank">' in eigen
    assert "Raadsinformatie" in eigen  # niet gelukt (Promonitor draait hier niet): staat onderaan bij Martijn
    # Voorbeeld voor Jeanette: haar eigen deadline, geen taak, geen links naar Buurmanitor, geen foutmeldingen.
    hare = klant.get(f"/overzichten/{oid}/voorbeeld?ontvanger={jeanette}", headers=KOP).text
    assert "Bijdrage van Jeanette" in hare and "Goedemorgen Jeanette," in hare and "Privétaak" not in hare
    assert "buurmanitor.neighbour.nu" not in hare and "Niet gelukt" not in hare and "Antwoord dan op deze mail" in hare
    # Proef: alleen naar Martijn.
    r = klant.post(f"/overzichten/{oid}/stuur", headers=FORM, data={"wat": "proef"}, follow_redirects=False)
    assert "1+mail+verstuurd" in r.headers["location"], r.headers["location"]
    assert [m["aan"] for m in verstuurd] == ["martijn@voorbeeld.nl"] and verstuurd[0]["onderwerp"].startswith("[Proef] Briefing ")
    assert verstuurd[0]["van_naam"] == "Promonitor" and "Vandaag telt Nieuws 0, echt." in verstuurd[0]["html"]  # geen gedachtestreepje
    assert gevraagd and "Privétaak" not in gevraagd[0] and "Bijdrage van" not in gevraagd[0]  # alleen openbare onderdelen naar Claude
    with _db() as c:
        assert c.execute("SELECT laatst FROM overzichten WHERE id = %s", (oid,)).fetchone()["laatst"] is None  # een proef telt niet
    # Iedereen: twee mails, elk met de eigen inhoud.
    verstuurd.clear()
    r = klant.post(f"/overzichten/{oid}/stuur", headers=FORM, data={"wat": "iedereen"}, follow_redirects=False)
    assert "2+mails+verstuurd" in r.headers["location"]
    per = {m["aan"]: m for m in verstuurd}
    assert "Privétaak" in per["martijn@voorbeeld.nl"]["html"] and "Privétaak" not in per["jeanette@voorbeeld.nl"]["html"]
    assert "Bijdrage van Jeanette" in per["jeanette@voorbeeld.nl"]["tekst"] and len(gevraagd) == 1  # samenvatting maar één keer gevraagd
    pagina = klant.get(f"/overzichten/{oid}", headers=KOP).text
    assert "jeanette@voorbeeld.nl" in pagina and "Laatst verstuurd" in pagina
    # Een mislukte mail komt bij de ontvanger te staan.
    monkeypatch_fout = verzenden.stuur_mail
    try:
        verzenden.stuur_mail = lambda **kw: (_ for _ in ()).throw(RuntimeError("SMTP weigert"))
        with _db() as c:
            uitslag = verzenden.versturen(c, c.execute("SELECT * FROM overzichten WHERE id = %s", (oid,)).fetchone())
            assert "mislukt bij" in uitslag
            assert c.execute("SELECT fout FROM overzicht_ontvangers WHERE id = %s", (jeanette,)).fetchone()["fout"] == "SMTP weigert"
    finally:
        verzenden.stuur_mail = monkeypatch_fout


def test_planner_kopie_en_weg(klant, nep):
    verstuurd, _ = nep
    with _db() as c:
        oid = c.execute("SELECT id FROM overzichten WHERE werkruimte = 'politiek' ORDER BY id LIMIT 1").fetchone()["id"]
        nu = dt.datetime.now(config.TZ)
        start = (nu - dt.timedelta(minutes=10)).time()
        if start > nu.time():  # net na middernacht: dan kan het vandaag niet meer
            pytest.skip("rond middernacht")
        c.execute("UPDATE overzichten SET actief = true, tijd = %s, dagen = '{0,1,2,3,4,5,6}', laatst = NULL WHERE id = %s", (start, oid))
        uitslag = verzenden.taak_overzichten(c)
        assert "Promonitor ochtendbriefing" in uitslag and len(verstuurd) == 2
        assert verzenden.taak_overzichten(c) == "niets aan de beurt" and len(verstuurd) == 2  # niet nog eens vandaag
    r = klant.post(f"/overzichten/{oid}/kopie", headers=FORM, follow_redirects=False)
    nid = int(r.headers["location"].split("/")[2].split("?")[0])
    with _db() as c:
        k = c.execute("SELECT * FROM overzichten WHERE id = %s", (nid,)).fetchone()
        assert k["naam"].endswith("(kopie)") and not k["actief"]
        assert not c.execute("SELECT 1 FROM overzicht_ontvangers WHERE overzicht_id = %s", (nid,)).fetchone()
    klant.post(f"/overzichten/{nid}/weg", headers=FORM)
    with _db() as c:
        assert not c.execute("SELECT 1 FROM overzichten WHERE id = %s", (nid,)).fetchone()
