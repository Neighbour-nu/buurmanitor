"""Agendabronnen van Clara: Google Agenda, Microsoft 365 (Outlook), agendalinks (ICS) en de raadsagenda (Notubiz).

Elke bron levert afspraken in dezelfde vorm (zie `item`), zodat de analyse niet hoeft te weten waar iets vandaan komt.
Google en Microsoft lopen via OAuth met een eigen app (deploy/clara.sh); Clara krijgt daarbij alleen toegang tot de
agenda, niet tot mail. Schrijven (een blok inplannen, een uitnodiging beantwoorden) gebeurt alleen na een klik van
Martijn, nooit vanzelf.
"""
import base64
import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..kern import config

UTC = dt.timezone.utc
ONLINE = re.compile(r"teams\.microsoft|microsoft teams|zoom\.us|\bzoom\b|meet\.google|\bwebex\b|online|https?://", re.I)
AGENT = "Buurmanitor/1.0 (Clara)"


class BronFout(Exception):
    """Ophalen of schrijven mislukt; de melding is in gewone taal."""


class OpnieuwKoppelen(BronFout):
    """De toegang is ingetrokken of verlopen: Martijn moet deze agenda opnieuw koppelen."""


def terugadres(soort, stap="terug"):
    return f"https://{config.PORTAL_HOST}/clara/koppel/{soort}/{stap}"


def item(**kw):
    basis = {"agenda_id": "", "extern_id": "", "ical_uid": "", "titel": "", "locatie": "", "heledag": False,
             "status": "bevestigd", "vrij": False, "antwoord": "eigen", "organisator": "", "online": False, "link": "",
             "geschat": False, "omschrijving": ""}
    basis.update(kw)
    basis["omschrijving"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", basis["omschrijving"] or "")).strip()[:1000]
    basis["titel"] = (basis["titel"] or "").strip()[:300] or "(zonder titel)"
    basis["locatie"] = (basis["locatie"] or "").strip()[:300]
    basis["online"] = bool(basis["online"] or ONLINE.search(basis["locatie"] or ""))
    return basis


def _http(url, data=None, headers=None, methode=None, body=None, timeout=30, soort="de agenda"):
    kop = {"User-Agent": AGENT, **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode()
        kop["Content-Type"] = "application/json"
    elif isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
        kop["Content-Type"] = "application/x-www-form-urlencoded"
    verzoek = urllib.request.Request(url, data=data, headers=kop, method=methode)
    try:
        with urllib.request.urlopen(verzoek, timeout=timeout) as r:
            ruw = r.read()
    except urllib.error.HTTPError as e:
        tekst = e.read()[:2000].decode("utf-8", "replace")
        try:
            j = json.loads(tekst)
        except ValueError:
            j = {}
        fout = j.get("error")
        if isinstance(fout, dict):  # Google- en Graph-API's: {"error": {"code": ..., "message": ...}}
            code, uitleg = str(fout.get("code") or fout.get("status") or ""), str(fout.get("message") or "")
        else:  # OAuth-tokenadres: {"error": "invalid_grant", "error_description": ...}
            code, uitleg = str(fout or ""), str(j.get("error_description") or "")
        if any(t in tekst for t in ("AADSTS65001", "AADSTS90094", "AADSTS90008")):
            raise BronFout(f"Voor {soort} is toestemming van de beheerder van die organisatie nodig.") from e
        if code in ("invalid_grant", "interaction_required", "consent_required"):
            raise OpnieuwKoppelen(f"De toegang tot {soort} is verlopen of ingetrokken. Koppel deze agenda opnieuw.") from e
        if e.code == 401:
            raise OpnieuwKoppelen(f"{soort} accepteert de toegang niet meer. Koppel deze agenda opnieuw.") from e
        raise BronFout(f"{soort} gaf fout {e.code}: {(uitleg or code or tekst)[:200]}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise BronFout(f"{soort} is nu niet bereikbaar ({getattr(e, 'reason', e)}).") from e
    if not ruw:
        return {}
    try:
        return json.loads(ruw)
    except ValueError:
        return {"_ruw": ruw}


def _id_token_gegevens(id_token):
    """De gegevens uit een id_token (alleen lezen; het token komt rechtstreeks van Google of Microsoft over https)."""
    try:
        deel = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(deel + "=" * (-len(deel) % 4)))
    except (IndexError, ValueError):
        return {}


# ---------------------------------------------------------------- Google

GOOGLE_SCOPES = ("openid email https://www.googleapis.com/auth/calendar.readonly "
                 "https://www.googleapis.com/auth/calendar.events")
GOOGLE_API = "https://www.googleapis.com/calendar/v3"


def google_url(staat):
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": terugadres("google"), "response_type": "code",
        "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent select_account", "state": staat,
        "include_granted_scopes": "true"})


