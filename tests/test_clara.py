"""Clara: analyse, bronnen (met nagebootste antwoorden van Google, Microsoft en ICS), kluis, en de schermen.

De database- en schermtests draaien alleen met TEST_DATABASE_URL (een lege, wegwerpbare database).
Draaien in een eigen proces: python -m pytest tests/test_clara.py (zie tests/alles.sh).
"""
import datetime as dt
import io
import json
import os
import urllib.error

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

import pytest  # noqa: E402

from app.clara import analyse, bronnen, gesprek, kluis  # noqa: E402
from app.kern import config  # noqa: E402

TZ = config.TZ
DB = bool(os.environ.get("TEST_DATABASE_URL"))
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
FORM = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}


def t(dag, uur, minuut=0):
    return dt.datetime(2026, 11, dag, uur, minuut, tzinfo=TZ)


def afspraak(titel, start, eind, **kw):
    return bronnen.item(titel=titel, start=start, eind=eind, extern_id=kw.pop("extern_id", titel), koppeling=kw.pop("koppeling", "A"),
                        soort=kw.pop("soort", "google"), domein=kw.pop("domein", "prive"), **kw)


# ---------------------------------------------------------------- analyse

def test_conflicten_alleen_bij_bezette_tijd():
    a = afspraak("Commissie", t(3, 19, 30), t(3, 22))
    b = afspraak("Ouderavond", t(3, 20), t(3, 21))
    vrij = afspraak("Werkplek", t(3, 20), t(3, 21), vrij=True)
    nee = afspraak("Webinar", t(3, 20), t(3, 21), antwoord="afgewezen")
    heledag = afspraak("Verjaardag", t(3, 0), t(4, 0), heledag=True)
    aansluitend = afspraak("Na", t(3, 22), t(3, 23))
    c = analyse.conflicten([a, b, vrij, nee, heledag, aansluitend])
    assert len(c) == 1 and {c[0]["a"]["titel"], c[0]["b"]["titel"]} == {"Commissie", "Ouderavond"}
    assert c[0]["van"] == t(3, 20) and c[0]["tot"] == t(3, 21)


def test_samenvoegen_zelfde_uitnodiging_en_raadsagenda():
    x1 = afspraak("Overleg", t(4, 10), t(4, 11), ical_uid="ABC@x", koppeling="Google")
    x2 = afspraak("Overleg", t(4, 10), t(4, 11), ical_uid="abc@X", koppeling="IV-professional")
    raad_dubbel = afspraak("Commissieavond", t(4, 19, 30), t(4, 22, 30), soort="raad", domein="politiek", koppeling="Raadsagenda")
    gemeente = afspraak("Commissie Middelen", t(4, 19, 30), t(4, 22), domein="politiek", koppeling="Gemeenteraad")
    raad_alleen = afspraak("Raadsvergadering", t(10, 19, 30), t(10, 23, 30), soort="raad", domein="politiek", koppeling="Raadsagenda")
    uit = analyse.samenvoegen([x2, x1, raad_dubbel, gemeente, raad_alleen])
    titels = [a["titel"] for a in uit]
    assert titels.count("Overleg") == 1 and "Commissieavond" not in titels and "Raadsvergadering" in titels
    overleg = next(a for a in uit if a["titel"] == "Overleg")
    assert overleg["ook"] == ["IV-professional"] or overleg["ook"] == ["Google"]


def test_krappe_overgang_alleen_tussen_verschillende_plekken():
    a = afspraak("Upper Spire", t(5, 9), t(5, 11), locatie="Utrecht, kantoor")
    b = afspraak("Lunch", t(5, 11, 15), t(5, 12), locatie="Helmond centrum")
    c = afspraak("Teams-overleg", t(5, 12), t(5, 13), locatie="Microsoft Teams-vergadering")
    d = afspraak("Zelfde plek", t(5, 13), t(5, 14), locatie="Helmond Centrum")
    uit = analyse.overgangen([a, b, c, d], reistijd=30)
    assert len(uit) == 1 and uit[0]["van"]["titel"] == "Upper Spire" and uit[0]["minuten"] == 15


def test_vrije_blokken_met_lucht_en_kwartieren():
    inst = {"werkdagen": [0, 1, 2, 3, 4], "begin": "09:00", "eind": "17:00", "avond": False, "buffer": 15}
    lijst = [afspraak("Ochtend", t(2, 9), t(2, 10, 50)), afspraak("Middag", t(2, 13), t(2, 17))]
    # Maandag 2 november. 11:05 (10:50 + 15 lucht) wordt 11:15; tot 12:45 (13:00 - 15) is 90 minuten: geen 2 uur.
    assert analyse.vrije_blokken(lijst, t(2, 8), 1, 120, inst, TZ) == []
    kort = analyse.vrije_blokken(lijst, t(2, 8), 1, 60, inst, TZ)
    assert kort[0]["start"] == t(2, 11, 15) and kort[0]["ruimte"] == 90
    # Zaterdag geen werkdag, behalve met avonden aan.
    assert analyse.vrije_blokken([], t(7, 8), 1, 60, inst, TZ) == []
    avond = analyse.vrije_blokken([], t(7, 8), 1, 60, {**inst, "avond": True}, TZ)
    assert avond[0]["start"] == t(7, 19)


def test_uitnodigingen():
    nu = t(3, 12)
    lijst = [afspraak("Open", t(4, 10), t(4, 11), antwoord="geen"), afspraak("Al geweest", t(2, 10), t(2, 11), antwoord="geen"),
             afspraak("Ja", t(4, 12), t(4, 13), antwoord="geaccepteerd")]
    assert [a["titel"] for a in analyse.uitnodigingen(lijst, nu)] == ["Open"]


# ---------------------------------------------------------------- bronnen

def test_google_item_tijden_en_antwoorden():
    e = {"id": "e1", "iCalUID": "u1@google.com", "summary": "Overleg", "location": "Helmond", "status": "confirmed",
         "start": {"dateTime": "2026-11-04T10:00:00+01:00"}, "end": {"dateTime": "2026-11-04T11:00:00+01:00"},
         "organizer": {"email": "jan@x.nl"}, "attendees": [{"email": "martijn@neighbour.nu", "self": True, "responseStatus": "needsAction"}],
         "htmlLink": "https://calendar.google.com/x"}
    i = bronnen.google_item(e, "primary")
    assert i["start"] == t(4, 10) and i["antwoord"] == "geen" and i["organisator"] == "jan@x.nl" and not i["heledag"]
    heledag = bronnen.google_item({"id": "e2", "summary": "Vrij", "start": {"date": "2026-11-06"}, "end": {"date": "2026-11-07"},
                                   "transparency": "transparent"}, "primary")
    assert heledag["heledag"] and heledag["vrij"] and heledag["antwoord"] == "eigen" and heledag["start"] == t(6, 0)
    eigen = bronnen.google_item({**e, "organizer": {"email": "m", "self": True}}, "primary")
    assert eigen["antwoord"] == "organisator"


def test_ms_item_tijdzone_en_status():
    e = {"id": "AAMk1", "iCalUId": "040000008200E", "subject": "Releasetest", "location": {"displayName": "Microsoft Teams-vergadering"},
         "start": {"dateTime": "2026-11-04T09:30:00.0000000", "timeZone": "Europe/Amsterdam"},
         "end": {"dateTime": "2026-11-04T10:30:00.0000000", "timeZone": "Europe/Amsterdam"}, "isAllDay": False,
         "showAs": "tentative", "isCancelled": False, "responseStatus": {"response": "tentativelyAccepted"},
         "organizer": {"emailAddress": {"name": "Upper Spire", "address": "x@upperspire.nl"}}, "isOrganizer": False,
         "isOnlineMeeting": True, "webLink": "https://outlook.office365.com/x"}
    i = bronnen.ms_item(e, "cal1")
    assert i["start"] == t(4, 9, 30) and i["antwoord"] == "voorlopig" and i["status"] == "voorlopig" and i["online"]
    assert bronnen.ms_item({**e, "responseStatus": {"response": "notResponded"}}, "cal1")["antwoord"] == "geen"
    assert bronnen.ms_item({**e, "showAs": "free"}, "cal1")["vrij"]


ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0//EN
BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16011028T030000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010325T020000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:fractie-1
SUMMARY:Fractievergadering
LOCATION:Stadhuis Helmond
DTSTART;TZID=W. Europe Standard Time:20261102T200000
DTEND;TZID=W. Europe Standard Time:20261102T220000
RRULE:FREQ=WEEKLY;COUNT=4
X-MICROSOFT-CDO-BUSYSTATUS:BUSY
END:VEVENT
BEGIN:VEVENT
UID:vrij-1
SUMMARY:Bezet
DTSTART;VALUE=DATE:20261105
DTEND;VALUE=DATE:20261106
X-MICROSOFT-CDO-BUSYSTATUS:FREE
END:VEVENT
BEGIN:VEVENT
UID:weg-1
SUMMARY:Geannuleerd
STATUS:CANCELLED
DTSTART:20261103T090000Z
DTEND:20261103T100000Z
END:VEVENT
END:VCALENDAR
"""


def test_ics_herhaling_tijdzone_en_bezet_status():
    lijst = bronnen.ics_items(ICS, t(1, 0), t(30, 0))
    fractie = [i for i in lijst if i["titel"] == "Fractievergadering"]
    assert len(fractie) == 4 and fractie[0]["start"] == t(2, 20) and fractie[0]["eind"] == t(2, 22)
    assert len({i["extern_id"] for i in fractie}) == 4  # elke herhaling een eigen sleutel
    vrij = next(i for i in lijst if i["ical_uid"] == "vrij-1")
    assert vrij["heledag"] and vrij["vrij"]
    assert not any(i["titel"] == "Geannuleerd" for i in lijst)


def test_voorstel_uit_antwoord():
    tekst = ('Dinsdag past het best.\n<voorstel>{"titel": "Schrijven keepersverhaal", "start": "2026-11-03T09:00", '
             '"eind": "2026-11-03T11:00", "domein": "zerohero"}</voorstel>\n<voorstel>{"kapot": 1}</voorstel>')
    schoon, vs = gesprek.voorstellen_uit(tekst)
    assert schoon == "Dinsdag past het best." and len(vs) == 1
    assert vs[0]["start"] == t(3, 9) and vs[0]["domein"] == "zerohero"


def test_kluis_heen_en_terug():
    geheim = kluis.sluit("1//refresh-token")
    assert geheim != "1//refresh-token" and kluis.open_(geheim) == "1//refresh-token"


def _http_fout(monkeypatch, code, body):
    def nep(verzoek, timeout=30):
        raise urllib.error.HTTPError(verzoek.full_url, code, "fout", {}, io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr(bronnen.urllib.request, "urlopen", nep)


def test_foutmeldingen_in_gewone_taal(monkeypatch):
    _http_fout(monkeypatch, 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    with pytest.raises(bronnen.OpnieuwKoppelen):
        bronnen.google_toegang("x")
    _http_fout(monkeypatch, 400, {"error": "invalid_grant", "error_description": "AADSTS65001: The user or administrator has not consented"})
    with pytest.raises(bronnen.BronFout, match="beheerder"):
        bronnen.ms_toegang("x", "tenant", False)
    _http_fout(monkeypatch, 403, {"error": {"code": 403, "message": "Insufficient Permission"}})
    with pytest.raises(bronnen.BronFout, match="Insufficient Permission"):
        bronnen.google_agendas("tok")


def test_koppel_adressen():
    url = bronnen.google_url("staat123")
    assert "access_type=offline" in url and "redirect_uri=https%3A%2F%2Fbuurmanitor.neighbour.nu%2Fclara%2Fkoppel%2Fgoogle%2Fterug" in url
    assert "calendar.events" in url and "gmail" not in url
    ms = bronnen.ms_url("s", alleen_lezen=True)
    assert "Calendars.Read" in ms and "Calendars.ReadWrite" not in ms and "/organizations/" in ms
    assert "adminconsent" in bronnen.ms_beheerder_url()


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
            c.execute("TRUNCATE agenda_koppelingen, agenda_voorstellen, oauth_staat CASCADE")
            c.execute("DELETE FROM meldingen WHERE bron = 'clara'")
            c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'clara%%'")
        yield k


def _nu_dag(dagen, uur, minuut=0):
    d = dt.datetime.now(TZ).date() + dt.timedelta(days=dagen)
    return dt.datetime.combine(d, dt.time(uur, minuut), tzinfo=TZ)


def test_lege_schermen(klant):
    r = klant.get("/clara", headers=KOP)
    assert r.status_code == 200 and "nog geen agenda" in r.text
    r = klant.get("/clara/koppelingen", headers=KOP)
    assert r.status_code == 200 and "Google-account koppelen" in r.text and "adminconsent" in r.text
    assert "Nog geen agenda" in klant.get("/", headers=KOP).text


def test_google_koppelen_ophalen_en_inplannen(klant, monkeypatch):
    morgen = _nu_dag(1, 10)
    gemaakt, beantwoord = [], []
    monkeypatch.setattr(bronnen, "google_code", lambda code: {"refresh": "1//ref", "toegang": "tok", "verloopt": 3600,
                                                               "account": "martijn@neighbour.nu"})
    monkeypatch.setattr(bronnen, "google_toegang", lambda refresh: ("tok-" + refresh, 3600, None))
    monkeypatch.setattr(bronnen, "google_agendas", lambda tok: [
        {"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True},
        {"id": "zh", "naam": "ZEROHERO", "primair": False, "schrijven": True}])

    def items(tok, agenda_id, van, tot):
        assert tok == "tok-1//ref"
        if agenda_id != "primary":
            return []
        return [bronnen.item(agenda_id="primary", extern_id="a1", ical_uid="a1@g", titel="Overleg ZEROHERO", start=morgen,
                             eind=morgen + dt.timedelta(hours=1)),
                bronnen.item(agenda_id="primary", extern_id="a2", titel="Tandarts", start=morgen + dt.timedelta(minutes=30),
                             eind=morgen + dt.timedelta(hours=1, minutes=30)),
                bronnen.item(agenda_id="primary", extern_id="a3", titel="Keepersavond", start=morgen + dt.timedelta(days=1),
                             eind=morgen + dt.timedelta(days=1, hours=2), antwoord="geen", organisator="Club")]
    monkeypatch.setattr(bronnen, "google_items", items)
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="", kleur=None: gemaakt.append((a, titel, s, e)) or "nieuw1")
    monkeypatch.setattr(bronnen, "google_antwoord", lambda tok, a, eid, antw: beantwoord.append((eid, antw)))

    r = klant.post("/clara/koppel/google", headers=FORM, data={"naam": "Neighbour", "domein": "prive"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    staat = dict(x.split("=", 1) for x in r.headers["location"].split("?", 1)[1].split("&"))["state"]
    assert klant.get("/clara/koppel/google/terug?code=x&state=verkeerd", headers=KOP, follow_redirects=False).headers["location"].startswith("/clara/koppelingen?fout=")
    r = klant.get(f"/clara/koppel/google/terug?code=x&state={staat}", headers=KOP, follow_redirects=False)
    assert r.status_code == 303 and "gekoppeld" in r.headers["location"]

    from app.kern import db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = 'google'").fetchone()
        assert k["account"] == "martijn@neighbour.nu" and k["schrijven"] and k["schrijfagenda"] == "primary"
        assert "1//ref" not in k["geheim"] and kluis.open_(k["geheim"]) == "1//ref"
        assert c.execute("SELECT count(*) AS n FROM agenda_items WHERE koppeling_id = %s", (k["id"],)).fetchone()["n"] == 3
        conflict = c.execute("SELECT * FROM meldingen WHERE bron = 'clara' AND titel LIKE 'Dubbele boeking%%'").fetchall()
        uitn = c.execute("SELECT * FROM meldingen WHERE bron = 'clara' AND titel LIKE 'Uitnodiging%%'").fetchall()
        assert len(conflict) == 1 and len(uitn) == 1
        item_id = c.execute("SELECT id FROM agenda_items WHERE extern_id = 'a3'").fetchone()["id"]

    pagina = klant.get("/clara", headers=KOP).text
    assert "Overleg ZEROHERO" in pagina and "dubbel" in pagina and "Keepersavond" in pagina and "Accepteren" in pagina
    vandaag = klant.get("/", headers=KOP).text
    assert "dubbele boeking" in vandaag

    # Uitnodiging beantwoorden (na een klik)
    r = klant.post(f"/clara/uitnodiging/{item_id}", headers=FORM, data={"antwoord": "geaccepteerd"}, follow_redirects=False)
    assert r.status_code == 303 and "geaccepteerd" in r.headers["location"] and beantwoord == [("a3", "geaccepteerd")]

    # Vrije tijd zoeken en een blok inplannen
    r = klant.get("/clara?duur=60&dagen=7", headers=KOP)
    assert r.status_code == 200 and "Zet in agenda" in r.text
    s, e = _nu_dag(3, 14), _nu_dag(3, 15)
    r = klant.post("/clara/blok", headers=FORM, follow_redirects=False, data={
        "titel": "Schrijven", "start": s.strftime("%Y-%m-%dT%H:%M"), "eind": e.strftime("%Y-%m-%dT%H:%M"), "doel": f"{k['id']}|primary"})
    assert r.status_code == 303 and "staat+in+je+agenda" in r.headers["location"], r.headers["location"]
    assert gemaakt == [("primary", "Schrijven", s, e)]

    # Zonder herkomst geen wijzigingen
    assert klant.post("/clara/blok", headers=KOP, data={"titel": "x", "start": "2026-01-01T10:00", "eind": "2026-01-01T11:00",
                                                         "doel": f"{k['id']}|primary"}).status_code == 403


def test_voorstel_van_clara_inplannen(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    gemaakt = []
    monkeypatch.setattr(bronnen, "google_toegang", lambda refresh: ("tok", 3600, None))
    monkeypatch.setattr(bronnen, "google_items", lambda *a: [])
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="", kleur=None: gemaakt.append(titel) or "v1")
    s = _nu_dag(4, 9)
    with db.pool.connection() as c:
        gid = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('clara', 'test') RETURNING id").fetchone()["id"]
        vid = c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, domein) VALUES (%s, 'Voorbereiden', %s, %s, 'politiek')
                           RETURNING id""", (gid, s, s + dt.timedelta(hours=1))).fetchone()["id"]
        k = c.execute("SELECT id FROM agenda_koppelingen WHERE soort = 'google'").fetchone()
    pagina = klant.get(f"/clara?g={gid}", headers=KOP).text
    assert "Voorstel:" in pagina and "Voorbereiden" in pagina
    r = klant.post(f"/clara/voorstel/{vid}", headers=FORM, data={"actie": "inplannen", "doel": f"{k['id']}|primary"}, follow_redirects=False)
    assert r.status_code == 303 and gemaakt == ["Voorbereiden"]
    with db.pool.connection() as c:
        assert c.execute("SELECT status FROM agenda_voorstellen WHERE id = %s", (vid,)).fetchone()["status"] == "ingepland"


