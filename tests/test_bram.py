"""Bram: mail uit Gmail en Outlook ophalen, sorteren (op regels en met Claude) en na een klik opruimen.

De Gmail-, Outlook- en Claude-antwoorden zijn nagebootst. De database- en schermtests draaien alleen met
TEST_DATABASE_URL (een lege, wegwerpbare database). Draaien in een eigen proces: python -m pytest tests/test_bram.py.
"""
import base64
import datetime as dt
import json
import os
import re
import urllib.parse

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="
os.environ["GOOGLE_CLIENT_ID"] = "123.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "GOCSPX-test"
os.environ["MS_CLIENT_ID"] = "00000000-0000-0000-0000-000000000001"
os.environ["MS_CLIENT_SECRET"] = "ms-geheim"
os.environ["ANTHROPIC_API_KEY"] = "sk-test"

import pytest  # noqa: E402

from app.bram import bronnen, sync, web  # noqa: E402
from app.kern import claude, config  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
UTC = dt.timezone.utc


def _b64(t):
    return base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")


def gmail_ruw(mid, van, onderwerp, tekst="", labels=("INBOX", "UNREAD"), uur=1, extra=()):
    return {"id": mid, "threadId": "t" + mid, "snippet": tekst[:60], "labelIds": list(labels),
            "internalDate": str(int((dt.datetime.now(UTC) - dt.timedelta(hours=uur)).timestamp() * 1000)),
            "payload": {"mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": van}, {"name": "To", "value": "martijn@neighbour.nu"},
                                    {"name": "Subject", "value": onderwerp}, *extra],
                        "parts": [{"mimeType": "text/html", "body": {"data": _b64(f"<p>{tekst}</p><style>x{{}}</style>")}},
                                  {"mimeType": "text/plain", "body": {"data": _b64(tekst)}}]}}


# ---------------------------------------------------------------- pure functies

def test_gmail_bericht_lezen(monkeypatch):
    ruw = gmail_ruw("m1", '"Jan de Vries" <Jan@Club.nl>', "Lezing in maart?", "Kun je op 12 maart een lezing geven?",
                    extra=[{"name": "List-Unsubscribe", "value": "<mailto:x>"}])
    monkeypatch.setattr(bronnen, "_http", lambda url, **kw: ruw)
    b = bronnen.gmail_bericht("tok", "m1", "martijn@neighbour.nu", met_tekst=True)
    assert b["van_naam"] == "Jan de Vries" and b["van_adres"] == "jan@club.nl" and b["onderwerp"] == "Lezing in maart?"
    assert not b["gelezen"] and b["nieuwsbrief"] and b["tekst"] == "Kun je op 12 maart een lezing geven?"
    assert b["link"].startswith("https://mail.google.com/mail/u/?authuser=martijn%40neighbour.nu#all/m1")
    assert b["labels"] == ["INBOX", "UNREAD"]


def test_html_naar_tekst():
    assert bronnen.html_naar_tekst("<style>p{}</style><p>Hoi&nbsp;Martijn</p><p>Groet</p>") == "Hoi\xa0Martijn\n Groet"


def test_ms_bericht_lezen():
    b = bronnen.ms_bericht({"id": "AAA=", "conversationId": "c1", "subject": "Factuur oktober", "isRead": True,
                            "from": {"emailAddress": {"name": "Boekhouding", "address": "Facturen@Upperspire.com"}},
                            "toRecipients": [{"emailAddress": {"address": "m@us.com"}}], "receivedDateTime": "2026-10-30T09:00:00Z",
                            "bodyPreview": "In de bijlage", "webLink": "https://outlook.office.com/x", "categories": ["Blauw"],
                            "inferenceClassification": "other", "body": {"content": "In de bijlage de factuur."}})
    assert b["gelezen"] and b["nieuwsbrief"] and b["labels"] == ["Blauw"] and b["tekst"] == "In de bijlage de factuur."
    assert b["ontvangen"] == dt.datetime(2026, 10, 30, 9, tzinfo=UTC) and b["aan"] == "m@us.com"


