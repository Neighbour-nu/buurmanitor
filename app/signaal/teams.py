"""Microsoft Teams: de chats (één op één, groepen en vergaderchats) van je werkaccount, alleen lezen.

- Koppelen met je Microsoft-account (dezelfde app als voor de agenda en de mail), met alleen de rechten Chat.Read en
  User.Read. Buurmanitor kan dus niets sturen. Het geheim staat versleuteld in de kluis.
- Elke vijf minuten haalt de planner de chats op die sinds de vorige keer iets nieuws hebben, en daarvan de nieuwe
  berichten. Verder werkt het als Signal: per chat kies je meelezen, archief, voorstellen en domein.
- Kanalen in een team (niet de chats) vragen toestemming van de beheerder (ChannelMessage.Read.All); die doen we niet.
"""
import datetime as dt
import html
import re
import time
import urllib.parse

from ..clara import bronnen, kluis
from ..kern import config, planner
from . import verwerken

SCOPES = "offline_access openid email User.Read Chat.Read"
GRAPH = "https://graph.microsoft.com/v1.0"
SOORT = "Teams"
_token = {}


class Fout(bronnen.BronFout):
    pass


# ---------------------------------------------------------------- koppelen

def koppel_url(staat):
    return f"{bronnen.MS_LOGIN}/organizations/oauth2/v2.0/authorize?" + urllib.parse.urlencode({
        "client_id": config.MS_CLIENT_ID, "response_type": "code", "redirect_uri": bronnen.terugadres("microsoft"),
        "response_mode": "query", "scope": SCOPES, "state": staat, "prompt": "select_account"})


def koppel_code(code):
    j = bronnen._http(f"{bronnen.MS_LOGIN}/organizations/oauth2/v2.0/token", data={  # pylint: disable=protected-access
        "client_id": config.MS_CLIENT_ID, "client_secret": config.MS_CLIENT_SECRET, "code": code,
        "redirect_uri": bronnen.terugadres("microsoft"), "grant_type": "authorization_code", "scope": SCOPES}, soort="Microsoft")
    if not j.get("refresh_token"):
        raise Fout("Microsoft gaf geen blijvende toegang terug. Probeer het opnieuw.")
    g = bronnen._id_token_gegevens(j.get("id_token", ""))  # pylint: disable=protected-access
    return {"refresh": j["refresh_token"], "account": g.get("email") or g.get("preferred_username", ""), "tenant": g.get("tid", "")}


def bewaar(c, gegevens):
    verwerken.zet(c, "teams_geheim", kluis.sluit(gegevens["refresh"]))
    verwerken.zet(c, "teams_tenant", gegevens.get("tenant", ""))
    verwerken.zet(c, "teams_account", gegevens.get("account", ""))
    verwerken.zet(c, "teams_ik", "")
    _token.clear()


def gekoppeld(c):
    return verwerken.instelling(c, "teams_account", "") or ("ja" if verwerken.instelling(c, "teams_geheim", "") else "")


def ontkoppel(c):
    c.execute("DELETE FROM instellingen WHERE sleutel LIKE 'teams\\_%%'")
    _token.clear()


def toegang(c):
    if _token.get("t") and _token.get("tot", 0) > time.monotonic():
        return _token["t"]
    geheim = verwerken.instelling(c, "teams_geheim", "")
    if not geheim:
        raise Fout("Teams is niet gekoppeld.")
    refresh = kluis.open_(geheim)
    tenant = verwerken.instelling(c, "teams_tenant", "") or "organizations"
    j = bronnen._http(f"{bronnen.MS_LOGIN}/{tenant}/oauth2/v2.0/token", data={  # pylint: disable=protected-access
        "client_id": config.MS_CLIENT_ID, "client_secret": config.MS_CLIENT_SECRET, "refresh_token": refresh,
        "grant_type": "refresh_token", "scope": SCOPES}, soort=SOORT)
    if j.get("refresh_token") and j["refresh_token"] != refresh:
        verwerken.zet(c, "teams_geheim", kluis.sluit(j["refresh_token"]))
    _token.update(t=j["access_token"], tot=time.monotonic() + int(j.get("expires_in", 3600)) - 120)
    return _token["t"]


def _vraag(c, pad):
    try:
        return bronnen._http(pad if pad.startswith("http") else GRAPH + pad,  # pylint: disable=protected-access
                             headers={"Authorization": f"Bearer {toegang(c)}"}, soort=SOORT)
    except bronnen.BronFout as e:
        if "AADSTS65001" in str(e) or "Forbidden" in str(e) or "403" in str(e):
            raise Fout("Teams geeft geen toegang tot je chats. Mogelijk moet de beheerder van je organisatie de app eerst "
                       f"toestaan (Chat.Read). ({str(e)[:200]})") from e
        raise


# ---------------------------------------------------------------- ophalen