def test_ics_koppelen_en_instellingen(klant, monkeypatch):
    nu = dt.datetime.now(TZ)
    morgen = (nu + dt.timedelta(days=1)).strftime("%Y%m%d")
    ics = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:g1\r\nSUMMARY:Bezet\r\n"
           b"DTSTART;TZID=Europe/Amsterdam:" + morgen.encode() + b"T190000\r\nDTEND;TZID=Europe/Amsterdam:" + morgen.encode() +
           b"T210000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    monkeypatch.setattr(bronnen, "ics_tekst", lambda url: ics)
    r = klant.post("/clara/koppel/ics", headers=FORM, follow_redirects=False,
                   data={"naam": "Gemeenteraad", "domein": "politiek", "url": "https://outlook.office365.com/owa/calendar/x/calendar.ics"})
    assert r.status_code == 303 and "1+afspraken" in r.headers["location"], r.headers["location"]
    from app.kern import db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = 'ics'").fetchone()
        assert "outlook.office365.com" not in k["geheim"] and not k["schrijven"]
    r = klant.post("/clara/instellingen", headers=FORM, data={"dag_0": "1", "dag_2": "1", "begin": "09:00", "eind": "16:00",
                                                              "avond": "1", "buffer": "5", "reistijd": "45"}, follow_redirects=False)
    assert r.status_code == 303
    assert 'value="45"' in klant.get("/clara/koppelingen", headers=KOP).text
    r = klant.post(f"/clara/koppeling/{k['id']}", headers=FORM, data={"actie": "weg"}, follow_redirects=False)
    assert "ontkoppeld" in r.headers["location"]


def test_iris_ziet_de_agenda(klant):
    from app import iris  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        tekst = iris.context(c)
    assert "Agenda vandaag (van Clara)" in tekst or "Agenda: nog geen" in tekst
    from app.clara import gesprek as g  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        ctx = g.context(c)
    # (de vorige test haalde een lege Google-agenda op, dus de afspraken zijn weer weg: precies zoals het hoort)
    assert "Agenda komende twee weken" in ctx and "Neighbour (google, lezen en schrijven)" in ctx and "Vrije blokken" in ctx
    assert "donderdag donderdag" not in ctx and "Overleg ZEROHERO" not in ctx


def test_raadsagenda_uit_notubiz(monkeypatch):
    from app.politiek import notubiz  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(notubiz, "evenementen", lambda van, tot: [
        {"id": 1, "type": "assembly", "naam": "Commissieavond", "soort": "commissie", "start": dt.datetime(2026, 11, 3, 19, 30),
         "plek": "Raadszaal", "url": "https://helmond.notubiz.nl/x"},
        {"id": 2, "type": "meeting", "naam": "Zonder tijd", "soort": "overig", "start": dt.datetime(2026, 11, 4, 0, 0)},
        {"id": 3, "type": "meeting", "naam": "Geen datum", "soort": "overig", "start": None}])
    lijst = bronnen.raad_items(t(1, 0), t(30, 0))
    assert len(lijst) == 2
    assert lijst[0]["start"] == t(3, 19, 30) and lijst[0]["eind"] == t(3, 22, 30) and lijst[0]["geschat"]
    assert lijst[1]["heledag"] and not lijst[1]["geschat"]


def test_planner_kent_de_agendataak():
    from app.clara import sync  # pylint: disable=import-outside-toplevel
    from app.kern import planner  # pylint: disable=import-outside-toplevel
    assert "agenda" in planner.TAKEN and planner.TAKEN["agenda"]["elke"] == dt.timedelta(minutes=5)
    van, tot = sync.venster(t(3, 12))
    assert van == t(3, 0) - dt.timedelta(days=7) and tot - van == dt.timedelta(days=63)


def test_verbergregels():
    whaller = afspraak("Bestuursvergadering", t(4, 19, 30), t(4, 21, 30), organisator="Whaller Event", antwoord="geen")
    sphere = afspraak("Overleg", t(5, 19, 30), t(5, 21), organisator="Whaller Event", omschrijving="Sphere: Bestuur PRO")
    ander = afspraak("Fractie", t(6, 19, 30), t(6, 21), organisator="Whaller Event", omschrijving="Sphere: Fractie")
    regels = [{"organisator": "Whaller", "tekst": "Bestuur"}]
    assert analyse.verborgen(whaller, regels) and analyse.verborgen(sphere, regels) and not analyse.verborgen(ander, regels)
    assert not analyse.verborgen(whaller, [{"organisator": "", "tekst": ""}])
    assert analyse.verborgen(ander, [{"organisator": "whaller event"}])
    assert bronnen.item(titel="x", start=t(1, 1), eind=t(1, 2), omschrijving="<p>Sphere:\n <b>Bestuur</b></p>")["omschrijving"] == "Sphere: Bestuur"


def test_whaller_bestuur_standaard_verborgen_en_negeren(klant):
    from app.clara import sync  # pylint: disable=import-outside-toplevel
    from app.kern import db, meldingen  # pylint: disable=import-outside-toplevel
    s = _nu_dag(6, 19, 30)
    with db.pool.connection() as c:
        c.execute("DELETE FROM instellingen WHERE sleutel = 'clara_verbergen'")
        k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, account, domein, schrijven, laatst)
                         VALUES ('google', 'Whaller-test', 'test@neighbour.nu', 'prive', true, now()) RETURNING id""").fetchone()["id"]
        for eid, titel, org, dag in (("w1", "Bestuursvergadering", "Whaller Event", 0), ("w2", "Bestuursvergadering", "Whaller Event", 28),
                                     ("p1", "Keeperstraining", "Club", 1)):
            c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, organisator, start, eind, antwoord)
                         VALUES (%s, 'primary', %s, %s, %s, %s, %s, 'geen')""",
                      (k, eid, titel, org, s + dt.timedelta(days=dag), s + dt.timedelta(days=dag, hours=2)))
        mid = meldingen.melden(c, "normaal", "clara", "2 nieuwe uitnodigingen",
                               f"Bestuursvergadering ({s:%d-%m %H.%M}); Bestuursvergadering (x)", "/clara", sleutel="test-whaller")
        sync.meldingen_opruimen(c)
        assert c.execute("SELECT gelezen FROM meldingen WHERE id = %s", (mid,)).fetchone()["gelezen"]
    pagina = klant.get("/clara", headers=KOP).text
    assert "Bestuursvergadering" not in pagina and "Keeperstraining" in pagina and "verborgen door je" in pagina
    assert "Whaller" in klant.get("/clara/koppelingen", headers=KOP).text
    # Regel weghalen: weer zichtbaar. Negeren vanaf de uitnodiging: weer weg, met een regel op organisator en titel.
    klant.post("/clara/verbergen", headers=FORM, data={"weg": "0"})
    assert "Bestuursvergadering" in klant.get("/clara", headers=KOP).text
    with db.pool.connection() as c:
        iid = c.execute("SELECT id FROM agenda_items WHERE extern_id = 'w1'").fetchone()["id"]
    r = klant.post("/clara/verbergen", headers=FORM, data={"item": iid, "van": "week"}, follow_redirects=False)
    assert r.headers["location"].startswith("/clara?melding=")
    assert "Bestuursvergadering" not in klant.get("/clara", headers=KOP).text
    with db.pool.connection() as c:
        assert sync.verbergregels(c) == [{"organisator": "Whaller Event", "tekst": "Bestuursvergadering"}]
        assert "Bestuursvergadering" not in __import__("app.clara.gesprek", fromlist=["x"]).context(c).split("# Agenda komende twee weken")[1]
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (k,))


class _NepClaude:
    """Bootst de Claude-API na: geeft de antwoorden in volgorde terug en onthoudt wat er gevraagd werd."""
    def __init__(self, *antwoorden):
        self.antwoorden, self.verzoeken = list(antwoorden), []

    def __call__(self, verzoek, timeout=0):
        body = json.loads(verzoek.data)
        self.verzoeken.append(body)
        a = self.antwoorden.pop(0)
        if isinstance(a, Exception):
            raise a
        return io.BytesIO(json.dumps(a).encode())


def _antw(tekst, stop="end_turn"):
    inhoud = [{"type": "thinking", "thinking": ""}] + ([{"type": "text", "text": tekst}] if tekst else [])
    return {"content": inhoud, "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 5}}


class _NepC:
    def execute(self, *a, **kw):
        class R:
            def fetchone(self):
                return {"eur": 0, "aantal": 0}
        return R()


def test_claude_ruimte_voor_nadenken_en_nooit_leeg(monkeypatch):
    from app.kern import claude  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    berichten = [{"role": "user", "content": "Vraag"}, {"role": "assistant", "content": ""}, {"role": "user", "content": "Nog een"}]
    nep = _NepClaude(_antw("", "max_tokens"), _antw("Volledig antwoord"))
    monkeypatch.setattr(claude.urllib.request, "urlopen", nep)
    assert claude.gesprek(_NepC(), "clara", "sys", berichten, model="claude-sonnet-5-5", max_tokens=1500) == "Volledig antwoord"
    eerste, tweede = nep.verzoeken
    assert eerste["output_config"] == {"effort": "low"} and eerste["max_tokens"] == 1500 + claude.DENKRUIMTE
    assert tweede["max_tokens"] == 2 * eerste["max_tokens"]
    assert all(m["content"] for m in eerste["messages"]) and len(eerste["messages"]) == 2  # leeg bericht weggelaten
    # Haiku 4.5 kent geen inspanning; afgebroken tekst krijgt een opmerking; leeg is een fout; weigering ook.
    nep = _NepClaude(_antw("Half", "max_tokens"), _antw("Half", "max_tokens"))
    monkeypatch.setattr(claude.urllib.request, "urlopen", nep)
    assert "afgebroken" in claude.gesprek(_NepC(), "iris", "sys", berichten[:1], model="claude-haiku-4-5")
    assert "output_config" not in nep.verzoeken[0]
    monkeypatch.setattr(claude.urllib.request, "urlopen", _NepClaude(_antw("")))
    with pytest.raises(claude.ClaudeFout, match="leeg"):
        claude.gesprek(_NepC(), "iris", "sys", berichten[:1])
    monkeypatch.setattr(claude.urllib.request, "urlopen", _NepClaude(_antw("", "refusal")))
    with pytest.raises(claude.ClaudeFout, match="niet antwoorden"):
        claude.gesprek(_NepC(), "iris", "sys", berichten[:1])
    fout = urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b'{"error":{"message":"output_config: Extra inputs are not permitted"}}'))
    nep = _NepClaude(fout, _antw("Zonder inspanning"))
    monkeypatch.setattr(claude.urllib.request, "urlopen", nep)
    assert claude.gesprek(_NepC(), "iris", "sys", berichten[:1]) == "Zonder inspanning" and "output_config" not in nep.verzoeken[1]


