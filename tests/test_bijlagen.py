"""Regels met bijlagen: opslaan in Google Drive en doorsturen naar een vast adres.

Gmail, Outlook en Google Drive zijn nagebootst; de adressen zijn verzonnen. De database- en schermtests draaien alleen met
TEST_DATABASE_URL. Draaien in een eigen proces (zie tests/alles.sh).
"""
import base64
import datetime as dt
import email
import json
import os
import urllib.parse

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ.pop("POLITIEK_DATABASE_URL", None)
os.environ.pop("ZEROHERO_DATABASE_URL", None)
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"
os.environ["PORTAL_HOST"] = "buurmanitor.voorbeeld.nl"
os.environ["TOEGESTANE_HOSTS"] = "buurmanitor.voorbeeld.nl"
os.environ["KLUIS_SLEUTEL"] = "x5uAUzkCJN3hNJmsXBi9zLvb2Bw7l2X1z0hYwW9U6Ck="
os.environ["GOOGLE_CLIENT_ID"] = "123.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "GOCSPX-test"

import pytest  # noqa: E402

from app.bram import bronnen, drive, regels, sync  # noqa: E402
from app.kern import config  # noqa: E402

DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.voorbeeld.nl"}
PDF = b"%PDF-1.4 factuur"
MAIL = {"id": 1, "extern_id": "m1", "van_naam": "Energie BV", "van_adres": "facturen@energie.example", "onderwerp": "Uw factuur november",
        "ontvangen": dt.datetime(2026, 11, 15, 9, 30, tzinfo=config.TZ)}


# ---------------------------------------------------------------- zonder database

def test_drive_paden_en_namen():
    assert drive.delen("Administratie / Facturen/2026") == ("root", ["Administratie", "Facturen", "2026"])
    assert drive.delen("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMn?usp=sharing") == ("1AbCdEfGhIjKlMn", [])
    assert drive.delen("https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMn/{jaar}") == ("1AbCdEfGhIjKlMn", ["{jaar}"])
    assert drive.invullen("Facturen/{jaar}/{maand} {afzender}", {"jaar": "2026", "maand": "11", "afzender": "A/B"}) == "Facturen/2026/11 A B"
    assert drive.schone_naam('rare: naam?/"x".pdf') == "rare naam x .pdf" and drive.schone_naam("") == "bijlage"
    assert regels.afzender(MAIL) == "Energie BV" and regels.afzender({"van_adres": "nota@mail.leverancier.nl"}) == "leverancier"
    assert regels.bestandsnaam("Factuur 118.pdf", MAIL, "datum") == "2026-11-15 Energie BV Factuur 118.pdf"
    assert regels.bestandsnaam("Factuur 118.pdf", MAIL, "") == "Factuur 118.pdf"


def test_welke_bijlagen():
    lijst = [{"naam": "Factuur.pdf", "soort": "application/pdf", "grootte": 40_000},
             {"naam": "logo.png", "soort": "image/png", "grootte": 4_000},
             {"naam": "foto.jpg", "soort": "image/jpeg", "grootte": 900_000}]
    assert [x["naam"] for x in regels.kies_bijlagen(lijst, "")] == ["Factuur.pdf", "foto.jpg"]  # geen logootje uit de handtekening
    assert [x["naam"] for x in regels.kies_bijlagen(lijst, ".pdf")] == ["Factuur.pdf"]
    assert [x["naam"] for x in regels.kies_bijlagen(lijst, "logo, foto")] == ["logo.png", "foto.jpg"]