def test_sorteren_op_regels():
    basis = {"onderwerp": "Hoi", "van_adres": "piet@example.nl", "nieuwsbrief": False, "bron_categorie": ""}
    assert sync.op_regels(basis)["categorie"] == "lezen"
    assert sync.op_regels(basis | {"nieuwsbrief": True})["actie"] == "archiveren"
    assert sync.op_regels(basis | {"bron_categorie": "promotions"})["categorie"] == "nieuwsbrief"
    assert sync.op_regels(basis | {"van_adres": "no-reply@ns.nl"})["categorie"] == "automatisch"
    assert sync.op_regels(basis | {"van_adres": "notifications@github.com"})["categorie"] == "automatisch"
    assert sync.op_regels(basis | {"van_adres": "noreen@example.nl"})["categorie"] == "lezen"


def test_oordeel_van_claude_controleren():
    tekst = 'Hier: [{"nr": 1, "categorie": "antwoord", "prioriteit": "hoog", "regel": "Club vraagt een lezing — 12 maart.", ' \
            '"voorstel": "Antwoorden", "actie": "bewaren"}, {"nr": 2, "categorie": "onzin", "regel": "x"}, ' \
            '{"nr": 3, "categorie": "info", "prioriteit": "superhoog", "regel": "Factuur", "actie": "weg"}, {"nr": 9, "categorie": "info", "regel": "x"}]'
    o = sync.lees_oordeel(tekst, 3)
    assert set(o) == {1, 3}
    assert o[1]["regel"] == "Club vraagt een lezing, 12 maart." and o[1]["prioriteit"] == "hoog"
    assert o[3]["prioriteit"] == "normaal" and o[3]["actie"] == "bewaren"
    assert sync.lees_oordeel("geen json", 2) == {}


def test_groepen():
    b = {"categorie": "lezen", "prioriteit": "normaal", "actie": "bewaren"}
    assert web.groep(b) == "lezen"
    assert web.groep(b | {"categorie": "antwoord"}) == "actie"
    assert web.groep(b | {"prioriteit": "hoog"}) == "actie"
    assert web.groep(b | {"categorie": "nieuwsbrief"}) == "weg"
    assert web.groep(b | {"categorie": "info", "actie": "archiveren"}) == "weg"


def test_koppeladressen_en_toestemming():
    g = urllib.parse.parse_qs(urllib.parse.urlsplit(bronnen.google_url("s")).query)
    assert g["scope"] == ["openid email https://www.googleapis.com/auth/gmail.modify"]
    assert g["redirect_uri"] == ["https://buurmanitor.neighbour.nu/clara/koppel/google/terug"]
    m = urllib.parse.parse_qs(urllib.parse.urlsplit(bronnen.ms_url("s")).query)
    assert "Mail.ReadWrite" in m["scope"][0] and "Mail.Send" not in m["scope"][0] and "Calendars" not in m["scope"][0]
    assert m["redirect_uri"] == ["https://buurmanitor.neighbour.nu/clara/koppel/microsoft/terug"]


# ---------------------------------------------------------------- database en schermen

@pytest.fixture(scope="module")
def klant():
    if not DB:
        pytest.skip("geen TEST_DATABASE_URL")
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel
    from app import main  # pylint: disable=import-outside-toplevel
    with TestClient(main.app) as k:
        from app.kern import db  # pylint: disable=import-outside-toplevel
        with db.pool.connection() as c:
            c.execute("TRUNCATE post_koppelingen, oauth_staat CASCADE")
            c.execute("DELETE FROM meldingen WHERE bron = 'bram'")
        yield k


class NepGmail:
    """Een nagebootste Gmail-inbox."""

    def __init__(self):
        self.inbox = {}
        self.gewijzigd = []

    def zet(self, ruw):
        self.inbox[ruw["id"]] = ruw

    def installeer(self, monkeypatch):
        monkeypatch.setattr(bronnen, "google_code", lambda code: {"refresh": "1//mail", "account": "martijn@neighbour.nu"})
        monkeypatch.setattr(bronnen, "google_toegang", lambda refresh: ("tok", 3600, None))

        def ids(tok, extra=""):
            return [m for m, r in sorted(self.inbox.items(), key=lambda x: -int(x[1]["internalDate"]))
                    if "is:unread" not in extra or "UNREAD" in r["labelIds"]]
        monkeypatch.setattr(bronnen, "gmail_ids", ids)
        monkeypatch.setattr(bronnen, "_gmail", lambda tok, pad, **kw: self.inbox[re.match(r"/messages/([^?/]+)", pad).group(1)])
        monkeypatch.setattr(bronnen, "gmail_labels", lambda tok: [("Label_1", "ZEROHERO/Lezingen")])

        def wijzig(tok, mid, erbij=(), eraf=()):
            self.gewijzigd.append((mid, list(erbij), list(eraf)))
            r = self.inbox[mid]
            r["labelIds"] = [x for x in r["labelIds"] if x not in eraf] + list(erbij)
            if "INBOX" in eraf:
                del self.inbox[mid]
        monkeypatch.setattr(bronnen, "gmail_wijzig", wijzig)