# ---------------------------------------------------------------- reistijd

THUIS = (5.6560, 51.4740)          # ergens in Helmond (lengte, breedte)
PLEKKEN = {"huis voor de stad": (5.6573, 51.4797), "utrecht centraal": (5.1103, 52.0894),
           "veldhoven, de run": (5.4020, 51.4180), "eindhoven, stationsplein": (5.4790, 51.4430),
           "stadhuis helmond": (5.6573, 51.4797)}


def test_vervoer_volgens_de_regels():
    from app.clara import reis  # pylint: disable=import-outside-toplevel
    inst = {"fiets_km": 6}
    assert reis.vervoer(THUIS, PLEKKEN["huis voor de stad"], inst) == "fiets"
    assert reis.vervoer(THUIS, PLEKKEN["utrecht centraal"], inst) == "ov"
    assert reis.vervoer(THUIS, PLEKKEN["eindhoven, stationsplein"], inst) == "ov"   # midden in een grote stad
    assert reis.vervoer(THUIS, PLEKKEN["veldhoven, de run"], inst) == "auto"


def _nep_reis(monkeypatch, keuzes=None):
    from app.clara import reis  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(reis, "actief", lambda: True)
    monkeypatch.setattr(reis, "adres", lambda c, tekst, focus=None, alleen_cache=False: (
        {"punt": THUIS, "label": "Thuis, Helmond"} if tekst == "thuisadres" else
        ({"punt": PLEKKEN[reis._sleutel(tekst)], "label": tekst} if reis._sleutel(tekst) in PLEKKEN else None)))
    snelheid = {"fiets": 15, "auto": 60, "lopen": 5}
    monkeypatch.setattr(reis, "route", lambda c, van, naar, soort, alleen_cache=False, **kw: (
        None if soort == "ov" else max(1, round(reis.km(van, naar) * 1.3 / snelheid[soort] * 60))))  # zoals OpenRouteService: geen OV
    monkeypatch.setattr(reis, "keuzes", lambda c: keuzes or {})
    return reis


def test_reisplan_heen_terug_en_past_niet(monkeypatch):
    reis = _nep_reis(monkeypatch)
    inst = {"vertrekpunt": "thuisadres", "fiets_km": 6, "marge": 5, "thuis_tussendoor": 180}
    lijst = [
        afspraak("Commissie", t(3, 19, 30), t(3, 22), locatie="Huis voor de Stad", koppeling_id=1, extern_id="c"),
        afspraak("Overleg", t(4, 10), t(4, 12), locatie="Utrecht Centraal", koppeling_id=1, extern_id="u"),
        afspraak("Lunch", t(4, 12, 30), t(4, 13, 30), locatie="Veldhoven, De Run", koppeling_id=1, extern_id="v"),
        afspraak("Teams", t(5, 9), t(5, 10), locatie="Microsoft Teams-vergadering", online=True, koppeling_id=1, extern_id="t"),
        afspraak("Ergens", t(5, 11), t(5, 12), locatie="Onvindbaar 1", koppeling_id=1, extern_id="o"),
        afspraak("Al gepland", t(3, 19, 10), t(3, 19, 30), omschrijving="[clara-reistijd:1:c:heen] Ingepland", koppeling_id=1, extern_id="r"),
    ]
    p = reis.plan(None, lijst, inst)
    per = {(r["afspraak"]["extern_id"], r["richting"]): r for r in p["regels"]}
    assert per[("c", "heen")]["vervoer"] == "fiets" and per[("c", "heen")]["ingepland"] and per[("c", "terug")]["vervoer"] == "fiets"
    assert per[("u", "heen")]["vervoer"] == "ov" and per[("u", "heen")]["geschat"] and ("u", "terug") not in per  # door naar Veldhoven
    lunch = per[("v", "heen")]
    assert lunch["van"] == "Utrecht Centraal" and lunch["vervoer"] == "auto" and not lunch["past"] and lunch["botst"] == "Overleg"
    assert ("t", "heen") not in per and [a["titel"] for a in p["onbekend"]] == ["Ergens"]
    assert per[("v", "terug")]["naar"] == "thuis"
    titel = reis.blok_titel(per[("u", "heen")])
    assert titel == "🚆 Reistijd naar Utrecht Centraal (OV)" and "[clara-reistijd:1:u:heen]" in reis.blok_omschrijving(per[("u", "heen")])


def test_zelf_gekozen_vervoer(monkeypatch):
    reis = _nep_reis(monkeypatch)
    a = afspraak("Overleg", t(4, 10), t(4, 12), locatie="Utrecht Centraal", koppeling_id=1, extern_id="u", ical_uid="REEKS@x")
    reis = _nep_reis(monkeypatch, {reis.keuze_sleutel(a): "auto"})
    p = reis.plan(None, [a], {"vertrekpunt": "thuisadres"})
    assert all(r["vervoer"] == "auto" and r["gekozen"] and not r.get("geschat") for r in p["regels"])
    reis = _nep_reis(monkeypatch, {"uid:reeks@x": "geen"})
    p = reis.plan(None, [a], {"vertrekpunt": "thuisadres"})
    assert [r["vervoer"] for r in p["regels"]] == ["geen"] and p["regels"][0]["minuten"] is None


def test_reistijd_in_het_scherm_en_inplannen(klant, monkeypatch):
    from app.clara import reis, sync  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(config, "ORS_API_KEY", "ors-test")
    vragen = []

    def nep_ors(pad, params=None, body=None):
        vragen.append(pad)
        if pad.startswith("/pelias"):
            punt = THUIS if "Strik" in params["text"] else PLEKKEN.get(reis._sleutel(params["text"]))
            return {"features": [{"geometry": {"coordinates": list(punt)}, "properties": {"label": params["text"], "confidence": 0.9}}]} if punt else {"features": []}
        assert pad.startswith("/openrouteservice/v2/directions/") and len(body["coordinates"]) == 2
        return {"routes": [{"summary": {"duration": 1200, "distance": 5000}}]}
    monkeypatch.setattr(reis, "_ors", nep_ors)
    r = klant.post("/clara/reistijd/instellingen", headers=FORM, follow_redirects=False,
                   data={"vertrekpunt": "Meester Strikstraat 25, 5708 GA Helmond", "fiets_km": "6", "marge": "5"})
    assert "Vertrekpunt+gevonden" in r.headers["location"], r.headers["location"]
    s = _nu_dag(2, 19, 30)
    gemaakt = []
    with db.pool.connection() as c:
        k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, account, domein, schrijven, agendas, schrijfagenda, laatst)
                         VALUES ('google', 'Reis-test', 'r@neighbour.nu', 'prive', true, %s, 'primary', now()) RETURNING *""",
                      (json.dumps([{"id": "primary", "naam": "M", "primair": True, "schrijven": True, "aan": True, "domein": "prive"}]),)).fetchone()
        iid = c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, locatie, start, eind, domein)
                           VALUES (%s, 'primary', 'raad1', 'Raadsvergadering', 'Huis voor de Stad', %s, %s, 'politiek') RETURNING id""",
                        (k["id"], s, s + dt.timedelta(hours=3))).fetchone()["id"]
        assert "reistijd: 2 reizen" in sync.reistijd_bijwerken(c)
        # tweede keer: alles uit het geheugen, geen nieuwe vragen
        aantal = len(vragen)
        sync.reistijd_bijwerken(c)
        assert len(vragen) == aantal
    pagina = klant.get("/clara?week=0", headers=KOP).text
    assert "Heen: 25 min" in pagina and "fietsen" in pagina and "Reistijd inplannen" in pagina and "automatisch (fiets)" in pagina
    r = klant.post(f"/clara/vervoer/{iid}", headers=FORM, data={"vervoer": "auto", "week": "0"}, follow_redirects=False)
    assert "auto" in r.headers["location"] and r.headers["location"].count("?") == 1
    assert klant.get(r.headers["location"], headers=KOP).status_code == 200
    pagina = klant.get("/clara", headers=KOP).text
    assert "rijden" in pagina and 'value="auto" selected' in pagina

    monkeypatch.setattr(bronnen, "google_toegang", lambda refresh: ("tok", 3600, None))
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k, forceer=False, nu=None: None)
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, st, e, o="", kleur=None: gemaakt.append((titel, st, e, o, kleur)) or "blok1")
    with db.pool.connection() as c:
        c.execute("UPDATE agenda_koppelingen SET geheim = %s WHERE id = %s", (kluis.sluit("ref"), k["id"]))
    # Eerst het overzicht: waar komt reistijd, met vinkjes (de knop bij de afspraak toont alleen die afspraak).
    r = klant.get("/clara/reistijd/overzicht?week=0", headers=KOP)
    assert r.status_code == 200 and "Heen naar Huis voor de Stad" in r.text and "Terug naar huis" in r.text
    assert f'name="sleutel" value="{k["id"]}:raad1:heen" checked' in r.text and "Raadsvergadering" in r.text and "M (Reis-test)" in r.text
    assert 'name="doel"' in r.text and 'action="/clara/reistijd"' in r.text
    r = klant.get(f"/clara/reistijd/overzicht?week=0&sleutel={k['id']}:raad1:terug", headers=KOP)
    assert "Terug naar huis" in r.text and "Heen naar" not in r.text
    assert 'formaction="/clara/reistijd/overzicht" formmethod="get"' in klant.get("/clara", headers=KOP).text
    r = klant.post("/clara/reistijd", headers=FORM, data={"week": "0", "sleutel": [f"{k['id']}:raad1:heen", f"{k['id']}:raad1:terug"],
                                                          "doel": "onzin|x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/clara?week=0&melding=2+reistijdblokken") and r.headers["location"].count("?") == 1
    assert gemaakt[0][0] == "🚗 Reistijd naar Huis voor de Stad (rijden)" and gemaakt[0][2] == s and "[clara-reistijd:" in gemaakt[0][3]
    assert gemaakt[0][4] == "8"  # Grafiet, de standaardkleur voor reistijd
    assert gemaakt[1][0].startswith("🚗 Terugreis naar huis") and gemaakt[1][1] == s + dt.timedelta(hours=3)
    # Komt het blok terug uit Google, dan staat de reis als ingepland.
    with db.pool.connection() as c:
        c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, start, eind, omschrijving)
                     VALUES (%s, 'primary', 'blok1', %s, %s, %s, %s)""", (k["id"], gemaakt[0][0], gemaakt[0][1], gemaakt[0][2], gemaakt[0][3]))
    assert "ingepland" in klant.get("/clara", headers=KOP).text
    # Bestaande blokken bijwerken: icoon (uit het vervoer in de omschrijving) en de gekozen kleur.
    gewijzigd = []
    monkeypatch.setattr(bronnen, "google_wijzig", lambda tok, a, eid, titel=None, kleur=None: gewijzigd.append((eid, titel, kleur)))
    klant.post("/clara/reistijd/instellingen", headers=FORM, data={"vertrekpunt": "Meester Strikstraat 25, 5708 GA Helmond",
                                                                   "fiets_km": "6", "marge": "5", "reis_kleur": "11"})
    with db.pool.connection() as c:
        c.execute("UPDATE agenda_items SET titel = 'Reistijd naar Huis voor de Stad (rijden)' WHERE extern_id = 'blok1'")
    assert "Icoon en kleur bijwerken" in klant.get("/clara/koppelingen", headers=KOP).text
    r = klant.post("/clara/reistijd/bijwerken", headers=FORM, follow_redirects=False)
    assert "1+reistijdblok+bijgewerkt" in r.headers["location"], r.headers["location"]
    assert gewijzigd == [("blok1", "🚗 Reistijd naar Huis voor de Stad (rijden)", "11")]
    # Clara legt een vervoerskeuze vast als Martijn dat zegt.
    from app.clara import gesprek as g  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        tekst, klaar = g.vervoer_uit(c, f'Goed, ik zet het op de fiets.\n<vervoer>{{"id": {iid}, "vervoer": "fiets"}}</vervoer>')
        assert tekst == "Goed, ik zet het op de fiets." and klaar == ["Raadsvergadering: fiets"]
        assert reis.keuzes(c)[f"id:{k['id']}:raad1"] == "fiets"
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (k["id"],))
        c.execute("DELETE FROM agenda_vervoer")



def test_google_maps_met_ov_volgens_dienstregeling(klant, monkeypatch):
    from app.clara import reis  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(config, "GOOGLE_MAPS_KEY", "AIza-test")
    monkeypatch.setattr(config, "ORS_API_KEY", "")
    verzoeken = []

    def nep_http(url, data=None, headers=None, methode=None, body=None, timeout=30, soort=""):
        verzoeken.append((url, headers, body))
        if "geocode" in url:
            assert "key=AIza-test" in url and "region=nl" in url
            if "Nergens" in url:
                return {"status": "ZERO_RESULTS", "results": []}
            return {"status": "OK", "results": [{"geometry": {"location": {"lat": 52.0894, "lng": 5.1103}},
                                                 "formatted_address": "Stationsplein, Utrecht", "types": ["transit_station"]}]}
        assert url == reis.GOOGLE_ROUTES and headers["X-Goog-Api-Key"] == "AIza-test" and "routes.duration" in headers["X-Goog-FieldMask"]
        if body["travelMode"] == "TRANSIT" and body.get("arrivalTime", "").endswith("T03:00:00Z"):
            return {}  # 's nachts geen OV
        return {"routes": [{"duration": {"TRANSIT": "4380s", "DRIVE": "3900s", "BICYCLE": "900s"}[body["travelMode"]], "distanceMeters": 1}]}
    monkeypatch.setattr(bronnen, "_http", nep_http)
    assert reis.dienst() == "google" and reis.dienst_naam() == "Google Maps"
    with db.pool.connection() as c:
        c.execute("DELETE FROM plekken")
        c.execute("DELETE FROM routes")
        plek = reis.adres(c, "Utrecht Centraal", focus=THUIS)
        assert plek["label"] == "Stationsplein, Utrecht" and plek["punt"] == (5.1103, 52.0894)
        assert reis.adres(c, "Nergens 1") is None and reis.adres(c, "Nergens 1") is None
        assert sum(1 for u, _h, _b in verzoeken if "Nergens" in u) == 1  # 'niet gevonden' wordt bewaard
        dag = t(10, 10)
        assert reis.reistijd(c, THUIS, plek["punt"], "ov", tijd=dag, aankomst=True) == (73, False)
        ov = [b for _u, _h, b in verzoeken if b and b["travelMode"] == "TRANSIT"][0]
        assert ov["arrivalTime"] == dag.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert reis.reistijd(c, THUIS, plek["punt"], "auto") == (65, False)
        auto = [b for _u, _h, b in verzoeken if b and b["travelMode"] == "DRIVE"][0]
        assert auto["routingPreference"] == "TRAFFIC_UNAWARE" and "arrivalTime" not in auto
        # Geen OV-verbinding (04.00 uur): schatting op basis van de autotijd, en 'geen route' wordt bewaard.
        nacht = dt.datetime(2026, 11, 10, 4, 0, tzinfo=TZ)
        assert reis.reistijd(c, THUIS, plek["punt"], "ov", tijd=nacht) == (120, True)
        aantal = len(verzoeken)
        assert reis.reistijd(c, THUIS, plek["punt"], "ov", tijd=nacht) == (120, True) and len(verzoeken) == aantal
        assert "Google Maps" in klant.get("/clara/koppelingen", headers=KOP).text


def test_terugkeerlink_met_vraag_en_anker():
    from app.clara.web import _terug  # pylint: disable=import-outside-toplevel
    assert _terug("/clara?week=0", melding="2 blokken").headers["location"] == "/clara?week=0&melding=2+blokken"
    assert _terug("/clara/koppelingen", fout="x").headers["location"] == "/clara/koppelingen?fout=x"
    assert _terug("/clara?g=3#gesprek", melding="ok").headers["location"] == "/clara?g=3&melding=ok#gesprek"
    assert _terug("/clara?g=3#gesprek").headers["location"] == "/clara?g=3#gesprek"


def test_google_kleur_in_verzoek(monkeypatch):
    verzoeken = []
    monkeypatch.setattr(bronnen, "_http", lambda url, **kw: verzoeken.append((url, kw)) or {"id": "x"})
    bronnen.google_maak("tok", "primary", "🚲 Reistijd", t(3, 9), t(3, 9, 20), "o", kleur="8")
    bronnen.google_maak("tok", "primary", "Blok", t(3, 9), t(3, 10), "o", kleur="99")  # onbekende kleur: geen colorId
    bronnen.google_wijzig("tok", "primary", "e1", titel="🚗 Nieuw", kleur="11")
    assert verzoeken[0][1]["body"]["colorId"] == "8" and "colorId" not in verzoeken[1][1]["body"]
    assert verzoeken[2][1]["methode"] == "PATCH" and verzoeken[2][1]["body"] == {"summary": "🚗 Nieuw", "colorId": "11"}


def test_gesprekken_archiveren_en_verwijderen(klant):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        c.execute("DELETE FROM gesprekken WHERE persona IN ('clara', 'iris')")
        oud = c.execute("INSERT INTO gesprekken (persona, titel, bijgewerkt) VALUES ('clara', 'Oud gesprek', now() - interval '1 day') RETURNING id").fetchone()["id"]
        nieuw = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('clara', 'Nieuw gesprek') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', 'Vraag in het nieuwe gesprek')", (nieuw,))
        s = _nu_dag(3, 9)
        vid = c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind) VALUES (%s, 'Blok', %s, %s) RETURNING id""",
                        (nieuw, s, s + dt.timedelta(hours=1))).fetchone()["id"]
        iris = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('iris', 'Dagstart') RETURNING id").fetchone()["id"]
    pagina = klant.get("/clara", headers=KOP).text
    # Het gesprek staat rechtsboven, boven de dubbele boekingen.
    assert pagina.index('id="gesprek"') < pagina.index("Dubbele boekingen") < pagina.index('id="uitnodigingen"') < pagina.index('id="vrij"')
    assert "Vraag in het nieuwe gesprek" in pagina and "Eerdere gesprekken (1)" in pagina and "Oud gesprek" in pagina
    # Archiveren: uit de lijst en niet meer het standaardgesprek; terug te halen.
    assert klant.post(f"/gesprek/{nieuw}/archief", headers=KOP, data={"terug": "/clara"}).status_code == 403  # zonder herkomst
    r = klant.post(f"/gesprek/{nieuw}/archief", headers=FORM, data={"terug": "/clara"}, follow_redirects=False)
    assert r.headers["location"] == "/clara"
    pagina = klant.get("/clara", headers=KOP).text
    assert "Vraag in het nieuwe gesprek" not in pagina and "Archief (1)" in pagina
    assert "gearchiveerd" in klant.get(f"/clara?g={nieuw}", headers=KOP).text
    r = klant.post(f"/gesprek/{nieuw}/archief", headers=FORM, data={"terug": "/clara"}, follow_redirects=False)
    assert r.headers["location"] == f"/clara?g={nieuw}"
    # Verwijderen: gesprek en berichten weg, open voorstel vervalt. Een vreemd terugadres wordt genegeerd.
    r = klant.post(f"/gesprek/{nieuw}/weg", headers=FORM, data={"terug": "https://kwaad.nl"}, follow_redirects=False)
    assert r.headers["location"] == "/clara"
    with db.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM gesprekken WHERE id = %s", (nieuw,)).fetchone()
        assert not c.execute("SELECT 1 FROM gesprek_berichten WHERE gesprek_id = %s", (nieuw,)).fetchone()
        assert c.execute("SELECT status FROM agenda_voorstellen WHERE id = %s", (vid,)).fetchone()["status"] == "afgewezen"
    # Iris kan het ook.
    assert "Dit gesprek archiveren" in klant.get("/iris", headers=KOP).text
    klant.post(f"/gesprek/{iris}/archief", headers=FORM, data={"terug": "/iris"})
    assert "Archief (1)" in klant.get("/iris", headers=KOP).text
    klant.post(f"/gesprek/{iris}/weg", headers=FORM, data={"terug": "/iris"})
    assert "Nog geen gesprekken" in klant.get("/iris", headers=KOP).text