def test_formulier_en_beschrijving():
    basis = {"factuur": "1", "gelezen": "1"}
    v, a, fout = regels.uit_formulier(basis | {"drive": "4", "drive_map": "Administratie/Facturen/{jaar}", "bestandsnaam": "datum",
                                               "bijlage_filter": ".pdf", "doorsturen": "Inbox@Boekhouding.example"})
    assert not fout and a == {"gelezen": True, "drive": 4, "drive_map": "Administratie/Facturen/{jaar}", "bestandsnaam": "datum",
                              "bijlage_filter": ".pdf", "doorsturen": "inbox@boekhouding.example"}
    _, dan = regels.beschrijf(v, a)
    assert 'bijlagen met ".pdf" opslaan in Drive: Administratie/Facturen/{jaar} (met datum en afzender)' in dan
    assert "bijlagen doorsturen naar inbox@boekhouding.example" in dan
    assert "map in Google Drive" in regels.uit_formulier({"factuur": "1", "drive": "4"})[2]
    assert "Google-account" in regels.uit_formulier({"factuur": "1", "drive_map": "X"})[2]
    assert "geldige mailadressen" in regels.uit_formulier({"factuur": "1", "doorsturen": "geen adres"})[2]
    assert not regels.uit_formulier({"factuur": "1", "doorsturen": "a@b.example"})[2]  # alleen doorsturen is ook een actie
    _, a, _ = regels.uit_formulier(basis | {"bijlage_filter": ".pdf"})  # zonder bijlage-actie geen filter bewaren
    assert "bijlage_filter" not in a


def test_verwijderen():
    v, a, fout = regels.uit_formulier({"van": "nieuwsbrief", "verwijderen": "1", "map": "Facturen", "archiveren": "1", "label": "X"})
    assert not fout and a == {"verwijderen": True}  # verplaatsen en een label hebben dan geen zin meer
    assert regels.beschrijf(v, a)[1] == "verwijderen (naar de prullenbak)"
    assert not regels.uit_formulier({"van": "x", "verwijderen": "1"})[2]  # verwijderen alleen is genoeg als actie


def test_doorstuurmail():
    ruw = bronnen.doorstuur_mime("ik@voorbeeld.nl", ["inbox@boekhouding.example"], MAIL,
                                 [{"naam": "Factuur.pdf", "soort": "application/pdf", "data": PDF}], "Facturen")
    m = email.message_from_bytes(ruw)
    assert m["Subject"] == "Fwd: Uw factuur november" and m["To"] == "inbox@boekhouding.example" and m["From"] == "ik@voorbeeld.nl"
    delen = [p for p in m.walk() if p.get_filename()]
    assert [p.get_filename() for p in delen] == ["Factuur.pdf"] and delen[0].get_payload(decode=True) == PDF
    tekst = next(p for p in m.walk() if p.get_content_type() == "text/plain").get_payload(decode=True).decode()
    assert "regel 'Facturen'" in tekst and "Energie BV <facturen@energie.example>" in tekst and "15-11-2026" in tekst


