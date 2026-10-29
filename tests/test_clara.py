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
    return bronnen.item(titel=titel, start=start, eind=eind, extern_id=titel, koppeling=kw.pop("koppeling", "A"),
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
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="": gemaakt.append((a, titel, s, e)) or "nieuw1")
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
    monkeypatch.setattr(bronnen, "google_maak", lambda tok, a, titel, s, e, o="": gemaakt.append(titel) or "v1")
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