# ---------------------------------------------------------------- kleuren en andere agenda's (zoals die van Sanne)

def test_kleuren_zoals_in_google(monkeypatch):
    verzoeken = []
    monkeypatch.setattr(bronnen, "_http", lambda url, **kw: verzoeken.append(url) or {"items": [
        {"id": "primary", "summary": "Martijn", "primary": True, "accessRole": "owner", "backgroundColor": "#9fc6e7"},
        {"id": "sanne", "summary": "Sanne", "accessRole": "owner", "backgroundColor": "#ac725e"},
        {"id": "eigen", "summary": "Eigen kleur", "accessRole": "reader", "backgroundColor": "#123ABC"},
        {"id": "raar", "summary": "Zonder", "accessRole": "reader", "backgroundColor": "rood"}]})
    lijst = {a["id"]: a for a in bronnen.google_agendas("tok")}
    assert lijst["sanne"]["kleur"] == "#795548"  # bruin, zoals Google Agenda het nu toont
    assert lijst["primary"]["kleur"] == "#4285F4" and lijst["eigen"]["kleur"] == "#123ABC" and lijst["raar"]["kleur"] == ""
    assert not lijst["eigen"]["schrijven"]
    e = {"id": "e1", "summary": "Zwemles", "colorId": "11", "start": {"dateTime": "2026-11-03T16:00:00+01:00"},
         "end": {"dateTime": "2026-11-03T17:00:00+01:00"}}
    assert bronnen.google_item(e, "sanne")["kleur"] == "#D50000"
    assert bronnen.google_item(dict(e, colorId=None), "sanne")["kleur"] == ""
    monkeypatch.setattr(bronnen, "_http", lambda url, **kw: verzoeken.append(url) or {"value": [
        {"id": "o1", "name": "Agenda", "isDefaultCalendar": True, "canEdit": True, "hexColor": "#e74856"},
        {"id": "o2", "name": "Gedeeld", "canEdit": False, "hexColor": ""}]})
    ms = {a["id"]: a for a in bronnen.ms_agendas("tok")}
    assert ms["o1"]["kleur"] == "#e74856" and ms["o2"]["kleur"] == "" and "hexColor" in verzoeken[-1]


def test_dubbele_in_andere_agenda_telt_niet_als_bezet():
    eigen = afspraak("Ouderavond", t(5, 19), t(5, 20), ical_uid="x@g", agenda_naam="Martijn")
    sanne = afspraak("Ouderavond", t(5, 19), t(5, 20), ical_uid="x@g", agenda_naam="Sanne", vrij=True, niet_voor_mij=True)
    for volgorde in ([eigen, sanne], [sanne, eigen]):
        uit = analyse.samenvoegen(volgorde)
        assert len(uit) == 1 and not uit[0].get("niet_voor_mij") and uit[0]["ook"] == ["Sanne"]


def test_voorstel_voor_een_andere_agenda():
    tekst, v = gesprek.voorstellen_uit('Prima. <voorstel>{"titel": "Zwemles Sanne", "start": "2026-11-04T16:00", '
                                       '"eind": "2026-11-04T17:00", "domein": "prive", "agenda": "Sanne"}</voorstel>')
    assert tekst == "Prima." and v[0]["agenda"] == "Sanne" and v[0]["titel"] == "Zwemles Sanne"
    assert gesprek.voorstellen_uit('<voorstel>{"titel": "x", "start": "2026-11-04T16:00", "eind": "2026-11-04T17:00"}</voorstel>')[1][0]["agenda"] == ""
    assert '"agenda"' in gesprek.UITLEG and "Sanne" in gesprek.UITLEG