def google_code(code):
    j = _http("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": terugadres("google"), "grant_type": "authorization_code"}, soort="Google")
    if not j.get("refresh_token"):
        raise BronFout("Google gaf geen blijvende toegang terug. Probeer het opnieuw; kies dan het account en sta alles toe.")
    return {"refresh": j["refresh_token"], "toegang": j.get("access_token"), "verloopt": j.get("expires_in", 3600),
            "account": _id_token_gegevens(j.get("id_token", "")).get("email", "")}


def google_toegang(refresh):
    j = _http("https://oauth2.googleapis.com/token", data={
        "refresh_token": refresh, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token"}, soort="Google")
    return j["access_token"], j.get("expires_in", 3600), None


def google_agendas(toegang):
    j = _http(f"{GOOGLE_API}/users/me/calendarList?maxResults=250", headers={"Authorization": f"Bearer {toegang}"},
              soort="Google Agenda")
    return [{"id": a["id"], "naam": a.get("summaryOverride") or a.get("summary") or a["id"], "primair": bool(a.get("primary")),
             "schrijven": a.get("accessRole") in ("owner", "writer")} for a in j.get("items", [])
            if not a.get("deleted") and not a.get("hidden")]


def _google_tijd(t, tz):
    if "dateTime" in t:
        return dt.datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00")), False
    d = dt.date.fromisoformat(t["date"])
    return dt.datetime.combine(d, dt.time(0), tzinfo=tz), True


def google_item(e, agenda_id, tz=config.TZ):
    start, heledag = _google_tijd(e["start"], tz)
    eind, _ = _google_tijd(e.get("end") or e["start"], tz)
    ik = next((a for a in e.get("attendees", []) if a.get("self")), None)
    if (e.get("organizer") or {}).get("self") and e.get("attendees"):
        antwoord = "organisator"
    elif ik:
        antwoord = {"accepted": "geaccepteerd", "tentative": "voorlopig", "declined": "afgewezen"}.get(
            ik.get("responseStatus"), "geen")
    else:
        antwoord = "eigen"
    return item(agenda_id=agenda_id, extern_id=e["id"], ical_uid=e.get("iCalUID", ""), titel=e.get("summary", ""),
                locatie=e.get("location", ""), start=start, eind=eind, heledag=heledag,
                status={"tentative": "voorlopig", "cancelled": "geannuleerd"}.get(e.get("status"), "bevestigd"),
                vrij=e.get("transparency") == "transparent", antwoord=antwoord,
                organisator=(e.get("organizer") or {}).get("displayName") or (e.get("organizer") or {}).get("email", ""),
                online=bool(e.get("hangoutLink") or e.get("conferenceData")), link=e.get("htmlLink", ""),
                omschrijving=e.get("description", ""))


def google_items(toegang, agenda_id, van, tot):
    uit, pagina = [], None
    while True:
        params = {"timeMin": van.isoformat(), "timeMax": tot.isoformat(), "singleEvents": "true", "orderBy": "startTime",
                  "maxResults": "2500", "showDeleted": "false"}
        if pagina:
            params["pageToken"] = pagina
        j = _http(f"{GOOGLE_API}/calendars/{urllib.parse.quote(agenda_id, safe='')}/events?" + urllib.parse.urlencode(params),
                  headers={"Authorization": f"Bearer {toegang}"}, soort="Google Agenda")
        for e in j.get("items", []):
            # Werklocatie en focustijd zijn geen afspraken; geannuleerde afspraken tellen niet mee.
            if e.get("eventType") in ("workingLocation",) or e.get("status") == "cancelled" or "start" not in e:
                continue
            uit.append(google_item(e, agenda_id))
        pagina = j.get("nextPageToken")
        if not pagina:
            return uit


def google_antwoord(toegang, agenda_id, extern_id, antwoord):
    """Uitnodiging beantwoorden: geaccepteerd, voorlopig of afgewezen. De organisator krijgt bericht."""
    url = f"{GOOGLE_API}/calendars/{urllib.parse.quote(agenda_id, safe='')}/events/{urllib.parse.quote(extern_id, safe='')}"
    kop = {"Authorization": f"Bearer {toegang}"}
    e = _http(url, headers=kop, soort="Google Agenda")
    deelnemers = e.get("attendees", [])
    ik = next((a for a in deelnemers if a.get("self")), None)
    if not ik:
        raise BronFout("Je staat bij deze afspraak niet als genodigde.")
    ik["responseStatus"] = {"geaccepteerd": "accepted", "voorlopig": "tentative", "afgewezen": "declined"}[antwoord]
    _http(url + "?sendUpdates=all", headers=kop, methode="PATCH", body={"attendees": deelnemers}, soort="Google Agenda")


# De vaste afspraakkleuren van Google Agenda (colorId).
GOOGLE_KLEUREN = {"1": "Lavendel", "2": "Salie", "3": "Druif", "4": "Flamingo", "5": "Banaan", "6": "Mandarijn",
                  "7": "Pauw", "8": "Grafiet", "9": "Bosbes", "10": "Basilicum", "11": "Tomaat"}


def google_maak(toegang, agenda_id, titel, start, eind, omschrijving="", kleur=None):
    body = {"summary": titel, "description": omschrijving,
            "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Amsterdam"},
            "end": {"dateTime": eind.isoformat(), "timeZone": "Europe/Amsterdam"}}
    if kleur in GOOGLE_KLEUREN:
        body["colorId"] = kleur
    j = _http(f"{GOOGLE_API}/calendars/{urllib.parse.quote(agenda_id, safe='')}/events",
              headers={"Authorization": f"Bearer {toegang}"}, soort="Google Agenda", body=body)
    return j.get("id", "")


def google_wijzig(toegang, agenda_id, extern_id, titel=None, kleur=None):
    """Titel en/of kleur van een afspraak aanpassen (alleen voor blokken die Clara zelf heeft gemaakt)."""
    body = {k: v for k, v in (("summary", titel), ("colorId", kleur if kleur in GOOGLE_KLEUREN else None)) if v}
    if body:
        _http(f"{GOOGLE_API}/calendars/{urllib.parse.quote(agenda_id, safe='')}/events/{urllib.parse.quote(extern_id, safe='')}",
              headers={"Authorization": f"Bearer {toegang}"}, methode="PATCH", body=body, soort="Google Agenda")


# ---------------------------------------------------------------- Microsoft 365 (Outlook)

MS_LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"


def ms_scopes(alleen_lezen):
    return "offline_access openid email User.Read " + ("Calendars.Read" if alleen_lezen else "Calendars.ReadWrite")


def ms_url(staat, alleen_lezen):
    return f"{MS_LOGIN}/organizations/oauth2/v2.0/authorize?" + urllib.parse.urlencode({
        "client_id": config.MS_CLIENT_ID, "response_type": "code", "redirect_uri": terugadres("microsoft"),
        "response_mode": "query", "scope": ms_scopes(alleen_lezen), "state": staat, "prompt": "select_account"})


def ms_beheerder_url(tenant="organizations"):
    """Link voor de beheerder van een organisatie om de app eenmalig toestemming te geven (voor iedereen daar)."""
    return f"{MS_LOGIN}/{tenant or 'organizations'}/v2.0/adminconsent?" + urllib.parse.urlencode({
        "client_id": config.MS_CLIENT_ID, "scope": "https://graph.microsoft.com/.default",
        "redirect_uri": terugadres("microsoft", "beheerder"), "state": "beheerder"})


def ms_code(code, alleen_lezen):
    j = _http(f"{MS_LOGIN}/organizations/oauth2/v2.0/token", data={
        "client_id": config.MS_CLIENT_ID, "client_secret": config.MS_CLIENT_SECRET, "code": code,
        "redirect_uri": terugadres("microsoft"), "grant_type": "authorization_code", "scope": ms_scopes(alleen_lezen)},
        soort="Microsoft")
    if not j.get("refresh_token"):
        raise BronFout("Microsoft gaf geen blijvende toegang terug. Probeer het opnieuw.")
    gegevens = _id_token_gegevens(j.get("id_token", ""))
    return {"refresh": j["refresh_token"], "toegang": j.get("access_token"), "verloopt": j.get("expires_in", 3600),
            "account": gegevens.get("email") or gegevens.get("preferred_username", ""), "tenant": gegevens.get("tid", "")}


def ms_toegang(refresh, tenant, alleen_lezen):
    """Nieuw toegangstoken. Microsoft geeft daarbij vaak ook een nieuw refresh-token; dat moet bewaard worden."""
    j = _http(f"{MS_LOGIN}/{tenant or 'organizations'}/oauth2/v2.0/token", data={
        "client_id": config.MS_CLIENT_ID, "client_secret": config.MS_CLIENT_SECRET, "refresh_token": refresh,
        "grant_type": "refresh_token", "scope": ms_scopes(alleen_lezen)}, soort="Microsoft")
    return j["access_token"], j.get("expires_in", 3600), j.get("refresh_token")


def ms_agendas(toegang):
    j = _http(f"{GRAPH}/me/calendars?$top=100&$select=id,name,isDefaultCalendar,canEdit",
              headers={"Authorization": f"Bearer {toegang}"}, soort="Outlook")
    return [{"id": a["id"], "naam": a.get("name") or "Agenda", "primair": bool(a.get("isDefaultCalendar")),
             "schrijven": bool(a.get("canEdit"))} for a in j.get("value", [])]


def _ms_tijd(t, tz):
    tekst = (t or {}).get("dateTime", "")[:19]
    return dt.datetime.fromisoformat(tekst).replace(tzinfo=tz)


def ms_item(e, agenda_id, tz=config.TZ):
    reactie = (e.get("responseStatus") or {}).get("response", "none")
    if e.get("isOrganizer"):
        antwoord = "organisator" if reactie == "organizer" and e.get("attendees") else "eigen"
    else:
        antwoord = {"accepted": "geaccepteerd", "tentativelyAccepted": "voorlopig", "declined": "afgewezen"}.get(reactie, "geen")
    locatie = (e.get("location") or {}).get("displayName", "")
    org = ((e.get("organizer") or {}).get("emailAddress") or {})
    return item(agenda_id=agenda_id, extern_id=e["id"], ical_uid=e.get("iCalUId", ""), titel=e.get("subject", ""),
                locatie=locatie, start=_ms_tijd(e.get("start"), tz), eind=_ms_tijd(e.get("end"), tz),
                heledag=bool(e.get("isAllDay")), status="geannuleerd" if e.get("isCancelled") else (
                    "voorlopig" if e.get("showAs") == "tentative" else "bevestigd"),
                vrij=e.get("showAs") in ("free", "workingElsewhere"), antwoord=antwoord,
                organisator=org.get("name") or org.get("address", ""), online=bool(e.get("isOnlineMeeting")),
                link=e.get("webLink", ""), omschrijving=e.get("bodyPreview", ""))


def ms_items(toegang, agenda_id, van, tot):
    velden = ("id,iCalUId,subject,location,start,end,isAllDay,showAs,isCancelled,responseStatus,organizer,"
              "isOrganizer,isOnlineMeeting,webLink,attendees,bodyPreview")
    url = (f"{GRAPH}/me/calendars/{urllib.parse.quote(agenda_id, safe='')}/calendarView?" + urllib.parse.urlencode({
        "startDateTime": van.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": tot.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "$top": "250", "$select": velden}))
    kop = {"Authorization": f"Bearer {toegang}", "Prefer": 'outlook.timezone="Europe/Amsterdam"'}
    uit = []
    while url:
        j = _http(url, headers=kop, soort="Outlook")
        uit += [ms_item(e, agenda_id) for e in j.get("value", []) if not e.get("isCancelled")]
        url = j.get("@odata.nextLink")
    return uit


def ms_antwoord(toegang, agenda_id, extern_id, antwoord):
    actie = {"geaccepteerd": "accept", "voorlopig": "tentativelyAccept", "afgewezen": "decline"}[antwoord]
    _http(f"{GRAPH}/me/events/{urllib.parse.quote(extern_id, safe='')}/{actie}", headers={"Authorization": f"Bearer {toegang}"},
          methode="POST", body={"sendResponse": True}, soort="Outlook")


def ms_maak(toegang, agenda_id, titel, start, eind, omschrijving="", kleur=None):  # noqa: ARG001 (Outlook: geen vaste kleuren per afspraak)
    j = _http(f"{GRAPH}/me/calendars/{urllib.parse.quote(agenda_id, safe='')}/events",
              headers={"Authorization": f"Bearer {toegang}"}, soort="Outlook", body={
                  "subject": titel, "body": {"contentType": "text", "content": omschrijving}, "showAs": "busy",
                  "start": {"dateTime": start.astimezone(config.TZ).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Europe/Amsterdam"},
                  "end": {"dateTime": eind.astimezone(config.TZ).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Europe/Amsterdam"}})
    return j.get("id", "")


# ---------------------------------------------------------------- Agendalink (ICS), alleen lezen

def ics_tekst(url):
    url = re.sub(r"^webcals?://", "https://", url.strip(), flags=re.I)
    if not url.lower().startswith("https://"):
        raise BronFout("Een agendalink moet met https:// of webcal:// beginnen.")
    j = _http(url, headers={"Accept": "text/calendar, */*"}, timeout=40, soort="de agendalink")
    ruw = j.get("_ruw", b"") if isinstance(j, dict) else b""
    if b"BEGIN:VCALENDAR" not in ruw[:2000]:
        raise BronFout("Deze link geeft geen agenda (ICS) terug. Controleer of je de ICS-link hebt gekopieerd.")
    return ruw


def ics_items(ruw, van, tot, tz=config.TZ):
    import icalendar  # pylint: disable=import-outside-toplevel
    import recurring_ical_events  # pylint: disable=import-outside-toplevel
    kal = icalendar.Calendar.from_ical(ruw)
    uit = []

    def tijd(w):
        if isinstance(w, dt.datetime):
            return (w if w.tzinfo else w.replace(tzinfo=tz)), False
        return dt.datetime.combine(w, dt.time(0), tzinfo=tz), True

    for e in recurring_ical_events.of(kal).between(van, tot):
        if e.get("DTSTART") is None:
            continue
        start, heledag = tijd(e.decoded("DTSTART"))
        if e.get("DTEND") is not None:
            eind, _ = tijd(e.decoded("DTEND"))
        elif e.get("DURATION") is not None:
            eind = start + e.decoded("DURATION")
        else:
            eind = start + (dt.timedelta(days=1) if heledag else dt.timedelta(0))
        status = str(e.get("STATUS", "")).upper()
        if status == "CANCELLED":
            continue
        bezet = str(e.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper()
        uid = str(e.get("UID", ""))
        uit.append(item(extern_id=f"{uid}|{start.isoformat()}", ical_uid=uid, titel=str(e.get("SUMMARY", "")) or "Bezet",
                        locatie=str(e.get("LOCATION", "")), start=start, eind=eind, heledag=heledag,
                        status="voorlopig" if status == "TENTATIVE" or bezet == "TENTATIVE" else "bevestigd",
                        vrij=str(e.get("TRANSP", "")).upper() == "TRANSPARENT" or bezet == "FREE",
                        link=str(e.get("URL", "")), omschrijving=str(e.get("DESCRIPTION", ""))))
    return uit


# ---------------------------------------------------------------- Raadsagenda Helmond (Notubiz, openbaar)

DUUR_RAAD = {"commissie": dt.timedelta(hours=3), "raad": dt.timedelta(hours=4), "overig": dt.timedelta(hours=2)}


def raad_items(van, tot, tz=config.TZ):
    """Openbare vergaderingen van de gemeenteraad. Notubiz geeft alleen een begintijd; het einde is een schatting."""
    from ..politiek import notubiz  # pylint: disable=import-outside-toplevel
    try:
        lijst = notubiz.evenementen(van.date(), tot.date())
    except Exception as e:  # noqa: BLE001
        raise BronFout(f"Notubiz is nu niet bereikbaar ({e}).") from e
    uit = []
    for ev in lijst:
        if not ev.get("start"):
            continue
        start = ev["start"].replace(tzinfo=tz)
        heledag = start.time() == dt.time(0)
        uit.append(item(extern_id=str(ev["id"]), titel=ev.get("naam") or "Vergadering", locatie=ev.get("plek", ""),
                        start=start, eind=start + (dt.timedelta(days=1) if heledag else DUUR_RAAD.get(ev.get("soort"), DUUR_RAAD["overig"])),
                        heledag=heledag, link=ev.get("url", ""), geschat=not heledag))
    return uit