def test_bijlagen_uit_gmail(monkeypatch):
    def b64(x):
        return base64.urlsafe_b64encode(x).decode().rstrip("=")
    bericht = {"payload": {"parts": [
        {"mimeType": "text/plain", "body": {"data": b64(b"hoi")}},
        {"filename": "Factuur.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "A1", "size": len(PDF)}},
        {"filename": "klein.txt", "mimeType": "text/plain", "body": {"data": b64(b"tekst"), "size": 5}},
        {"filename": "logo.png", "mimeType": "image/png", "headers": [{"name": "Content-Disposition", "value": "inline"}],
         "body": {"attachmentId": "A2", "size": 10}}]}}
    gevraagd = []

    def nep(tok, pad, **kw):
        gevraagd.append(pad)
        return bericht if "format=full" in pad else {"data": b64(PDF)}
    monkeypatch.setattr(bronnen, "_gmail", nep)
    uit = bronnen.gmail_bijlagen_data("tok", "m1")
    assert [(x["naam"], x["data"]) for x in uit] == [("Factuur.pdf", PDF), ("klein.txt", b"tekst")]
    assert gevraagd == ["/messages/m1?format=full", "/messages/m1/attachments/A1"]  # het ingesloten logo niet


def test_google_koppelen_met_drive():
    url = urllib.parse.urlparse(bronnen.google_url("s", drive=True, account="ik@voorbeeld.nl"))
    q = urllib.parse.parse_qs(url.query)
    assert bronnen.DRIVE_SCOPE in q["scope"][0] and q["include_granted_scopes"] == ["true"] and q["login_hint"] == ["ik@voorbeeld.nl"]
    assert bronnen.DRIVE_SCOPE not in urllib.parse.parse_qs(urllib.parse.urlparse(bronnen.google_url("s")).query)["scope"][0]


class NepDrive:
    """Google Drive als woordenboek: mappen en bestanden met een ouder."""

    def __init__(self):
        self.items = {"root": {"name": "Mijn Drive", "ouder": None, "map": True}}
        self.n = 0

    def http(self, url, headers=None, body=None, methode=None, **kw):  # pylint: disable=unused-argument
        delen = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(delen.query)
        if methode == "POST":
            self.n += 1
            nid = f"map{self.n}"
            self.items[nid] = {"name": body["name"], "ouder": body["parents"][0], "map": True}
            return {"id": nid}
        if delen.path.endswith("/files"):
            zoek = q["q"][0]
            ouder = zoek.split("'")[1]
            naam = zoek.split("name = '")[1].split("'")[0].replace("\\'", "'")
            gevonden = [{"id": i} for i, x in self.items.items() if x["ouder"] == ouder and x["name"] == naam
                        and (x["map"] or "mimeType" not in zoek)]
            return {"files": gevonden}
        return {"id": delen.path.rsplit("/", 1)[-1]}

    def upload(self, toegang, map_, naam, soort, data):  # pylint: disable=unused-argument
        self.n += 1
        self.items[f"f{self.n}"] = {"name": naam, "ouder": map_, "map": False, "data": data}
        return f"f{self.n}"

    def pad(self, i):
        delen = []
        while i and i != "root":
            delen.insert(0, self.items[i]["name"])
            i = self.items[i]["ouder"]
        return "/".join(delen)

    def bestanden(self):
        return sorted(f"{self.pad(x['ouder'])}/{x['name']}" for x in self.items.values() if not x["map"])


def test_drive_mappen_maken_en_niet_dubbel(monkeypatch):
    nep = NepDrive()
    monkeypatch.setattr(drive, "_http", nep.http)
    drive._mappen.clear()  # pylint: disable=protected-access
    nep.items["bestaand"] = {"name": "Administratie", "ouder": "root", "map": True}
    m = drive.map_id("tok", 1, "Administratie/Facturen/2026")
    assert nep.pad(m) == "Administratie/Facturen/2026" and nep.n == 2  # Administratie bestond al, de rest is gemaakt
    assert drive.map_id("tok", 1, "Administratie/Facturen/2026") == m and nep.n == 2  # onthouden
    assert drive.map_id("tok", 1, "https://drive.google.com/drive/folders/bestaand12345/Sub") != m


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
def opzet(klant, monkeypatch):  # pylint: disable=redefined-outer-name,unused-argument
    from app.clara import kluis  # pylint: disable=import-outside-toplevel
    nep = NepDrive()
    monkeypatch.setattr(drive, "_http", nep.http)
    monkeypatch.setattr(drive, "uploaden", nep.upload)
    drive._mappen.clear()  # pylint: disable=protected-access
    monkeypatch.setattr(sync, "toegang", lambda c, k: f"tok-{k['id']}")
    monkeypatch.setattr(bronnen, "gmail_bijlagen_data", lambda tok, mid: [
        {"naam": "Factuur 118.pdf", "soort": "application/pdf", "grootte": len(PDF), "data": PDF},
        {"naam": "logo.png", "soort": "image/png", "grootte": 900, "data": b"png"}])
    gewijzigd, verstuurd = [], []
    monkeypatch.setattr(bronnen, "gmail_wijzig", lambda tok, mid, erbij=(), eraf=(): gewijzigd.append((mid, list(erbij), list(eraf))))
    monkeypatch.setattr(bronnen, "gmail_labels", lambda tok: [("Label_1", "Facturen")])
    monkeypatch.setattr(bronnen, "gmail_stuur_mime", lambda tok, ruw: verstuurd.append((tok, email.message_from_bytes(ruw))))
    with _db() as c:
        c.execute("TRUNCATE post_koppelingen, post_regels CASCADE")
        g = c.execute("""INSERT INTO post_koppelingen (soort, naam, account, geheim, laatst, drive)
                         VALUES ('google', 'Werk', 'ik@voorbeeld.nl', %s, now(), true) RETURNING *""", (kluis.sluit("r"),)).fetchone()
        p = c.execute("""INSERT INTO post_koppelingen (soort, naam, account, geheim, laatst)
                         VALUES ('google', 'Privé', 'prive@voorbeeld.nl', %s, now()) RETURNING *""", (kluis.sluit("r2"),)).fetchone()
    yield {"nep": nep, "g": g, "p": p, "gewijzigd": gewijzigd, "verstuurd": verstuurd}
    with _db() as c:
        c.execute("TRUNCATE post_koppelingen, post_regels CASCADE")


def _bericht(c, k, extern="m1"):
    return c.execute("""INSERT INTO post_berichten (koppeling_id, extern_id, van_naam, van_adres, onderwerp, ontvangen, bijlagen)
                        VALUES (%s, %s, 'Energie BV', 'facturen@energie.example', 'Uw factuur november', %s, '["Factuur 118.pdf"]')
                        RETURNING *""", (k["id"], extern, MAIL["ontvangen"])).fetchone()


def test_regel_slaat_op_en_stuurt_door(klant, opzet):  # pylint: disable=redefined-outer-name
    g, nep = opzet["g"], opzet["nep"]
    # Het scherm: de keuze voor Drive, en een Gmail-account zonder toestemming zegt dat erbij.
    pagina = klant.get("/bram/regels", headers=KOP).text
    assert "Met de bijlagen" in pagina and "Privé (prive@voorbeeld.nl) (nog geen toegang tot Drive)" in pagina
    assert "Google Drive toestaan" in klant.get("/bram/instellingen", headers=KOP).text
    r = klant.post("/bram/regels", headers=FORM, follow_redirects=False, data={
        "naam": "Facturen", "factuur": "1", "gelezen": "1", "map": "Facturen", "drive": str(g["id"]),
        "drive_map": "Administratie/Facturen/{jaar}", "bestandsnaam": "datum", "doorsturen": "inbox@boekhouding.example"})
    assert r.status_code == 303 and "/probeer" in r.headers["location"]
    fout = klant.post("/bram/regels", headers=FORM, follow_redirects=False, data={"factuur": "1", "drive": str(g["id"]), "gelezen": "1"})
    assert "fout=" in fout.headers["location"] and "drive=" in fout.headers["location"]  # ingevulde velden blijven staan
    with _db() as c:
        regel = c.execute("SELECT * FROM post_regels").fetchone()
        b = _bericht(c, g)
        over, aantal = regels.toepassen(c, g, [b])
        log = c.execute("SELECT * FROM post_regel_log").fetchall()
    assert aantal == 1 and over == []
    assert nep.bestanden() == ["Administratie/Facturen/2026/2026-11-15 Energie BV Factuur 118.pdf"]  # het logo niet
    tok, m = opzet["verstuurd"][0]
    assert tok == f"tok-{g['id']}" and m["To"] == "inbox@boekhouding.example"
    assert [p.get_filename() for p in m.walk() if p.get_filename()] == ["Factuur 118.pdf"]
    assert opzet["gewijzigd"] == [("m1", ["Label_1"], ["UNREAD", "INBOX"])]  # pas na de bijlagen: gelezen en naar Facturen
    assert log[0]["fout"] == "" and log[0]["bericht_id"] == b["id"] and log[0]["link"].startswith("https://drive.google.com/drive/folders/")
    assert "1 bijlage in Drive (Administratie/Facturen/2026)" in log[0]["wat"] and "doorgestuurd naar inbox@boekhouding.example" in log[0]["wat"]
    assert "Open de map in Drive" in klant.get("/bram/regels", headers=KOP).text
    # Nog eens (bijvoorbeeld via Probeer > Nu toepassen): niet dubbel in Drive en niet nog eens doorsturen.
    with _db() as c:
        c.execute("UPDATE post_berichten SET status = 'open' WHERE id = %s", (b["id"],))
        gelukt, fouten = regels.nu_toepassen(c, regel, [b["id"]])
        laatste = c.execute("SELECT wat FROM post_regel_log ORDER BY id DESC LIMIT 1").fetchone()["wat"]
    assert gelukt == 1 and not fouten and len(nep.bestanden()) == 1 and len(opzet["verstuurd"]) == 1
    assert "0 bijlagen in Drive" in laatste and "1 stond(en) er al" in laatste and "al eerder doorgestuurd" in laatste


def test_drive_zonder_toestemming_laat_de_mail_staan(klant, opzet):  # pylint: disable=redefined-outer-name
    g, p = opzet["g"], opzet["p"]
    with _db() as c:
        regel = c.execute("""INSERT INTO post_regels (naam, voorwaarden, acties) VALUES ('Privé-bonnen', '{"factuur": true}', %s) RETURNING *""",
                          (json.dumps({"gelezen": True, "map": "Facturen", "drive": p["id"], "drive_map": "Bonnen"}),)).fetchone()
        b = _bericht(c, g, "m2")
        over, aantal = regels.toepassen(c, g, [b])
        log = c.execute("SELECT * FROM post_regel_log WHERE regel_id = %s", (regel["id"],)).fetchone()
        melding = c.execute("SELECT * FROM meldingen WHERE bron = 'bram' ORDER BY id DESC LIMIT 1").fetchone()
    assert aantal == 0 and [x["id"] for x in over] == [b["id"]] and opzet["gewijzigd"] == []  # niet gelezen, niet verplaatst
    assert "Privé heeft nog geen toestemming voor Google Drive" in log["fout"] and "Privé-bonnen" in melding["titel"]


def test_drive_toestaan_bij_koppelen(klant, opzet, monkeypatch):  # pylint: disable=redefined-outer-name
    p = opzet["p"]
    r = klant.post("/bram/koppel/google", headers=FORM, follow_redirects=False,
                   data={"drive": "1", "naam": p["naam"], "domein": "prive", "account": p["account"]})
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    assert bronnen.DRIVE_SCOPE in q["scope"][0] and q["login_hint"] == [p["account"]]
    monkeypatch.setattr(bronnen, "google_code", lambda code: {"refresh": "r3", "account": p["account"], "drive": True})
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k: "")
    t = klant.get(f"/clara/koppel/google/terug?code=x&state={q['state'][0]}", headers=KOP, follow_redirects=False)
    assert t.headers["location"].startswith("/bram/regels") and "Google+Drive" in t.headers["location"]
    with _db() as c:
        assert c.execute("SELECT drive FROM post_koppelingen WHERE id = %s", (p["id"],)).fetchone()["drive"] is True