def test_agenda_van_sanne_kleur_niet_bezet_en_inplannen(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    morgen = _nu_dag(2, 16)
    gemaakt = []
    monkeypatch.setattr(bronnen, "google_toegang", lambda refresh: ("tok", 3600, None))
    monkeypatch.setattr(bronnen, "google_agendas", lambda tok: [
        {"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True, "kleur": "#4285F4"},
        {"id": "sanne", "naam": "Sanne", "primair": False, "schrijven": True, "kleur": "#795548"}])

    def items(tok, agenda_id, van, tot):
        if agenda_id == "primary":
            return [bronnen.item(agenda_id="primary", extern_id="m1", titel="Belafspraak", start=morgen, eind=morgen + dt.timedelta(hours=1)),
                    bronnen.item(agenda_id="primary", extern_id="m2", titel="Rood blok", start=morgen + dt.timedelta(days=1),
                                 eind=morgen + dt.timedelta(days=1, hours=1), kleur="#D50000")]
        vandaag = _nu_dag(0, 0)
        return [bronnen.item(agenda_id="sanne", extern_id="s1", titel="Zwemles", start=morgen, eind=morgen + dt.timedelta(hours=1),
                             locatie="Zwembad De Wissen, Helmond"),
                bronnen.item(agenda_id="sanne", extern_id="s0", titel="Studiedag", start=vandaag, eind=vandaag + dt.timedelta(days=1),
                             heledag=True)]
    monkeypatch.setattr(bronnen, "google_items", items)
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="", kleur=None: gemaakt.append((a, titel)) or "n1")
    with db.pool.connection() as c:
        c.execute("DELETE FROM meldingen WHERE bron = 'clara'")
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = 'google'").fetchone()
    kid = k["id"]
    klant.post(f"/clara/koppeling/{kid}", headers=FORM, data={"actie": "agendas"})
    pagina = klant.get("/clara/koppelingen", headers=KOP).text
    assert "Bezet voor mij" in pagina and 'name="bezet_sanne" value="1" checked' in pagina and "background: #795548" in pagina

    # Eerst telt Sanne mee als bezet: een dubbele boeking.
    klant.post(f"/clara/koppeling/{kid}", headers=FORM, data={
        "naam": k["naam"], "domein": "prive", "actief": "1", "schrijfagenda": "primary",
        "aan_primary": "1", "aan_sanne": "1", "bezet_primary": "1", "bezet_sanne": "1"})
    assert "Zwemles" in klant.get("/clara", headers=KOP).text
    with db.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM meldingen WHERE bron = 'clara' AND titel LIKE 'Dubbele boeking%%'").fetchone()["n"] == 1
        c.execute("DELETE FROM meldingen WHERE bron = 'clara'")

    # Sanne niet meer 'bezet voor mij': geen dubbele boeking, wel zichtbaar in haar eigen kleur.
    klant.post(f"/clara/koppeling/{kid}", headers=FORM, data={
        "naam": k["naam"], "domein": "prive", "actief": "1", "schrijfagenda": "primary",
        "aan_primary": "1", "aan_sanne": "1", "bezet_primary": "1"})
    with db.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM meldingen WHERE bron = 'clara' AND titel LIKE 'Dubbele boeking%%'").fetchone()["n"] == 0
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone()
        assert {a["id"]: a["bezet"] for a in k["agendas"]} == {"primary": True, "sanne": False}
        from app.clara import sync  # pylint: disable=import-outside-toplevel
        lijst = {a["titel"]: a for a in sync.afspraken(c, morgen - dt.timedelta(hours=1), morgen + dt.timedelta(days=2))}
        assert lijst["Zwemles"]["niet_voor_mij"] and lijst["Zwemles"]["agenda_naam"] == "Sanne" and lijst["Zwemles"]["kleur_toon"] == "#795548"
        assert lijst["Belafspraak"]["kleur_toon"] == "#4285F4" and lijst["Rood blok"]["kleur_toon"] == "#D50000"
        assert not analyse.conflicten(list(lijst.values()))
        ctx = gesprek.context(c)
        assert "Zwemles [Privé, Sanne] (Zwembad De Wissen, Helmond; niet bezet voor Martijn, hij gaat niet mee; id " in ctx
        assert "kan inplannen: Martijn, Sanne" in ctx
    pagina = klant.get("/clara", headers=KOP).text
    assert "border-left-color: #795548" in pagina and "border-left-color: #D50000" in pagina and "(niet bezet voor jou)" in pagina
    assert "dubbel" not in pagina.split('id="gesprek"')[0]
    vandaag = klant.get("/", headers=KOP).text
    assert "Studiedag" in vandaag and "border-left-color: #795548" in vandaag

    # Een voorstel van Clara voor Sanne: haar agenda staat al gekozen.
    with db.pool.connection() as c:
        gid = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('clara', 'Sanne') RETURNING id").fetchone()["id"]
        s = _nu_dag(5, 16)
        vid = c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, domein, agenda)
                           VALUES (%s, 'Tandarts Sanne', %s, %s, 'prive', 'Sanne') RETURNING id""",
                        (gid, s, s + dt.timedelta(hours=1))).fetchone()["id"]
    pagina = klant.get(f"/clara?g={gid}", headers=KOP).text
    assert "voor Sanne" in pagina and f'<option value="{kid}|sanne" selected>' in pagina
    assert f'<option value="{kid}|primary" selected>' not in pagina
    klant.post(f"/clara/voorstel/{vid}", headers=FORM, data={"actie": "inplannen", "doel": f"{kid}|sanne"})
    assert gemaakt == [("sanne", "Tandarts Sanne")]


# ---------------------------------------------------------------- beschikbaarheid delen (in plaats van CalendarBridge)

def test_blok_bezet_verzoeken(monkeypatch):
    verzoeken = []
    monkeypatch.setattr(bronnen, "_http", lambda url, **kw: verzoeken.append((url, kw)) or {"id": "b1"})
    assert bronnen.google_blok("tok", "primary", "Bezet", t(3, 9), t(3, 10)) == "b1"
    bronnen.google_blok("tok", "primary", "Bezet", t(3, 9), t(3, 10), voorlopig=True, extern_id="b1")
    bronnen.google_verwijder("tok", "primary", "b1")
    g = verzoeken[0][1]["body"]
    assert g["visibility"] == "private" and g["transparency"] == "opaque" and g["reminders"] == {"useDefault": False, "overrides": []}
    assert g["description"].startswith("[clara-spiegel]") and "methode" not in verzoeken[0][1]
    assert verzoeken[1][0].endswith("/events/b1") and verzoeken[1][1]["methode"] == "PATCH" and verzoeken[1][1]["body"]["summary"] == "Bezet (voorlopig)"
    assert verzoeken[2][1]["methode"] == "DELETE"
    verzoeken.clear()
    bronnen.ms_blok("tok", "cal", "Bezet", t(3, 9), t(3, 10))
    bronnen.ms_blok("tok", "cal", "Bezet", t(3, 9), t(3, 10), voorlopig=True, extern_id="e/1")
    bronnen.ms_verwijder("tok", "cal", "e/1")
    m = verzoeken[0][1]["body"]
    assert verzoeken[0][0].endswith("/me/calendars/cal/events") and m["sensitivity"] == "private" and m["isReminderOn"] is False
    assert m["showAs"] == "busy" and m["start"]["dateTime"] == "2026-11-03T09:00:00" and m["body"]["content"].startswith("[clara-spiegel]")
    assert verzoeken[1][0].endswith("/me/events/e%2F1") and verzoeken[1][1]["body"]["showAs"] == "tentative"
    assert verzoeken[2][1]["methode"] == "DELETE"


def test_niet_gevonden_is_eigen_fout(monkeypatch):
    _http_fout(monkeypatch, 410, {"error": {"code": 410, "message": "Resource has been deleted"}})
    with pytest.raises(bronnen.NietGevonden):
        bronnen.google_verwijder("tok", "primary", "x")


def test_beschikbaarheid_doorzetten(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    from app.clara import spiegel, sync  # pylint: disable=import-outside-toplevel
    nu = dt.datetime.now(TZ)
    morgen = _nu_dag(1, 10)
    log = []
    teller = iter(range(1000))

    def maak(soort):
        def f(tok, agenda_id, titel, start, eind, voorlopig=False, extern_id=None):
            log.append((soort, "wijzig" if extern_id else "nieuw", agenda_id, titel, start, voorlopig))
            return extern_id or f"{soort}{next(teller)}"
        return f

    def weg(soort):
        return lambda tok, agenda_id, extern_id: log.append((soort, "weg", agenda_id, extern_id))
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")
    monkeypatch.setattr(bronnen, "google_blok", maak("g"))
    monkeypatch.setattr(bronnen, "ms_blok", maak("m"))
    monkeypatch.setattr(bronnen, "google_verwijder", weg("g"))
    monkeypatch.setattr(bronnen, "ms_verwijder", weg("m"))

    def item(kid, agenda, eid, titel, start, uur=1, **kw):
        c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, ical_uid, titel, start, eind, heledag, status, vrij,
                     antwoord, omschrijving) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                  (kid, agenda, eid, kw.get("uid", ""), titel, start, start + dt.timedelta(hours=uur), kw.get("heledag", False),
                   kw.get("status", "bevestigd"), kw.get("vrij", False), kw.get("antwoord", "eigen"), kw.get("omschrijving", "")))

    with db.pool.connection() as c:
        g = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = 'google'").fetchone()
        c.execute("DELETE FROM agenda_koppelingen WHERE soort <> 'google'")
        c.execute("DELETE FROM agenda_items")
        c.execute("DELETE FROM agenda_spiegel")
        c.execute("UPDATE agenda_koppelingen SET schrijfagenda = 'primary', fout = NULL, spiegel = true, actief = true WHERE id = %s", (g["id"],))
        ag = [{"id": "cal", "naam": "Agenda", "primair": True, "schrijven": True, "aan": True, "bezet": True}]
        ms = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, domein, schrijven, agendas, schrijfagenda)
                          VALUES ('microsoft', 'IV-professional', 'fbim', true, %s, 'cal') RETURNING id""", (json.dumps(ag),)).fetchone()["id"]
        lees = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, domein, schrijven, agendas, schrijfagenda)
                            VALUES ('microsoft', 'Upper Spire', 'upperspire', false, %s, '') RETURNING id""", (json.dumps(ag),)).fetchone()["id"]
        ics = c.execute("INSERT INTO agenda_koppelingen (soort, naam, domein) VALUES ('ics', 'Gemeenteraad', 'politiek') RETURNING id").fetchone()["id"]
        gid = g["id"]
        item(gid, "primary", "p1", "Tandarts", morgen)
        item(gid, "primary", "p2", "Werkplek", morgen + dt.timedelta(hours=2), vrij=True)
        item(gid, "primary", "p3", "Verjaardag", morgen, uur=24, heledag=True)
        item(gid, "primary", "p4", "Nog beantwoorden", morgen + dt.timedelta(hours=3), antwoord="geen")
        item(gid, "primary", "p5", "Whaller Bestuur", morgen + dt.timedelta(hours=4), omschrijving="Whaller Bestuursvergadering")
        c.execute("UPDATE agenda_items SET organisator = 'Whaller Event' WHERE extern_id = 'p5'")
        item(gid, "primary", "p6", "Busy", morgen + dt.timedelta(hours=5), omschrijving="Created by CalendarBridge")
        item(gid, "sanne", "s1", "Zwemles", morgen)
        item(gid, "zh", "z1", "Keepersavond", morgen + dt.timedelta(days=1))  # agenda leest niet mee
        item(ms, "cal", "m1", "Klantoverleg", morgen + dt.timedelta(days=2), status="voorlopig")
        item(ms, "cal", "m2", "Gedeeld overleg", morgen + dt.timedelta(days=3), uid="GEDEELD@x")
        item(gid, "primary", "p7", "Gedeeld overleg", morgen + dt.timedelta(days=3), uid="gedeeld@X")
        item(ics, "", "r1", "Commissie", morgen + dt.timedelta(days=4))
        item(ms, "cal", "oud", "Bezet", morgen + dt.timedelta(days=5), omschrijving=spiegel.OMSCHRIJVING)  # blok van Clara zelf

        # Uit: niets gebeurt, maar het scherm laat zien wat er zou komen.
        assert spiegel.bijwerken(c) is None and not log
    pagina = klant.get("/clara/koppelingen", headers=KOP).text
    assert "Beschikbaarheid delen" in pagina and "CalendarBridge" in pagina
    assert "koppel opnieuw met lezen en schrijven" in pagina and "agendalink: alleen lezen" in pagina
    assert f'name="doel_{ms}"' in pagina and f'name="doel_{lees}"' not in pagina

    assert klant.post("/clara/spiegel", headers=KOP, data={"spiegel_aan": "1"}).status_code == 403
    r = klant.post("/clara/spiegel", headers=FORM, follow_redirects=False,
                   data={"spiegel_aan": "1", "spiegel_titel": "bezet", f"doel_{gid}": "1", f"doel_{ms}": "1"})
    assert r.status_code == 303 and "binnen+vijf+minuten" in r.headers["location"]

    with db.pool.connection() as c:
        assert "beschikbaarheid" in spiegel.bijwerken(c)
        naar_ms = sorted((x[3], x[4]) for x in log if x[0] == "m")
        naar_g = sorted((x[3], x[4], x[5]) for x in log if x[0] == "g")
        # Naar IV-professional: de tandarts, het gedeelde overleg niet (staat er al) en de commissie uit de agendalink.
        assert naar_ms == [("Bezet", morgen), ("Bezet", morgen + dt.timedelta(days=4))], naar_ms
        # Naar Google: het voorlopige klantoverleg en de commissie; niet het blok dat Clara zelf in Outlook zette.
        assert naar_g == [("Bezet", morgen + dt.timedelta(days=2), True), ("Bezet", morgen + dt.timedelta(days=4), False)], naar_g
        assert all(x[2] == ("cal" if x[0] == "m" else "primary") for x in log)
        # Nog een ronde: niets meer te doen.
        log.clear()
        assert spiegel.bijwerken(c) is None and not log
        # De blokken van Clara staan niet in de week en tellen niet als dubbele boeking.
        item(ms, "cal", "m-blok", "Bezet", morgen, omschrijving=spiegel.OMSCHRIJVING)
        titels = [a["titel"] for a in sync.afspraken(c, morgen - dt.timedelta(hours=1), morgen + dt.timedelta(days=6))]
        assert "Bezet" not in titels and "Tandarts" in titels
        assert spiegel.bijwerken(c) is None and not log

        # Afspraak verschoven: blok gaat mee. Afspraak weg: blok weg. Met de hand verwijderd blok: opnieuw.
        c.execute("UPDATE agenda_items SET start = start + interval '1 hour', eind = eind + interval '1 hour' WHERE extern_id = 'p1'")
        c.execute("DELETE FROM agenda_items WHERE extern_id = 'r1'")
        spiegel.bijwerken(c)
        soorten = sorted((x[0], x[1]) for x in log)
        assert soorten == [("g", "weg"), ("m", "weg"), ("m", "wijzig")], soorten
        log.clear()

        def kwijt(*a, **kw):
            if kw.get("extern_id"):
                raise bronnen.NietGevonden("weg")
            return "g-opnieuw"
        monkeypatch.setattr(bronnen, "google_blok", kwijt)
        c.execute("UPDATE agenda_items SET status = 'bevestigd' WHERE extern_id = 'm1'")
        spiegel.bijwerken(c)
        assert c.execute("SELECT extern_id FROM agenda_spiegel WHERE doel_koppeling = %s", (gid,)).fetchone()["extern_id"] == "g-opnieuw"
        monkeypatch.setattr(bronnen, "google_blok", maak("g"))

        # Titel tonen in plaats van "Bezet".
        inst = sync.instellingen(c)
        sync.instellingen_opslaan(c, {**inst, "spiegel_titel": "titel"})
        spiegel.bijwerken(c)
        assert sorted(x[3] for x in log) == ["Klantoverleg", "Tandarts"]
        log.clear()
    pagina = klant.get("/clara/koppelingen", headers=KOP).text
    assert "1 blok van Clara" in pagina and "Laatste ronde" in pagina

    # Eén doel uit: daar gaan de blokken weg. Alles uit: overal weg.
    klant.post("/clara/spiegel", headers=FORM, data={"spiegel_aan": "1", "spiegel_titel": "bezet", f"doel_{gid}": "1"})
    with db.pool.connection() as c:
        spiegel.bijwerken(c)
        assert [x[:2] for x in log if x[1] == "weg"] == [("m", "weg")]
        log.clear()
    klant.post("/clara/spiegel", headers=FORM, data={"spiegel_titel": "bezet", f"doel_{gid}": "1", f"doel_{ms}": "1"})
    with db.pool.connection() as c:
        spiegel.bijwerken(c)
        assert [x[:2] for x in log] == [("g", "weg")]
        assert c.execute("SELECT count(*) AS n FROM agenda_spiegel").fetchone()["n"] == 0
        c.execute("DELETE FROM agenda_koppelingen WHERE soort <> 'google'")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'clara%%'")


def test_spiegel_fout_bij_een_doel_houdt_de_rest_niet_op(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    from app.clara import spiegel, sync  # pylint: disable=import-outside-toplevel
    morgen = _nu_dag(1, 10)
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")

    def kapot(*a, **kw):
        raise bronnen.BronFout("Outlook gaf fout 403: Access is denied")
    gemaakt = []
    monkeypatch.setattr(bronnen, "ms_blok", kapot)
    monkeypatch.setattr(bronnen, "google_blok", lambda *a, **kw: gemaakt.append(a[1]) or "g1")
    with db.pool.connection() as c:
        g = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = 'google'").fetchone()
        ag = [{"id": "cal", "naam": "Agenda", "primair": True, "schrijven": True, "aan": True, "bezet": True}]
        ms = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, schrijven, agendas, schrijfagenda)
                          VALUES ('microsoft', 'Raad', true, %s, 'cal') RETURNING id""", (json.dumps(ag),)).fetchone()["id"]
        c.execute("DELETE FROM agenda_items")
        c.execute("DELETE FROM agenda_spiegel")
        for kid, agenda, titel, start in ((g["id"], "primary", "Eigen", morgen), (ms, "cal", "Raadsvergadering", morgen + dt.timedelta(days=1))):
            c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, start, eind)
                         VALUES (%s, %s, %s, %s, %s, %s)""", (kid, agenda, titel, titel, start, start + dt.timedelta(hours=1)))
        sync.instellingen_opslaan(c, {**sync.instellingen(c), "spiegel_aan": True})
        uitslag = spiegel.bijwerken(c)
        assert "Raad: Outlook gaf fout 403" in uitslag and gemaakt == ["primary"]
        assert c.execute("SELECT count(*) AS n FROM meldingen WHERE titel LIKE 'Beschikbaarheid naar Raad%%'").fetchone()["n"] == 1
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (ms,))
        c.execute("DELETE FROM agenda_items")
        c.execute("DELETE FROM agenda_spiegel")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'clara%%'")


def test_verkeerd_clientgeheim_duidelijk(monkeypatch):
    _http_fout(monkeypatch, 401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."})
    with pytest.raises(bronnen.BronFout) as fout:
        bronnen.ms_code("code", False)
    assert not isinstance(fout.value, bronnen.OpnieuwKoppelen)
    assert "AADSTS7000215" in str(fout.value) and "clientgeheim" in str(fout.value) and "clara.sh" in str(fout.value)


def test_mee_naar_afspraak_van_sanne(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    from app.clara import reis, spiegel, sync  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(sync, "reistijd_bijwerken", lambda c: None)
    morgen = _nu_dag(1, 13, 30)
    with db.pool.connection() as c:
        g = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = 'google'").fetchone()
        agendas = [{"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True, "aan": True, "bezet": True, "kleur": ""},
                   {"id": "sanne", "naam": "Sanne", "primair": False, "schrijven": True, "aan": True, "bezet": False, "kleur": "#795548"}]
        c.execute("UPDATE agenda_koppelingen SET agendas = %s, fout = NULL WHERE id = %s", (json.dumps(agendas), g["id"]))
        c.execute("DELETE FROM agenda_items")
        c.execute("DELETE FROM agenda_vervoer")
        iid = c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, ical_uid, titel, locatie, start, eind)
                           VALUES (%s, 'sanne', 'pr1', 'paard@g', 'Paardrijles Sanne', 'Manege Hooidonk, Nuenen', %s, %s) RETURNING id""",
                        (g["id"], morgen, morgen + dt.timedelta(hours=1))).fetchone()["id"]
        a = sync.afspraken(c, morgen - dt.timedelta(hours=1), morgen + dt.timedelta(hours=2))[0]
        assert a["niet_voor_mij"] and not a["mee"] and not reis.buiten_de_deur(a)
        assert f"id {iid} voor vervoer als hij meegaat" in gesprek.context(c)
    pagina = klant.get("/clara", headers=KOP).text
    assert "je gaat niet mee" in pagina and "mee, met de auto" in pagina

    r = klant.post(f"/clara/vervoer/{iid}", headers=FORM, data={"vervoer": "auto", "week": "0"}, follow_redirects=False)
    assert r.status_code == 303
    with db.pool.connection() as c:
        a = sync.afspraken(c, morgen - dt.timedelta(hours=1), morgen + dt.timedelta(hours=2))[0]
        assert a["mee"] and not a["niet_voor_mij"] and not a["vrij"] and reis.buiten_de_deur(a)
        assert "Martijn gaat mee" in gesprek.context(c)
        assert [b["titel"] for b in spiegel._bronnen(c, morgen - dt.timedelta(hours=1), morgen + dt.timedelta(hours=2))] == ["Paardrijles Sanne"]  # pylint: disable=protected-access
    assert ">je gaat mee</span>" in klant.get("/clara", headers=KOP).text

    # Niet meer mee: terug naar 'niet bezet voor jou'.
    klant.post(f"/clara/vervoer/{iid}", headers=FORM, data={"vervoer": "automatisch"})
    with db.pool.connection() as c:
        a = sync.afspraken(c, morgen - dt.timedelta(hours=1), morgen + dt.timedelta(hours=2))[0]
        assert a["niet_voor_mij"] and not a["mee"]
        assert not spiegel._bronnen(c, morgen - dt.timedelta(hours=1), morgen + dt.timedelta(hours=2))  # pylint: disable=protected-access
        c.execute("DELETE FROM agenda_items")