def platte_tekst(inhoud, soort="html"):
    """De tekst van een Teams-bericht (dat meestal HTML is). Pure functie."""
    t = inhoud or ""
    if (soort or "").lower() == "html":
        t = re.sub(r"<attachment[^>]*>.*?</attachment>", "[bijlage]", t, flags=re.S | re.I)
        t = re.sub(r"<(br|/p|/div|/li)\s*/?>", "\n", t, flags=re.I)
        t = re.sub(r"<[^>]+>", "", t)
        t = html.unescape(t)
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def chatnaam(ch, ik=""):
    """De naam van een chat: het onderwerp, of anders de namen van de anderen. Pure functie."""
    if ch.get("topic"):
        return ch["topic"][:120]
    anderen = [m.get("displayName") or "" for m in ch.get("members") or [] if m.get("userId") != ik and m.get("displayName")]
    return (", ".join(anderen[:4]) + (f" en {len(anderen) - 4} anderen" if len(anderen) > 4 else ""))[:120] or "Teams-chat"


def _utc(tekst):
    """'2026-11-19T14:26:01.1234567Z' (Graph) als datetime; op de seconde nauwkeurig."""
    return dt.datetime.fromisoformat(tekst[:19]).replace(tzinfo=dt.timezone.utc)


def naar_bericht(m, ch, naam, ik=""):
    """Een Teams-bericht in de vorm van verwerken.opslaan, of None (systeemmelding, verwijderd, leeg). Pure functie."""
    if m.get("messageType") != "message" or m.get("deletedDateTime"):
        return None
    body = m.get("body") or {}
    tekst = platte_tekst(body.get("content"), body.get("contentType"))
    if m.get("attachments") and "[bijlage]" not in tekst:
        tekst = (tekst + " [bijlage]").strip()
    if not tekst:
        return None
    afz = ((m.get("from") or {}).get("user") or {})
    mij = bool(ik) and afz.get("id") == ik
    try:
        tijd = _utc(m["createdDateTime"])
    except (KeyError, ValueError):
        return None
    ts = int(m["id"]) if str(m.get("id", "")).isdigit() else int(tijd.timestamp() * 1000)
    soort = "persoon" if ch.get("chatType") == "oneOnOne" else "groep"
    return {"sleutel": f"teams:{ch['id']}", "soort": soort, "chatnaam": naam, "bron": "teams", "ts": ts,
            "van": "jij" if mij else (afz.get("displayName") or "onbekend")[:80], "van_mij": mij, "tekst": tekst[:8000], "tijd": tijd}


def ophalen(c, nu=None):
    """Nieuwe berichten uit de chats die sinds de vorige keer veranderd zijn. Geeft (berichten, bewaard, nieuwe chats)."""
    nu = nu or dt.datetime.now(dt.timezone.utc)
    ik = verwerken.instelling(c, "teams_ik", "")
    if not ik:
        ik = _vraag(c, "/me?$select=id")["id"]
        verwerken.zet(c, "teams_ik", ik)
    sinds_tekst = verwerken.instelling(c, "teams_sinds", "")
    sinds = dt.datetime.fromisoformat(sinds_tekst) if sinds_tekst else nu - dt.timedelta(days=1)
    url = "/me/chats?" + urllib.parse.urlencode({"$expand": "members", "$top": 50,
                                                  "$orderby": "lastMessagePreview/createdDateTime desc"})
    berichten = []
    for _ in range(4):  # hooguit 200 chats per keer
        j = _vraag(c, url)
        klaar = False
        for ch in j.get("value") or []:
            laatste = ((ch.get("lastMessagePreview") or {}).get("createdDateTime") or ch.get("lastUpdatedDateTime") or "")
            if laatste and _utc(laatste) < sinds:
                klaar = True
                break
            naam = chatnaam(ch, ik)
            mj = _vraag(c, f"/chats/{urllib.parse.quote(ch['id'])}/messages?" + urllib.parse.urlencode({
                "$top": 50, "$orderby": "createdDateTime desc",
                "$filter": f"createdDateTime gt {sinds.astimezone(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}"}))
            berichten += [b for b in (naar_bericht(m, ch, naam, ik) for m in reversed(mj.get("value") or [])) if b]
        url = j.get("@odata.nextLink") or ""
        if klaar or not url:
            break
    nieuw, bewaard = verwerken.opslaan(c, berichten)
    verwerken.zet(c, "teams_sinds", (nu - dt.timedelta(minutes=2)).isoformat())
    return len(berichten), bewaard, nieuw


@planner.taak("teams", elke=dt.timedelta(minutes=5))
def taak_teams(c):
    if not verwerken.instelling(c, "teams_geheim", ""):
        return "Teams is niet gekoppeld"
    n, bewaard, nieuw = ophalen(c)
    return f"{n} berichten, {bewaard} bewaard" + (f", {nieuw} nieuwe chats" if nieuw else "")