def test_eerst_opslaan_dan_verwijderen(klant, opzet, monkeypatch):  # pylint: disable=redefined-outer-name
    g, nep = opzet["g"], opzet["nep"]
    weg = []
    monkeypatch.setattr(bronnen, "gmail_verwijder", lambda tok, mid: weg.append(mid))
    r = klant.post("/bram/regels", headers=FORM, follow_redirects=False, data={
        "naam": "Bonnen", "van": "energie", "gelezen": "1", "verwijderen": "1", "drive": str(g["id"]), "drive_map": "Bonnen"})
    assert "/probeer" in r.headers["location"]
    assert "verwijderen (naar de prullenbak)" in klant.get("/bram/regels", headers=KOP).text
    with _db() as c:
        b = _bericht(c, g, "m3")
        over, aantal = regels.toepassen(c, g, [b])
        rij = c.execute("SELECT status, gelezen FROM post_berichten WHERE id = %s", (b["id"],)).fetchone()
        wat = c.execute("SELECT wat FROM post_regel_log ORDER BY id DESC LIMIT 1").fetchone()["wat"]
    assert aantal == 1 and over == [] and weg == ["m3"] and nep.bestanden() == ["Bonnen/Factuur 118.pdf"]
    assert opzet["gewijzigd"] == [("m3", [], ["UNREAD"])] and rij == {"status": "weg", "gelezen": True}
    assert wat.endswith("gelezen, naar de prullenbak")
    # Lukt het opslaan niet, dan wordt de mail ook niet verwijderd.
    monkeypatch.setattr(drive, "uploaden", lambda *x: (_ for _ in ()).throw(bronnen.BronFout("Google Drive is vol.")))
    monkeypatch.setattr(bronnen, "gmail_bijlagen_data", lambda tok, mid: [{"naam": "Nieuw.pdf", "soort": "application/pdf", "grootte": 9, "data": PDF}])
    with _db() as c:
        b2 = _bericht(c, g, "m4")
        over, aantal = regels.toepassen(c, g, [b2])
    assert aantal == 0 and weg == ["m3"] and [x["id"] for x in over] == [b2["id"]]