# ---------------------------------------------------------------- herhalende afspraken

def test_herhaling_regels_en_verzoeken(monkeypatch):
    h = bronnen.herhaal_normaal({"dagen": ["za", "Woensdag", "do", "xx"], "tot": "2027-06-30"})
    assert h == {"dagen": ["wo", "do", "za"], "tot": "2027-06-30"}
    assert bronnen.herhaal_normaal({"dagen": []}) is None and bronnen.herhaal_normaal("wo") is None
    assert bronnen.herhaal_normaal({"dagen": ["wo"], "tot": "ooit"}) == {"dagen": ["wo"], "tot": ""}
    assert bronnen.herhaal_tekst(h) == "elke woensdag, donderdag en zaterdag tot en met 30-06-2027"
    maandag = dt.datetime(2026, 11, 2, 16, 0, tzinfo=TZ)
    assert bronnen.eerste_keer(maandag, h) == dt.timedelta(days=2)
    assert bronnen.eerste_keer(maandag, {"dagen": ["ma"], "tot": ""}) == dt.timedelta(0)
    assert bronnen._rrule(h) == "RRULE:FREQ=WEEKLY;WKST=MO;BYDAY=WE,TH,SA;UNTIL=20270630T215959Z"  # zomertijd  # pylint: disable=protected-access
    assert bronnen._rrule({"dagen": ["wo"], "tot": "2026-12-31"}).endswith("UNTIL=20261231T225959Z")  # pylint: disable=protected-access
    verzoeken = []
    monkeypatch.setattr(bronnen, "_http", lambda url, **kw: verzoeken.append((url, kw)) or {"id": "r1"})
    woensdag = maandag + dt.timedelta(days=2)
    assert bronnen.google_maak("tok", "primary", "🚗 Sanne brengen", woensdag, woensdag + dt.timedelta(minutes=30), herhaal=h) == "r1"
    assert verzoeken[-1][1]["body"]["recurrence"] == [bronnen._rrule(h)]  # pylint: disable=protected-access
    bronnen.google_reeks("tok", "primary", "r1", {"dagen": ["wo"], "tot": "2026-12-01"})
    assert verzoeken[-1][1]["methode"] == "PATCH" and verzoeken[-1][1]["body"]["recurrence"][0].endswith("UNTIL=20261201T225959Z")
    bronnen.ms_maak("tok", "cal", "Brengen", woensdag, woensdag + dt.timedelta(minutes=30), herhaal={"dagen": ["wo", "za"], "tot": ""})
    r = verzoeken[-1][1]["body"]["recurrence"]
    assert r["pattern"]["daysOfWeek"] == ["wednesday", "saturday"] and r["range"] == {
        "type": "noEnd", "startDate": "2026-11-04", "recurrenceTimeZone": "W. Europe Standard Time"}
    bronnen.ms_reeks("tok", "cal", "m1", {"dagen": ["wo"], "tot": "2026-12-01"}, woensdag)
    assert verzoeken[-1][0].endswith("/me/events/m1") and verzoeken[-1][1]["body"]["recurrence"]["range"]["endDate"] == "2026-12-01"
    # Bij het ophalen: bij welke reeks hoort een afspraak.
    e = {"id": "r1_20261104T150000Z", "recurringEventId": "r1", "summary": "x", "start": {"dateTime": "2026-11-04T16:00:00+01:00"},
         "end": {"dateTime": "2026-11-04T16:30:00+01:00"}}
    assert bronnen.google_item(e, "primary")["reeks"] == "r1"


def test_voorstel_met_herhaling_uit_antwoord():
    tekst, v = gesprek.voorstellen_uit('Zo doen we het. <voorstel>{"titel": "🚗 Sanne naar paardrijles brengen", '
                                       '"start": "2026-11-04T15:40", "eind": "2026-11-04T16:20", "domein": "prive", '
                                       '"herhaal": {"dagen": ["wo"]}, "vervangt": "3"}</voorstel>')
    assert tekst == "Zo doen we het." and v[0]["herhaal"] == {"dagen": ["wo"], "tot": ""} and v[0]["vervangt"] == 3
    assert gesprek.voorstellen_uit('<voorstel>{"titel": "x", "start": "2026-11-04T15:40", "eind": "2026-11-04T16:20"}</voorstel>')[1][0]["herhaal"] is None
    assert "<reeks>" in gesprek.UITLEG and "herhaal" in gesprek.UITLEG


def test_reeks_inplannen_overslaan_stoppen_vervangen(klant, monkeypatch):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    from app.clara import sync  # pylint: disable=import-outside-toplevel
    gemaakt, verwijderd, gewijzigd = [], [], []
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k, forceer=False, nu=None: None)
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="", kleur=None, herhaal=None:
                        gemaakt.append((a, titel, s, herhaal)) or f"serie{len(gemaakt)}")
    monkeypatch.setattr(bronnen, "google_verwijder", lambda tok, a, eid: verwijderd.append(eid))
    monkeypatch.setattr(bronnen, "google_reeks", lambda tok, a, eid, h, eerste=None: gewijzigd.append((eid, h)))
    maandag = _nu_dag((7 - dt.datetime.now(TZ).weekday()) % 7 or 7, 15, 40)  # een maandag in de toekomst
    with db.pool.connection() as c:
        g = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = 'google'").fetchone()
        c.execute("DELETE FROM agenda_reeksen")
        c.execute("DELETE FROM agenda_items")
        gid = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('clara', 'Paardrijles') RETURNING id").fetchone()["id"]
        vid = c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, domein, herhaal)
                           VALUES (%s, '🚗 Sanne naar paardrijles brengen', %s, %s, 'prive', %s) RETURNING id""",
                        (gid, maandag, maandag + dt.timedelta(minutes=40), json.dumps({"dagen": ["wo"], "tot": ""}))).fetchone()["id"]
    pagina = klant.get(f"/clara?g={gid}", headers=KOP).text
    assert "elke woensdag, 15.40–16.20" in pagina and "of Ctrl-Enter" in pagina and "metaKey" in pagina
    r = klant.post(f"/clara/voorstel/{vid}", headers=FORM, data={"actie": "inplannen", "doel": f"{g['id']}|primary"}, follow_redirects=False)
    assert "elke+woensdag" in r.headers["location"], r.headers["location"]
    woensdag = maandag + dt.timedelta(days=2)
    assert gemaakt == [("primary", "🚗 Sanne naar paardrijles brengen", woensdag, {"dagen": ["wo"], "tot": ""})]
    with db.pool.connection() as c:
        reeks = c.execute("SELECT * FROM agenda_reeksen").fetchone()
        assert reeks["extern_id"] == "serie1" and reeks["start"] == woensdag and reeks["voor"] == "Martijn"
        for n in range(3):  # zo komen de keren na het ophalen binnen
            s = woensdag + dt.timedelta(days=7 * n)
            c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, start, eind, reeks)
                         VALUES (%s, 'primary', %s, '🚗 Sanne naar paardrijles brengen', %s, %s, 'serie1')""",
                      (g["id"], f"serie1_{n}", s, s + dt.timedelta(minutes=40)))
        assert f"[reeks {reeks['id']}] 🚗 Sanne naar paardrijles brengen, elke woensdag, 15.40-16.20, in agenda Martijn" in gesprek.context(c)
    pagina = klant.get("/clara", headers=KOP).text
    assert "Herhalende afspraken" in pagina and "Deze keer overslaan" in pagina

    # Eén keer overslaan (direct in het scherm).
    tweede = (woensdag + dt.timedelta(days=7)).date()
    r = klant.post(f"/clara/reeks/{reeks['id']}", headers=FORM, data={"actie": "overslaan", "datum": tweede.isoformat()}, follow_redirects=False)
    assert "vervalt" in r.headers["location"] and verwijderd == ["serie1_1"]
    r = klant.post(f"/clara/reeks/{reeks['id']}", headers=FORM, data={"actie": "overslaan", "datum": "2030-01-01"}, follow_redirects=False)
    assert "fout=" in r.headers["location"]

    # Clara stelt voor te stoppen; Martijn klikt.
    with db.pool.connection() as c:
        tekst, acties = gesprek.reeksacties_uit(c, f'Goed. <reeks>{{"id": {reeks["id"]}, "actie": "stop", "datum": "{tweede.isoformat()}"}}</reeks>')
        assert tekst == "Goed." and acties[0]["soort"] == "stop"
        assert gesprek.reeksacties_uit(c, '<reeks>{"id": 99999, "actie": "stop", "datum": "2026-12-01"}</reeks>')[1] == []
        vid = c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, soort, reeks_id, datum)
                           VALUES (%s, %s, %s, %s, 'stop', %s, %s) RETURNING id""",
                        (gid, reeks["titel"], acties[0]["start"], acties[0]["eind"], reeks["id"], tweede)).fetchone()["id"]
    assert "stoppen vanaf" in klant.get(f"/clara?g={gid}", headers=KOP).text
    klant.post(f"/clara/voorstel/{vid}", headers=FORM, data={"actie": "inplannen"})
    assert gewijzigd == [("serie1", {"dagen": ["wo"], "tot": (tweede - dt.timedelta(days=1)).isoformat()})]

    # Andere tijd: nieuwe reeks vervangt de oude vanaf de eerste nieuwe keer.
    with db.pool.connection() as c:
        s = woensdag + dt.timedelta(days=14, minutes=-20)
        c.execute("UPDATE agenda_reeksen SET herhaal = %s WHERE id = %s", (json.dumps({"dagen": ["wo"], "tot": ""}), reeks["id"]))
        vid = c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, domein, herhaal, reeks_id)
                           VALUES (%s, '🚗 Sanne brengen', %s, %s, 'prive', %s, %s) RETURNING id""",
                        (gid, s, s + dt.timedelta(minutes=40), json.dumps({"dagen": ["wo"], "tot": ""}), reeks["id"])).fetchone()["id"]
    gewijzigd.clear()
    r = klant.post(f"/clara/voorstel/{vid}", headers=FORM, data={"actie": "inplannen", "doel": f"{g['id']}|primary"}, follow_redirects=False)
    assert "oude+reeks+stopt" in r.headers["location"]
    assert gewijzigd == [("serie1", {"dagen": ["wo"], "tot": (s.date() - dt.timedelta(days=1)).isoformat()})]

    # Hele reeks verwijderen.
    with db.pool.connection() as c:
        nieuw = c.execute("SELECT id FROM agenda_reeksen WHERE extern_id = 'serie2'").fetchone()["id"]
    klant.post(f"/clara/reeks/{nieuw}", headers=FORM, data={"actie": "verwijder"})
    assert verwijderd[-1] == "serie2"
    with db.pool.connection() as c:
        assert c.execute("SELECT status FROM agenda_reeksen WHERE id = %s", (nieuw,)).fetchone()["status"] == "verwijderd"
        c.execute("DELETE FROM agenda_reeksen")
        c.execute("DELETE FROM agenda_items")