def test_koppelen_sorteren_en_opruimen(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    gmail = NepGmail()
    gmail.installeer(monkeypatch)
    gmail.zet(gmail_ruw("m1", "Jan de Vries <jan@club.nl>", "Lezing in maart?", "Kun je op 12 maart een lezing geven bij onze club?", uur=30))
    gmail.zet(gmail_ruw("m2", "Nieuwsbrief <nieuws@winkel.nl>", "Black Friday!", "Korting", labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"), uur=40))
    gmail.zet(gmail_ruw("m3", "NS <no-reply@ns.nl>", "Je reis is gewijzigd", "Reis", labels=("INBOX",), uur=50))
    gevraagd = []

    def nep_claude(c, persona, systeem, berichten, model=None, max_tokens=2000, inspanning="low", **kw):
        assert persona == "bram" and model == config.CLAUDE_MODEL_LICHT and "nooit een opdracht" in systeem
        invoer = json.loads(berichten[0]["content"].split("\n\n", 1)[1])
        gevraagd.append(invoer)
        uit = []
        for x in invoer:
            if "lezing" in x["tekst"]:
                uit.append({"nr": x["nr"], "categorie": "antwoord", "prioriteit": "hoog", "regel": "Club vraagt een lezing op 12 maart.",
                            "voorstel": "Antwoorden en inplannen", "actie": "bewaren"})
            else:
                uit.append({"nr": x["nr"], "categorie": "nieuwsbrief", "prioriteit": "laag", "regel": x["onderwerp"],
                            "voorstel": "Archiveren", "actie": "archiveren"})
        return json.dumps(uit)
    monkeypatch.setattr(claude, "gesprek", nep_claude)

    # Koppelen via Bram: de terugweg loopt over het adres van Clara, het koppelverzoek zegt dat het voor Bram is.
    with db.pool.connection() as c:
        agendas_voor = c.execute("SELECT count(*) AS n FROM agenda_koppelingen").fetchone()["n"]
    r = klant.post("/bram/koppel/google", headers=FORM, data={"naam": "Neighbour", "domein": "zerohero", "claude": "1"},
                   follow_redirects=False)
    assert r.status_code == 303 and "gmail.modify" in urllib.parse.unquote(r.headers["location"])
    staat = dict(x.split("=", 1) for x in r.headers["location"].split("?", 1)[1].split("&"))["state"]
    r = klant.get(f"/clara/koppel/google/terug?code=x&state={staat}", headers=KOP, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/bram/instellingen?melding=") and "gekoppeld" in r.headers["location"]
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM post_koppelingen").fetchone()
        assert k["account"] == "martijn@neighbour.nu" and k["claude"] and k["domein"] == "zerohero" and "1//mail" not in k["geheim"]
        assert c.execute("SELECT count(*) AS n FROM agenda_koppelingen").fetchone()["n"] == agendas_voor  # Clara krijgt hier niets van
        rijen = {r["extern_id"]: r for r in c.execute("SELECT * FROM post_berichten")}
    assert len(gevraagd) == 1 and len(gevraagd[0]) == 3 and "12 maart" in gevraagd[0][0]["tekst"]
    assert rijen["m1"]["categorie"] == "antwoord" and rijen["m1"]["beoordeeld_door"] == "claude" and not rijen["m1"]["gelezen"]
    assert rijen["m3"]["gelezen"] and rijen["m2"]["actie"] == "archiveren"
    with db.pool.connection() as c:  # bij het eerste koppelen geen meldingen
        assert not c.execute("SELECT 1 FROM meldingen WHERE bron = 'bram'").fetchone()

    pagina = klant.get("/bram", headers=KOP).text
    actie = pagina.split('id="actie"')[1].split('id="lezen"')[0]
    weg = pagina.split('id="weg"')[1].split("<aside")[0]
    assert "Club vraagt een lezing op 12 maart." in actie and "Antwoorden en inplannen" in actie and "Black Friday" not in actie
    assert "Black Friday" in weg and "Je reis is gewijzigd" in weg and 'form="kan-weg" checked' in weg
    assert "ZEROHERO/Lezingen" in pagina  # labels om te kiezen

    # Nieuw bericht na het koppelen, hoog: een melding. Zonder Claude op regels.
    gmail.zet(gmail_ruw("m4", "Wethouder <wethouder@helmond.nl>", "Morgen overleg?", "Kun je morgen om 10 uur bij de lezing praten?", uur=1))
    with db.pool.connection() as c:
        c.execute("UPDATE post_koppelingen SET laatst = now() - interval '10 minutes'")
        assert "1 nieuw" in sync.taak_post(c)
        m = c.execute("SELECT * FROM meldingen WHERE bron = 'bram'").fetchall()
        assert len(m) == 1 and "Wethouder" in m[0]["titel"] and m[0]["link"].startswith("/bram#b")
        c.execute("UPDATE post_koppelingen SET claude = false")
    gmail.zet(gmail_ruw("m5", "Piet <piet@example.nl>", "Foto's van zaterdag", "Kijk maar", uur=1))
    with db.pool.connection() as c:
        sync.taak_post(c)
        r5 = c.execute("SELECT * FROM post_berichten WHERE extern_id = 'm5'").fetchone()
        assert r5["beoordeeld_door"] == "regels" and r5["categorie"] == "lezen" and len(gevraagd) == 2
        ids = {r["extern_id"]: r["id"] for r in c.execute("SELECT id, extern_id FROM post_berichten")}

    # Na een klik: gelezen, label, archiveren. Zonder herkomst geweigerd.
    assert klant.post(f"/bram/bericht/{ids['m1']}/gelezen", headers=KOP).status_code == 403
    r = klant.post(f"/bram/bericht/{ids['m1']}/gelezen", headers=FORM, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"#b{ids['m1']}") and ("m1", [], ["UNREAD"]) in gmail.gewijzigd
    r = klant.post(f"/bram/bericht/{ids['m1']}/label", headers=FORM, data={"label": "Label_1"}, follow_redirects=False)
    assert ("m1", ["Label_1"], []) in gmail.gewijzigd
    assert "fout=" in klant.post(f"/bram/bericht/{ids['m1']}/label", headers=FORM, data={"label": "Label_99"}, follow_redirects=False).headers["location"]
    r = klant.post("/bram/archiveren", headers=FORM, data={"bericht": [str(ids["m2"]), str(ids["m3"])]}, follow_redirects=False)
    assert "2+berichten+gearchiveerd" in r.headers["location"] and "m2" not in gmail.inbox and "m3" not in gmail.inbox
    assert "fout=" in klant.post("/bram/archiveren", headers=FORM, data={}, follow_redirects=False).headers["location"]
    with db.pool.connection() as c:
        st = {r["extern_id"]: r for r in c.execute("SELECT * FROM post_berichten")}
        assert st["m1"]["gelezen"] and "Label_1" in st["m1"]["labels"]
        assert st["m2"]["status"] == st["m3"]["status"] == "weg"
    # In Gmail zelf gearchiveerd: verdwijnt hier vanzelf. Terug in de inbox: weer open.
    weggezet = gmail.inbox.pop("m5")
    with db.pool.connection() as c:
        sync.taak_post(c)
        assert c.execute("SELECT status FROM post_berichten WHERE extern_id = 'm5'").fetchone()["status"] == "weg"
        gmail.zet(weggezet)
        sync.taak_post(c)
        assert c.execute("SELECT status FROM post_berichten WHERE extern_id = 'm5'").fetchone()["status"] == "open"
    pagina = klant.get("/bram", headers=KOP).text
    assert "Black Friday" not in pagina and "Foto&#39;s van zaterdag" in pagina

    # Vandaag en Iris weten ervan.
    vandaag = klant.get("/", headers=KOP).text
    assert 'href="/bram#actie">2 berichten</a> vragen om actie' in vandaag
    from app import iris  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        assert "Club vraagt een lezing op 12 maart." in iris.context(c)

    # Instellingen: Claude uit/aan, ontkoppelen.
    pagina = klant.get("/bram/instellingen", headers=KOP).text
    assert "Neighbour" in pagina and "Claude mag de inhoud lezen" in pagina
    kid = k["id"]
    klant.post(f"/bram/koppeling/{kid}", headers=FORM, data={"naam": "Neighbour", "domein": "prive", "actief": "1", "claude": "1"})
    with db.pool.connection() as c:
        assert c.execute("SELECT claude, domein FROM post_koppelingen WHERE id = %s", (kid,)).fetchone() == {"claude": True, "domein": "prive"}
    r = klant.post(f"/bram/koppeling/{kid}", headers=FORM, data={"actie": "weg"}, follow_redirects=False)
    assert "ontkoppeld" in r.headers["location"]
    with db.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM post_berichten").fetchone()


def test_outlook_archiveren_krijgt_nieuw_id(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(bronnen, "ms_code", lambda code: {"refresh": "ms-ref", "account": "martijn@upperspire.com", "tenant": "t1"})
    monkeypatch.setattr(bronnen, "ms_toegang", lambda refresh, tenant: ("tok", 3600, "ms-ref-2"))
    inbox = [bronnen.ms_bericht({"id": "A1", "subject": "Planning sprint", "isRead": False, "receivedDateTime": "2026-10-31T08:00:00Z",
                                 "from": {"emailAddress": {"name": "Eva", "address": "eva@upperspire.com"}}, "categories": []})]
    monkeypatch.setattr(bronnen, "ms_berichten", lambda tok, met_tekst=False, nu=None: [dict(b) for b in inbox])
    monkeypatch.setattr(bronnen, "ms_tekst", lambda tok, mid: pytest.fail("Claude staat uit: geen tekst ophalen"))
    verplaatst = []
    monkeypatch.setattr(bronnen, "ms_archiveer", lambda tok, mid: verplaatst.append(mid) or "A1-archief")
    r = klant.post("/bram/koppel/microsoft", headers=FORM, data={"naam": "Upper Spire", "domein": "upperspire"}, follow_redirects=False)
    staat = dict(x.split("=", 1) for x in r.headers["location"].split("?", 1)[1].split("&"))["state"]
    r = klant.get(f"/clara/koppel/microsoft/terug?code=x&state={staat}", headers=KOP, follow_redirects=False)
    assert r.headers["location"].startswith("/bram/instellingen?melding=")
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM post_koppelingen WHERE soort = 'microsoft'").fetchone()
        assert not k["claude"] and k["tenant"] == "t1"
        b = c.execute("SELECT * FROM post_berichten WHERE koppeling_id = %s", (k["id"],)).fetchone()
        assert b["beoordeeld_door"] == "regels"
    r = klant.post(f"/bram/bericht/{b['id']}/archiveren", headers=FORM, follow_redirects=False)
    assert "gearchiveerd" in r.headers["location"] and verplaatst == ["A1"]
    with db.pool.connection() as c:
        b2 = c.execute("SELECT * FROM post_berichten WHERE id = %s", (b["id"],)).fetchone()
        assert b2["extern_id"] == "A1-archief" and b2["status"] == "weg"
        from app.clara import kluis  # pylint: disable=import-outside-toplevel
        assert kluis.open_(c.execute("SELECT geheim FROM post_koppelingen WHERE id = %s", (k["id"],)).fetchone()["geheim"]) == "ms-ref-2"  # nieuw refresh-token bewaard


def test_fout_bij_ophalen_wordt_gemeld(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    from app.clara import kluis  # pylint: disable=import-outside-toplevel

    def verlopen(refresh):
        raise bronnen.OpnieuwKoppelen("De toegang tot Gmail is verlopen of ingetrokken. Koppel deze agenda opnieuw.")
    monkeypatch.setattr(bronnen, "google_toegang", verlopen)
    with db.pool.connection() as c:
        c.execute("DELETE FROM meldingen WHERE bron = 'bram'")
        c.execute("DELETE FROM post_koppelingen")
        c.execute("INSERT INTO post_koppelingen (soort, naam, account, geheim) VALUES ('google', 'Privé', 'x@neighbour.nu', %s)",
                  (kluis.sluit("r"),))
        uitslag = sync.taak_post(c)
        assert "verlopen" in uitslag
        assert c.execute("SELECT count(*) AS n FROM meldingen WHERE bron = 'bram'").fetchone()["n"] == 1
    assert "verlopen" in klant.get("/bram", headers=KOP).text