# ---------------------------------------------------------------- reistijd per afspraak (ook elke keer van een reeks)

def test_eigen_reisadres_telt():
    from app.clara import reis  # pylint: disable=import-outside-toplevel
    a = afspraak("Overleg", t(4, 10), t(4, 11), locatie="https://teams.microsoft.com/l/abc")
    assert a["online"] and not reis.buiten_de_deur(a)
    a["reisadres"] = "Stadhuisplein 1, Eindhoven"
    assert reis.buiten_de_deur(a) and reis.plek_tekst(a) == "Stadhuisplein 1, Eindhoven" and reis.plek_naam(a) == "Stadhuisplein 1, Eindhoven"
    assert not reis.buiten_de_deur(afspraak("Zonder plek", t(4, 10), t(4, 11)))


def test_reistijd_per_afspraak_en_elke_keer(klant, monkeypatch):
    from app.clara import reis, sync  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(config, "ORS_API_KEY", "ors-test")
    monkeypatch.setattr(config, "GOOGLE_MAPS_KEY", "")
    eindhoven = (5.4797, 51.4416)

    def nep_ors(pad, params=None, body=None):
        if pad.startswith("/pelias"):
            punt = THUIS if "Strik" in params["text"] else (eindhoven if "Eindhoven" in params["text"] else None)
            return {"features": [{"geometry": {"coordinates": list(punt)}, "properties": {"label": params["text"], "confidence": 0.9}}]} if punt else {"features": []}
        return {"routes": [{"summary": {"duration": 1500, "distance": 15000}}]}
    monkeypatch.setattr(reis, "_ors", nep_ors)
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")
    gemaakt = []
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_items")
        c.execute("DELETE FROM agenda_reisinfo")
        c.execute("DELETE FROM agenda_vervoer")
        c.execute("DELETE FROM plekken WHERE sleutel LIKE '%%nergens%%'")
        c.execute("UPDATE agenda_koppelingen SET schrijven = false")
        sync.instellingen_opslaan(c, {**sync.instellingen(c), "vertrekpunt": "Meester Strikstraat 25, 5708 GA Helmond", "marge": 5})
        k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, domein, schrijven, agendas, schrijfagenda, laatst)
                         VALUES ('google', 'Reis-per-afspraak', 'prive', true, %s, 'primary', now()) RETURNING *""",
                      (json.dumps([{"id": "primary", "naam": "M", "primair": True, "schrijven": True, "aan": True, "bezet": True}]),)).fetchone()
        kid = k["id"]
        morgen = _nu_dag(2, 14)
        los = c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, start, eind)
                           VALUES (%s, 'primary', 'los1', 'Overleg zonder adres', %s, %s) RETURNING id""",
                        (kid, morgen, morgen + dt.timedelta(hours=1))).fetchone()["id"]
        reeks_ids = [c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, ical_uid, titel, locatie, start, eind, reeks)
                                  VALUES (%s, 'primary', %s, 'les@x', 'Cursus', 'Stadhuisplein 1, Eindhoven', %s, %s, 'les') RETURNING id""",
                                (kid, f"les_{n}", morgen + dt.timedelta(days=7 * n, hours=5), morgen + dt.timedelta(days=7 * n, hours=7))).fetchone()["id"]
                     for n in range(3)]

    teller = iter(range(1000))

    def maak(tok, agenda_id, titel, s, e, o="", kleur=None, **kw):
        gemaakt.append((titel, s))
        eid = f"blok{next(teller)}"
        with db.pool.connection() as c2:  # zoals na het ophalen: het blok staat dan in de agenda
            c2.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, start, eind, omschrijving)
                          VALUES (%s, %s, %s, %s, %s, %s, %s)""", (kid, agenda_id, eid, titel, s, e, o))
        return eid
    monkeypatch.setattr(bronnen, "google_maak", maak)
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k, forceer=False, nu=None: None)

    pagina = klant.get("/clara", headers=KOP).text
    assert f'href="/clara/reistijd/{los}?week=0">＋ Reistijd' in pagina
    r = klant.get(f"/clara/reistijd/{los}", headers=KOP)
    assert r.status_code == 200 and "Waar ga je naartoe" in r.text and 'name="elke"' not in r.text
    # Onbekend adres: het formulier komt terug met de vraag erbij.
    r = klant.post(f"/clara/reistijd/{los}", headers=FORM, data={"adres": "Nergensstraat 99", "vervoer": "automatisch"})
    assert r.status_code == 200 and "vindt OpenRouteService niet" in r.text and "Nergensstraat 99" in r.text and not gemaakt
    assert klant.post(f"/clara/reistijd/{los}", headers=KOP, data={"adres": "x"}).status_code == 403
    r = klant.post(f"/clara/reistijd/{los}", headers=FORM, follow_redirects=False,
                   data={"adres": "Stadhuisplein 1, Eindhoven", "vervoer": "auto", "week": "0"})
    assert r.status_code == 303 and "ingepland" in r.headers["location"], r.headers["location"]
    assert [x[0] for x in gemaakt] == ["🚗 Reistijd naar Stadhuisplein 1, Eindhoven (rijden)", "🚗 Terugreis naar huis (rijden)"]
    assert gemaakt[0][1] == morgen - dt.timedelta(minutes=30)
    with db.pool.connection() as c:
        assert c.execute("SELECT adres, auto FROM agenda_reisinfo WHERE sleutel = %s", (f"id:{kid}:los1",)).fetchone() == {
            "adres": "Stadhuisplein 1, Eindhoven", "auto": False}

    # Reeks: elke keer, als herhalende reistijd voor de hele reeks.
    gemaakt.clear()
    herhalingen = []
    dag = list(bronnen.DAGCODES)[(morgen + dt.timedelta(hours=5)).weekday()]
    monkeypatch.setattr(bronnen, "google_reeks_ophalen", lambda tok, agenda, rid: {
        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={bronnen.DAGCODES[dag][0]};COUNT=10"],
        "start": {"dateTime": (morgen + dt.timedelta(hours=5)).isoformat()}})

    def maak_reeks(tok, agenda_id, titel, s, e, o="", kleur=None, herhaal=None):
        herhalingen.append(herhaal)
        return maak(tok, agenda_id, titel, s, e, o, kleur)
    monkeypatch.setattr(bronnen, "google_maak", maak_reeks)
    r = klant.get(f"/clara/reistijd/{reeks_ids[0]}", headers=KOP)
    assert 'name="elke"' in r.text and "herhalende afspraak" in r.text and 'value="Stadhuisplein 1, Eindhoven"' in r.text
    r = klant.post(f"/clara/reistijd/{reeks_ids[0]}", headers=FORM, follow_redirects=False,
                   data={"adres": "Stadhuisplein 1, Eindhoven", "vervoer": "automatisch", "elke": "1", "week": "0"})
    assert "elke+keer" in r.headers["location"] and "Herhalende+afspraken" in r.headers["location"], r.headers["location"]
    laatste = (morgen + dt.timedelta(hours=5, days=63)).date().isoformat()
    assert len(gemaakt) == 2 and herhalingen == [{"dagen": [dag], "tot": laatste}] * 2  # heen en terug, tien keer
    with db.pool.connection() as c:
        rr = c.execute("SELECT * FROM agenda_reeksen WHERE soort = 'reistijd' ORDER BY id").fetchall()
        assert len(rr) == 2 and all(x["bron_koppeling"] == kid and x["bron_reeks"] == "les" for x in rr)
        assert not c.execute("SELECT auto FROM agenda_reisinfo WHERE sleutel = 'uid:les@x'").fetchone()["auto"]
        # De keren van de reeks gelden nu als ingepland: geen losse blokken, ook niet automatisch.
        p = sync.reisplan(c, morgen, morgen + dt.timedelta(days=20))
        assert all(x["ingepland"] for x in p["regels"] if x["afspraak"].get("reeks") == "les")
        assert sync.reistijd_automatisch(c) is None and len(gemaakt) == 2
        # Valt een keer weg, dan gaat de reistijd die dag ook weg (alleen als de bron goed is bijgewerkt).
        heen = rr[0]
        for n in range(3):
            s0 = heen["start"] + dt.timedelta(days=7 * n)
            c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, start, eind, reeks, omschrijving)
                         VALUES (%s, 'primary', %s, %s, %s, %s, %s, '[clara-reistijd:x] reeks')""",
                      (kid, f"{heen['extern_id']}_{n}", heen["titel"], s0, s0 + (heen["eind"] - heen["start"]), heen["extern_id"]))
        c.execute("DELETE FROM agenda_items WHERE extern_id = 'les_1'")
        weggehaald = []
        monkeypatch.setattr(bronnen, "google_verwijder", lambda tok, a, eid: weggehaald.append(eid))
        c.execute("UPDATE agenda_koppelingen SET laatst = now() - interval '2 hours' WHERE id = %s", (kid,))
        assert sync.reistijdreeksen_opruimen(c) is None and not weggehaald  # bron niet vers bijgewerkt: niets weg
        c.execute("UPDATE agenda_koppelingen SET laatst = now() WHERE id = %s", (kid,))
        assert "1 vervallen" in sync.reistijdreeksen_opruimen(c) and weggehaald == [f"{heen['extern_id']}_1"]
    assert "Herhalende afspraken" in klant.get("/clara", headers=KOP).text

    # Een reeks die maandelijks herhaalt: dan steeds vier weken vooruit.
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_reeksen")
        c.execute("DELETE FROM agenda_items WHERE omschrijving LIKE '[clara-reistijd%%'")
    gemaakt.clear()
    monkeypatch.setattr(bronnen, "google_reeks_ophalen", lambda tok, agenda, rid: {
        "recurrence": ["RRULE:FREQ=MONTHLY;BYMONTHDAY=4"], "start": {"dateTime": (morgen + dt.timedelta(hours=5)).isoformat()}})
    r = klant.post(f"/clara/reistijd/{reeks_ids[0]}", headers=FORM, follow_redirects=False,
                   data={"adres": "Stadhuisplein 1, Eindhoven", "vervoer": "automatisch", "elke": "1", "week": "0"})
    assert "vier+weken+vooruit" in r.headers["location"], r.headers["location"]
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_items WHERE extern_id = 'les_1'")
    assert len(gemaakt) >= 4
    with db.pool.connection() as c:
        assert c.execute("SELECT auto FROM agenda_reisinfo WHERE sleutel = 'uid:les@x'").fetchone()["auto"]
    klant.post(f"/clara/reistijd/{reeks_ids[0]}/stop", headers=FORM, data={"week": "0"})
    with db.pool.connection() as c:
        assert not c.execute("SELECT auto FROM agenda_reisinfo WHERE sleutel = 'uid:les@x'").fetchone()["auto"]
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (kid,))
        c.execute("UPDATE agenda_koppelingen SET schrijven = true WHERE soort = 'google'")
        c.execute("DELETE FROM agenda_reisinfo")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'clara%%'")


def test_herhaling_overnemen_van_google_en_outlook():
    e = {"recurrence": ["EXDATE;TZID=Europe/Amsterdam:20261111T190000", "RRULE:FREQ=WEEKLY;BYDAY=WE,SA;UNTIL=20270101T225959Z"],
         "start": {"dateTime": "2026-11-04T19:00:00+01:00"}}
    assert bronnen.herhaal_uit_google(e) == {"dagen": ["wo", "za"], "tot": "2027-01-01"}
    e2 = {"recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=3"], "start": {"dateTime": "2026-11-04T19:00:00+01:00"}}
    assert bronnen.herhaal_uit_google(e2) == {"dagen": ["wo"], "tot": "2026-12-02", "interval": 2}
    assert bronnen.herhaal_uit_google({"recurrence": ["RRULE:FREQ=DAILY"], "start": {"dateTime": "2026-11-04T19:00:00+01:00"}})["dagen"] == list(bronnen.DAGCODES)
    assert bronnen.herhaal_uit_google({"recurrence": ["RRULE:FREQ=MONTHLY;BYDAY=1WE"], "start": {"dateTime": "2026-11-04T19:00:00+01:00"}}) is None
    assert bronnen.herhaal_uit_google({"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=1WE"], "start": {"dateTime": "2026-11-04T19:00:00+01:00"}}) is None
    m = {"recurrence": {"pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["thursday"]},
                        "range": {"type": "numbered", "startDate": "2026-11-05", "numberOfOccurrences": 4}}}
    assert bronnen.herhaal_uit_ms(m) == {"dagen": ["do"], "tot": "2026-11-26"}
    m["recurrence"]["range"] = {"type": "noEnd", "startDate": "2026-11-05"}
    assert bronnen.herhaal_uit_ms(m) == {"dagen": ["do"], "tot": ""}
    m["recurrence"]["pattern"] = {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 5}
    assert bronnen.herhaal_uit_ms(m) is None
    assert bronnen.herhaal_tekst({"dagen": ["wo"], "tot": "", "interval": 2}) == "om de 2 weken op woensdag"
    assert ";INTERVAL=2" in bronnen._rrule({"dagen": ["wo"], "tot": "", "interval": 2})  # pylint: disable=protected-access


def test_geen_reistijdknop_bij_online_of_thuis(klant, monkeypatch):
    from app.clara import reis, sync  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(reis, "actief", lambda: True)
    monkeypatch.setattr(reis, "adres", lambda c, tekst, focus=None, alleen_cache=False: (
        {"punt": THUIS, "label": "thuis"} if "strik" in tekst.lower() else None))
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_items")
        sync.instellingen_opslaan(c, {**sync.instellingen(c), "vertrekpunt": "Meester Strikstraat 25, Helmond"})
        k = c.execute("SELECT id FROM agenda_koppelingen WHERE soort = 'google' LIMIT 1").fetchone()["id"]
        s = _nu_dag(1, 10)
        ids = {}
        for n, (titel, locatie, online) in enumerate([("Teams-overleg", "Microsoft Teams-vergadering", True), ("Thuiswerken", "Thuis", False),
                                                        ("Bij mij", "Meester Strikstraat 25, Helmond", False), ("Zonder plek", "", False)]):
            ids[titel] = c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, locatie, online, start, eind)
                                      VALUES (%s, 'primary', %s, %s, %s, %s, %s, %s) RETURNING id""",
                                   (k, f"o{n}", titel, locatie, online, s + dt.timedelta(hours=n), s + dt.timedelta(hours=n, minutes=30))).fetchone()["id"]
    pagina = klant.get("/clara", headers=KOP).text
    for titel in ("Teams-overleg", "Thuiswerken", "Bij mij"):
        assert f"/clara/reistijd/{ids[titel]}?" not in pagina, titel
    assert f"/clara/reistijd/{ids['Zonder plek']}?week=0\">＋ Reistijd" in pagina
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_items")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'clara%%'")


def test_brengen_en_ophalen_voor_afspraak_van_sanne(klant, monkeypatch):
    from app.clara import reis, sync, web  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(config, "ORS_API_KEY", "ors-test")
    monkeypatch.setattr(config, "GOOGLE_MAPS_KEY", "")
    nuenen = (5.5500, 51.4700)

    def nep_ors(pad, params=None, body=None):
        if pad.startswith("/pelias"):
            punt = THUIS if "Strik" in params["text"] else (nuenen if "Nuenen" in params["text"] else None)
            return {"features": [{"geometry": {"coordinates": list(punt)}, "properties": {"label": params["text"], "confidence": 0.9}}]} if punt else {"features": []}
        return {"routes": [{"summary": {"duration": 900, "distance": 9000}}]}  # 15 minuten
    monkeypatch.setattr(reis, "_ors", nep_ors)
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k, forceer=False, nu=None: None)
    gemaakt, teller = [], iter(range(1000))

    def maak(tok, agenda_id, titel, s, e, o="", kleur=None, herhaal=None):
        eid = f"rit{next(teller)}"
        gemaakt.append((agenda_id, titel, s, e, herhaal))
        with db.pool.connection() as c2:
            c2.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, start, eind, omschrijving)
                          VALUES (%s, %s, %s, %s, %s, %s, %s)""", (kid, agenda_id, eid, titel, s, e, o))
        return eid
    monkeypatch.setattr(bronnen, "google_maak", maak)
    with db.pool.connection() as c:
        for t_ in ("agenda_items", "agenda_reisinfo", "agenda_vervoer", "agenda_reeksen"):
            c.execute(f"DELETE FROM {t_}")
        c.execute("UPDATE agenda_koppelingen SET schrijven = false")
        sync.instellingen_opslaan(c, {**sync.instellingen(c), "vertrekpunt": "Meester Strikstraat 25, 5708 GA Helmond", "marge": 5})
        agendas = [{"id": "sanne", "naam": "Sanne", "primair": False, "schrijven": True, "aan": True, "bezet": False},
                   {"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True, "aan": True, "bezet": True}]
        k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, domein, schrijven, agendas, schrijfagenda, laatst)
                         VALUES ('google', 'Privé', 'prive', true, %s, 'primary', now()) RETURNING *""", (json.dumps(agendas),)).fetchone()
        kid = k["id"]
        assert [w for w, _l, _d in web._reisdoelen([k])] == [f"{kid}|primary"]  # nooit in Sanne's agenda  # pylint: disable=protected-access
        les = _nu_dag(2, 13, 30)
        ids = [c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, ical_uid, titel, locatie, start, eind, reeks)
                            VALUES (%s, 'sanne', %s, 'paard@g', 'Paardrijles', 'Manege Hooidonk, Nuenen', %s, %s, 'paard') RETURNING id""",
                         (kid, f"paard_{n}", les + dt.timedelta(days=7 * n), les + dt.timedelta(days=7 * n, hours=1))).fetchone()["id"]
               for n in range(2)]
        kort = c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, locatie, start, eind)
                            VALUES (%s, 'sanne', 'kort1', 'Tandarts', 'Nuenen centrum', %s, %s) RETURNING id""",
                         (kid, les + dt.timedelta(hours=4), les + dt.timedelta(hours=4, minutes=30))).fetchone()["id"]
    week = 0  # de week begint vandaag
    pagina = klant.get("/clara", headers=KOP).text + klant.get("/clara?week=1", headers=KOP).text
    assert f"/clara/reistijd/{ids[0]}?week=" in pagina and "Brengen en ophalen" in pagina
    r = klant.get(f"/clara/reistijd/{ids[0]}", headers=KOP)
    assert 'value="beide" checked' in r.text and "agenda van Sanne" in r.text and "Berekenen en inplannen" in r.text

    # Eén keer wegbrengen en ophalen.
    r = klant.post(f"/clara/reistijd/{ids[0]}", headers=FORM, follow_redirects=False,
                   data={"adres": "Manege Hooidonk, Nuenen", "vervoer": "auto", "modus": "beide", "week": str(week)})
    assert r.status_code == 303 and "In+je+agenda" in r.headers["location"], r.headers["location"]
    m = dt.timedelta(minutes=1)
    assert gemaakt == [("primary", "🚗 Sanne brengen (Paardrijles)", les - 20 * m, les + 15 * m, None),
                       ("primary", "🚗 Sanne ophalen (Paardrijles)", les + 45 * m, les + 80 * m, None)]
    assert "ritten ingepland" in klant.get(f"/clara?week={week}", headers=KOP).text
    r = klant.post(f"/clara/reistijd/{ids[0]}", headers=FORM, follow_redirects=False,
                   data={"adres": "Manege Hooidonk, Nuenen", "vervoer": "auto", "modus": "beide", "week": str(week)})
    assert "stonden+al" in r.headers["location"] and len(gemaakt) == 2

    # Korte afspraak: één blok, je blijft er tussendoor.
    gemaakt.clear()
    klant.post(f"/clara/reistijd/{kort}", headers=FORM, data={"adres": "Nuenen centrum", "vervoer": "auto", "modus": "beide"})
    assert [(x[1], x[2], x[3]) for x in gemaakt] == [("🚗 Sanne brengen en ophalen (Tandarts)", les + 4 * 60 * m - 20 * m, les + 4 * 60 * m + 50 * m)]

    # Elke keer: herhalende ritten voor de hele reeks.
    gemaakt.clear()
    dag = list(bronnen.DAGCODES)[les.weekday()]
    monkeypatch.setattr(bronnen, "google_reeks_ophalen", lambda tok, agenda, rid: {
        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={bronnen.DAGCODES[dag][0]}"], "start": {"dateTime": les.isoformat()}})
    r = klant.post(f"/clara/reistijd/{ids[1]}", headers=FORM, follow_redirects=False,
                   data={"adres": "Manege Hooidonk, Nuenen", "vervoer": "auto", "modus": "halen", "elke": "1"})
    assert "Valt+een+keer+weg" in r.headers["location"], r.headers["location"]
    assert [(x[0], x[1], x[4]) for x in gemaakt] == [("primary", "🚗 Sanne ophalen (Paardrijles)", {"dagen": [dag], "tot": ""})]
    with db.pool.connection() as c:
        rr = c.execute("SELECT * FROM agenda_reeksen").fetchone()
        assert rr["soort"] == "reistijd" and rr["bron_reeks"] == "paard" and rr["agenda_id"] == "primary"
    r = klant.post(f"/clara/reistijd/{ids[1]}", headers=FORM, follow_redirects=False,
                   data={"adres": "Manege Hooidonk, Nuenen", "vervoer": "auto", "modus": "beide", "elke": "1"})
    assert "al+als+reeks" in r.headers["location"] and len(gemaakt) == 1

    # Meegaan: dan is het zijn afspraak, met gewone reistijd.
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_reeksen")
        c.execute("DELETE FROM agenda_items WHERE omschrijving LIKE '[clara-reistijd%%'")
    gemaakt.clear()
    klant.post(f"/clara/reistijd/{ids[0]}", headers=FORM, data={"adres": "Manege Hooidonk, Nuenen", "vervoer": "fiets", "modus": "mee"})
    assert [x[1] for x in gemaakt] == ["🚲 Reistijd naar Manege Hooidonk, Nuenen (fietsen)", "🚲 Terugreis naar huis (fietsen)"]
    with db.pool.connection() as c:
        assert reis.keuzes(c)["uid:paard@g"] == "fiets"
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (kid,))
        c.execute("UPDATE agenda_koppelingen SET schrijven = true WHERE soort = 'google'")
        for t_ in ("agenda_items", "agenda_reisinfo", "agenda_vervoer", "agenda_reeksen"):
            c.execute(f"DELETE FROM {t_}")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'clara%%'")


def test_agenda_compact_twee_weken(klant):
    from app.kern import db  # pylint: disable=import-outside-toplevel
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_items")
        k = c.execute("SELECT id FROM agenda_koppelingen WHERE soort = 'google' LIMIT 1").fetchone()["id"]
        s = _nu_dag(12, 10)
        iid = c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, titel, locatie, start, eind, link)
                           VALUES (%s, 'primary', 'ver1', 'Afspraak over twaalf dagen', 'Ergens', %s, %s, 'https://calendar.google.com/x')
                           RETURNING id""", (k, s, s + dt.timedelta(hours=1))).fetchone()["id"]
    pagina = klant.get("/clara", headers=KOP).text
    assert "Afspraak over twaalf dagen" in pagina and pagina.count('class="dagkop"') == 14
    assert f'id="a{iid}">' in pagina and "<summary>" in pagina and "openen in Google Agenda" in pagina and "alles open" in pagina
    assert "Afspraak over twaalf dagen" not in klant.get("/clara?week=1", headers=KOP).text
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_items")


def test_van_meegaan_naar_brengen_en_ophalen(klant, monkeypatch):
    from app.clara import reis, sync  # pylint: disable=import-outside-toplevel
    from app.kern import db  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(config, "ORS_API_KEY", "ors-test")
    monkeypatch.setattr(config, "GOOGLE_MAPS_KEY", "")
    monkeypatch.setattr(reis, "_ors", lambda pad, params=None, body=None: (
        {"features": [{"geometry": {"coordinates": list(THUIS if "Strik" in params["text"] else (5.55, 51.47))},
                       "properties": {"label": params["text"], "confidence": 0.9}}]} if pad.startswith("/pelias")
        else {"routes": [{"summary": {"duration": 900, "distance": 9000}}]}))
    monkeypatch.setattr(sync, "toegang", lambda c, k: "tok")
    monkeypatch.setattr(sync, "synchroniseer", lambda c, k, forceer=False, nu=None: None)
    gemaakt = []
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="", kleur=None, herhaal=None: gemaakt.append((a, titel)) or f"x{len(gemaakt)}")
    with db.pool.connection() as c:
        for t_ in ("agenda_items", "agenda_reisinfo", "agenda_vervoer", "agenda_reeksen"):
            c.execute(f"DELETE FROM {t_}")
        sync.instellingen_opslaan(c, {**sync.instellingen(c), "vertrekpunt": "Meester Strikstraat 25, 5708 GA Helmond"})
        agendas = [{"id": "primary", "naam": "Martijn", "primair": True, "schrijven": True, "aan": True, "bezet": True},
                   {"id": "sanne", "naam": "Sanne", "primair": False, "schrijven": True, "aan": True, "bezet": False}]
        kid = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, domein, schrijven, agendas, schrijfagenda, laatst)
                           VALUES ('google', 'Privé2', 'prive', true, %s, 'primary', now()) RETURNING id""", (json.dumps(agendas),)).fetchone()["id"]
        les = _nu_dag(0, 23, 0)
        iid = c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, ical_uid, titel, locatie, start, eind)
                           VALUES (%s, 'sanne', 'za1', 'za@g', 'Paardrijles Sanne', 'Manege Hooidonk, Nuenen', %s, %s) RETURNING id""",
                        (kid, les, les + dt.timedelta(minutes=50))).fetchone()["id"]
    # Vanochtend 'mee, met de auto' gekozen: de afspraak is dan van Martijn, maar brengen en ophalen kan nog steeds.
    klant.post(f"/clara/vervoer/{iid}", headers=FORM, data={"vervoer": "auto"})
    pagina = klant.get("/clara", headers=KOP).text
    assert f'href="/clara/reistijd/{iid}?week=0">🚗 Brengen en ophalen' in pagina
    r = klant.get(f"/clara/reistijd/{iid}", headers=KOP)
    assert 'value="mee" checked' in r.text and 'value="beide" >' in r.text
    r = klant.post(f"/clara/reistijd/{iid}", headers=FORM, follow_redirects=False,
                   data={"adres": "Manege Hooidonk, Nuenen", "vervoer": "auto", "modus": "beide"})
    assert "In+je+agenda" in r.headers["location"], r.headers["location"]
    assert gemaakt and all(a == "primary" for a, _ in gemaakt) and "brengen" in gemaakt[0][1]
    with db.pool.connection() as c:
        assert "uid:za@g" not in reis.keuzes(c)  # niet meer 'mee'
    # Meegaan met 'automatisch' vervoer blijft ook echt staan.
    gemaakt.clear()
    with db.pool.connection() as c:
        c.execute("DELETE FROM agenda_items WHERE omschrijving LIKE '[clara-reistijd%%'")
    klant.post(f"/clara/reistijd/{iid}", headers=FORM, data={"adres": "Manege Hooidonk, Nuenen", "vervoer": "automatisch", "modus": "mee"})
    with db.pool.connection() as c:
        assert reis.keuzes(c).get("uid:za@g") in ("fiets", "auto")
        c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (kid,))
        for t_ in ("agenda_items", "agenda_reisinfo", "agenda_vervoer", "agenda_reeksen"):
            c.execute(f"DELETE FROM {t_}")
        c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'clara%%'")


def test_meet_of_teams_link_met_adres_is_niet_online():
    basis = {"id": "e", "summary": "Paardrijles", "start": {"dateTime": "2026-10-31T13:30:00+01:00"},
             "end": {"dateTime": "2026-10-31T14:30:00+01:00"}, "hangoutLink": "https://meet.google.com/abc-defg-hij"}
    assert not bronnen.google_item(dict(basis, location="Manege Hooidonk, Beekstraat 48, Nuenen"), "sanne")["online"]
    assert bronnen.google_item(basis, "sanne")["online"]  # alleen een Meet-link: online
    ms = {"id": "m", "subject": "Overleg", "start": {"dateTime": "2026-11-03T10:00:00", "timeZone": "Europe/Amsterdam"},
          "end": {"dateTime": "2026-11-03T11:00:00", "timeZone": "Europe/Amsterdam"}, "isOnlineMeeting": True,
          "responseStatus": {"response": "organizer"}, "isOrganizer": True}
    assert not bronnen.ms_item(dict(ms, location={"displayName": "Huis voor de Stad"}), "cal")["online"]
    assert bronnen.ms_item(dict(ms, location={"displayName": "Microsoft Teams-vergadering"}), "cal")["online"]
    assert bronnen.ms_item(ms, "cal")["online"]
