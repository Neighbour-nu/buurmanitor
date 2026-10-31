"""Bram leest mail: Gmail (Google Workspace) en Outlook (Microsoft 365).

Bram gebruikt dezelfde apps bij Google en Microsoft als Clara, met hetzelfde terugkeeradres, maar vraagt een eigen
toestemming voor mail en bewaart een eigen token per account. Clara krijgt dus nooit toegang tot je mail.

- Gmail: gmail.modify (lezen, labels zetten, gelezen markeren en archiveren) en gmail.send (een antwoord versturen,
  alleen na een klik van Martijn). Niet definitief verwijderen.
- Outlook: Mail.ReadWrite (lezen, categorie, gelezen, verplaatsen naar Archief), Mail.Send (antwoord versturen, na een
  klik) en MailboxSettings.Read (de lijst categorieën).
"""
import base64
import datetime as dt
import email.utils
import html
import re
import urllib.error
import urllib.parse
import urllib.request

from ..clara.bronnen import (MS_LOGIN, BronFout, NietGevonden, OpnieuwKoppelen, _http,  # noqa: F401
                             _id_token_gegevens, terugadres)
from ..kern import config

UTC = dt.timezone.utc
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0"
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send"
MS_SCOPES = "offline_access openid email User.Read Mail.ReadWrite Mail.Send MailboxSettings.Read"
TEKST_MAX = 4000  # zoveel tekst van een mail gaat (alleen als het mag) naar Claude; hij wordt niet bewaard
DAGEN_TERUG = 30
MAX_BERICHTEN = 100


# ---------------------------------------------------------------- koppelen

def google_url(staat):
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": terugadres("google"), "response_type": "code",
        "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent select_account", "state": staat})


def google_code(code):
    j = _http("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": terugadres("google"), "grant_type": "authorization_code"}, soort="Google")
    if not j.get("refresh_token"):
        raise BronFout("Google gaf geen blijvende toegang terug. Probeer het opnieuw; kies dan het account en sta alles toe.")
    if "gmail.modify" not in (j.get("scope") or "gmail.modify"):
        raise BronFout("Google gaf geen toegang tot Gmail. Koppel opnieuw en vink de toegang tot Gmail aan.")
    return {"refresh": j["refresh_token"], "account": _id_token_gegevens(j.get("id_token", "")).get("email", "")}


def google_toegang(refresh):
    j = _http("https://oauth2.googleapis.com/token", data={
        "refresh_token": refresh, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token"}, soort="Gmail")
    return j["access_token"], j.get("expires_in", 3600), None


def ms_url(staat):
    return f"{MS_LOGIN}/organizations/oauth2/v2.0/authorize?" + urllib.parse.urlencode({
        "client_id": config.MS_CLIENT_ID, "response_type": "code", "redirect_uri": terugadres("microsoft"),
        "response_mode": "query", "scope": MS_SCOPES, "state": staat, "prompt": "select_account"})


def ms_code(code):
    j = _http(f"{MS_LOGIN}/organizations/oauth2/v2.0/token", data={
        "client_id": config.MS_CLIENT_ID, "client_secret": config.MS_CLIENT_SECRET, "code": code,
        "redirect_uri": terugadres("microsoft"), "grant_type": "authorization_code", "scope": MS_SCOPES}, soort="Microsoft")
    if not j.get("refresh_token"):
        raise BronFout("Microsoft gaf geen blijvende toegang terug. Probeer het opnieuw.")
    g = _id_token_gegevens(j.get("id_token", ""))
    return {"refresh": j["refresh_token"], "account": g.get("email") or g.get("preferred_username", ""), "tenant": g.get("tid", "")}


def ms_toegang(refresh, tenant):
    j = _http(f"{MS_LOGIN}/{tenant or 'organizations'}/oauth2/v2.0/token", data={
        "client_id": config.MS_CLIENT_ID, "client_secret": config.MS_CLIENT_SECRET, "refresh_token": refresh,
        "grant_type": "refresh_token", "scope": MS_SCOPES}, soort="Outlook")
    return j["access_token"], j.get("expires_in", 3600), j.get("refresh_token")


# ---------------------------------------------------------------- hulpjes

def bericht(**kw):
    """Eén bericht in de vorm die Bram bewaart (zonder de tekst zelf)."""
    b = {"extern_id": "", "draad": "", "van_naam": "", "van_adres": "", "aan": "", "onderwerp": "", "fragment": "",
         "ontvangen": None, "gelezen": False, "labels": [], "link": "", "nieuwsbrief": False, "bron_categorie": "",
         "tekst": ""}
    b.update(kw)
    b["onderwerp"] = (b["onderwerp"] or "").strip()[:300] or "(geen onderwerp)"
    b["fragment"] = re.sub(r"\s+", " ", html.unescape(b["fragment"] or "")).strip()[:300]
    b["van_adres"] = (b["van_adres"] or "").strip().lower()[:200]
    b["van_naam"] = (b["van_naam"] or "").strip().strip('"')[:120] or b["van_adres"]
    b["tekst"] = (b["tekst"] or "")[:TEKST_MAX]
    return b


def html_naar_tekst(h):
    h = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


def _b64(data):
    return base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4)).decode("utf-8", "replace")


# ---------------------------------------------------------------- Gmail

def _gmail(toegang, pad, **kw):
    return _http(f"{GMAIL}{pad}", headers={"Authorization": f"Bearer {toegang}"}, soort="Gmail", **kw)


def gmail_ids(toegang, extra=""):
    """De id's van berichten in de Inbox van de laatste weken (nieuwste eerst)."""
    q = f"in:inbox newer_than:{DAGEN_TERUG}d {extra}".strip()
    ids, pagina = [], None
    while len(ids) < MAX_BERICHTEN:
        param = {"q": q, "maxResults": min(100, MAX_BERICHTEN - len(ids))} | ({"pageToken": pagina} if pagina else {})
        j = _gmail(toegang, "/messages?" + urllib.parse.urlencode(param))
        ids += [m["id"] for m in j.get("messages", [])]
        pagina = j.get("nextPageToken")
        if not pagina:
            break
    return ids


def _gmail_tekst(deel):
    """De leesbare tekst uit een Gmail-bericht: text/plain als die er is, anders text/html zonder opmaak."""
    platte, opgemaakt = [], []

    def loop(d):
        soort = d.get("mimeType", "")
        data = (d.get("body") or {}).get("data")
        if data and soort == "text/plain":
            platte.append(_b64(data))
        elif data and soort == "text/html":
            opgemaakt.append(html_naar_tekst(_b64(data)))
        for p in d.get("parts") or []:
            loop(p)
    loop(deel or {})
    return "\n".join(platte) if platte else "\n".join(opgemaakt)


def gmail_bericht(toegang, mid, account, met_tekst=False):
    if met_tekst:
        m = _gmail(toegang, f"/messages/{mid}?format=full")
    else:
        kop = "&".join(f"metadataHeaders={h}" for h in ("From", "To", "Cc", "Subject", "Date", "List-Unsubscribe"))
        m = _gmail(toegang, f"/messages/{mid}?format=metadata&{kop}")
    koppen = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}
    naam, adres = email.utils.parseaddr(koppen.get("from", ""))
    labels = m.get("labelIds") or []
    try:
        ontvangen = dt.datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000, UTC)
    except ValueError:
        ontvangen = dt.datetime.now(UTC)
    cat = next((x.removeprefix("CATEGORY_").lower() for x in labels if x.startswith("CATEGORY_")), "")
    return bericht(extern_id=m["id"], draad=m.get("threadId", ""), van_naam=naam, van_adres=adres,
                   aan=", ".join(filter(None, [koppen.get("to", ""), koppen.get("cc", "")]))[:500],
                   onderwerp=koppen.get("subject", ""), fragment=m.get("snippet", ""), ontvangen=ontvangen,
                   gelezen="UNREAD" not in labels, labels=[x for x in labels if not x.startswith("CATEGORY_")],
                   link=f"https://mail.google.com/mail/u/?authuser={urllib.parse.quote(account)}#all/{m['id']}",
                   nieuwsbrief=bool(koppen.get("list-unsubscribe")) or cat in ("promotions", "social"),
                   bron_categorie=cat, tekst=_gmail_tekst(m.get("payload")) if met_tekst else "")


def gmail_labels(toegang):
    """Je eigen labels (geen systeemlabels zoals INBOX), als (id, naam)."""
    j = _gmail(toegang, "/labels")
    return sorted(((x["id"], x["name"]) for x in j.get("labels", []) if x.get("type") == "user"), key=lambda x: x[1].lower())


def gmail_wijzig(toegang, mid, erbij=(), eraf=()):
    _gmail(toegang, f"/messages/{mid}/modify", body={"addLabelIds": list(erbij), "removeLabelIds": list(eraf)})


def gmail_archiveer(toegang, mid):
    gmail_wijzig(toegang, mid, eraf=["INBOX"])


def gmail_gelezen(toegang, mid):
    gmail_wijzig(toegang, mid, eraf=["UNREAD"])


def gmail_label(toegang, mid, label_id):
    gmail_wijzig(toegang, mid, erbij=[label_id])


# ---------------------------------------------------------------- Outlook

def _graph(toegang, pad, koppen=None, **kw):
    return _http(f"{GRAPH}{pad}", headers={"Authorization": f"Bearer {toegang}", **(koppen or {})}, soort="Outlook", **kw)


VELDEN = ("id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,isRead,webLink,"
          "categories,inferenceClassification")


def ms_berichten(toegang, met_tekst=False, nu=None):
    """De berichten in de Postvak IN van de laatste weken (nieuwste eerst)."""
    nu = nu or dt.datetime.now(UTC)
    van = (nu - dt.timedelta(days=DAGEN_TERUG)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    param = {"$top": "50", "$orderby": "receivedDateTime desc", "$filter": f"receivedDateTime ge {van}",
             "$select": VELDEN + (",body" if met_tekst else "")}
    koppen = {"Prefer": 'outlook.body-content-type="text"'} if met_tekst else None
    url, uit = "/me/mailFolders/inbox/messages?" + urllib.parse.urlencode(param), []
    while url and len(uit) < MAX_BERICHTEN:
        j = _graph(toegang, url, koppen)
        uit += [ms_bericht(m) for m in j.get("value", [])]
        volgende = j.get("@odata.nextLink") or ""
        url = volgende.removeprefix(GRAPH) if volgende else None
    return uit[:MAX_BERICHTEN]


def ms_bericht(m):
    van = (m.get("from") or {}).get("emailAddress") or {}
    ontvangers = [(r.get("emailAddress") or {}).get("address", "") for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
    try:
        ontvangen = dt.datetime.fromisoformat((m.get("receivedDateTime") or "").replace("Z", "+00:00"))
    except ValueError:
        ontvangen = dt.datetime.now(UTC)
    adres = van.get("address", "")
    return bericht(extern_id=m["id"], draad=m.get("conversationId", ""), van_naam=van.get("name", ""), van_adres=adres,
                   aan=", ".join(filter(None, ontvangers))[:500], onderwerp=m.get("subject", ""),
                   fragment=m.get("bodyPreview", ""), ontvangen=ontvangen, gelezen=bool(m.get("isRead")),
                   labels=m.get("categories") or [], link=m.get("webLink", ""),
                   nieuwsbrief=m.get("inferenceClassification") == "other",
                   bron_categorie=m.get("inferenceClassification", ""),
                   tekst=((m.get("body") or {}).get("content") or "") if "body" in m else "")


def ms_tekst(toegang, mid):
    j = _graph(toegang, f"/me/messages/{urllib.parse.quote(mid)}?$select=body", {"Prefer": 'outlook.body-content-type="text"'})
    return ((j.get("body") or {}).get("content") or "")[:TEKST_MAX]


def gmail_tekst(toegang, mid):
    return _gmail_tekst(_gmail(toegang, f"/messages/{mid}?format=full").get("payload"))[:TEKST_MAX]


def ms_categorieen(toegang):
    j = _graph(toegang, "/me/outlook/masterCategories")
    return sorted(((x["displayName"], x["displayName"]) for x in j.get("value", [])), key=lambda x: x[1].lower())


def ms_archiveer(toegang, mid):
    """Naar de map Archief. Outlook geeft het bericht daarbij een nieuw id."""
    return _graph(toegang, f"/me/messages/{mid}/move", body={"destinationId": "archive"}, methode="POST").get("id", "")


def ms_gelezen(toegang, mid):
    _graph(toegang, f"/me/messages/{mid}", body={"isRead": True}, methode="PATCH")


def ms_label(toegang, mid, categorie, huidige=()):
    _graph(toegang, f"/me/messages/{mid}", body={"categories": sorted(set(huidige) | {categorie})}, methode="PATCH")


# ---------------------------------------------------------------- één bericht helemaal lezen, en antwoorden

BIJLAGE_MAX = 3_000_000  # grotere bijlagen leest Bram niet
BIJLAGE_TEKST = 6000


def bijlage_tekst(naam, soort, data):
    """De leesbare tekst van een bijlage: pdf, agenda-uitnodiging (ics), tekst of html. Anders leeg."""
    naam_l, soort = (naam or "").lower(), (soort or "").lower()
    try:
        if soort == "application/pdf" or naam_l.endswith(".pdf"):
            import io  # pylint: disable=import-outside-toplevel
            from pypdf import PdfReader  # pylint: disable=import-outside-toplevel
            lezer = PdfReader(io.BytesIO(data))
            tekst = "\n".join((p.extract_text() or "") for p in lezer.pages[:10])
        elif soort in ("text/calendar", "application/ics") or naam_l.endswith(".ics"):
            tekst = data.decode("utf-8", "replace")
            tekst = "\n".join(r for r in tekst.splitlines() if r.split(":", 1)[0].split(";", 1)[0] in (
                "SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION", "ORGANIZER", "TZID"))
        elif soort == "text/html" or naam_l.endswith((".html", ".htm")):
            tekst = html_naar_tekst(data.decode("utf-8", "replace"))
        elif soort.startswith("text/") or naam_l.endswith((".txt", ".csv")):
            tekst = data.decode("utf-8", "replace")
        else:
            return ""
    except Exception:  # noqa: BLE001  (een kapotte bijlage mag het lezen van de mail niet tegenhouden)
        return ""
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", tekst)).strip()[:BIJLAGE_TEKST]


def _gmail_bijlagen(deel):
    uit = []

    def loop(d):
        body = d.get("body") or {}
        if d.get("filename") and body.get("attachmentId"):
            uit.append({"naam": d["filename"], "soort": d.get("mimeType", ""), "id": body["attachmentId"],
                        "grootte": int(body.get("size") or 0)})
        for p in d.get("parts") or []:
            loop(p)
    loop(deel or {})
    return uit


def gmail_volledig(toegang, mid, met_bijlagen=True):
    """De hele mail: tekst, de koppen om te antwoorden en de bijlagen (met hun tekst als die te lezen is)."""
    m = _gmail(toegang, f"/messages/{mid}?format=full")
    koppen = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}
    bijlagen = _gmail_bijlagen(m.get("payload"))
    for b in bijlagen:
        b["tekst"] = ""
        if met_bijlagen and b["grootte"] <= BIJLAGE_MAX:
            j = _gmail(toegang, f"/messages/{mid}/attachments/{b['id']}")
            b["tekst"] = bijlage_tekst(b["naam"], b["soort"], base64.urlsafe_b64decode((j.get("data") or "") + "=" * (-len(j.get("data") or "") % 4)))
    return {"tekst": _gmail_tekst(m.get("payload"))[:12000], "bijlagen": bijlagen, "draad": m.get("threadId", ""),
            "van": koppen.get("from", ""), "antwoord_aan": koppen.get("reply-to") or koppen.get("from", ""),
            "aan": koppen.get("to", ""), "cc": koppen.get("cc", ""), "onderwerp": koppen.get("subject", ""),
            "message_id": koppen.get("message-id", ""), "references": koppen.get("references", "")}


def gmail_antwoord(toegang, account, origineel, tekst):
    """Antwoord op een Gmail-bericht, in dezelfde draad, aan de afzender (of het Reply-To-adres)."""
    from email.message import EmailMessage  # pylint: disable=import-outside-toplevel
    m = EmailMessage()
    m["From"] = account
    m["To"] = origineel["antwoord_aan"]
    onderwerp = origineel["onderwerp"] or ""
    m["Subject"] = onderwerp if onderwerp.lower().startswith(("re:", "antw:")) else f"Re: {onderwerp}"
    if origineel.get("message_id"):
        m["In-Reply-To"] = origineel["message_id"]
        m["References"] = (origineel.get("references", "") + " " + origineel["message_id"]).strip()
    m.set_content(tekst)
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
    return _gmail(toegang, "/messages/send", body={"raw": raw, "threadId": origineel.get("draad", "")}).get("id", "")


def ms_volledig(toegang, mid, met_bijlagen=True):
    q = urllib.parse.quote(mid)
    m = _graph(toegang, f"/me/messages/{q}?$select=subject,from,replyTo,toRecipients,ccRecipients,body,hasAttachments,conversationId",
               {"Prefer": 'outlook.body-content-type="text"'})
    adres = lambda x: f"{(x.get('emailAddress') or {}).get('name', '')} <{(x.get('emailAddress') or {}).get('address', '')}>"  # noqa: E731
    bijlagen = []
    if m.get("hasAttachments"):
        for a in _graph(toegang, f"/me/messages/{q}/attachments?$select=id,name,contentType,size").get("value", []):
            b = {"naam": a.get("name", ""), "soort": a.get("contentType", ""), "id": a.get("id", ""), "grootte": int(a.get("size") or 0), "tekst": ""}
            if met_bijlagen and b["grootte"] <= BIJLAGE_MAX and "fileAttachment" in a.get("@odata.type", "fileAttachment"):
                j = _graph(toegang, f"/me/messages/{q}/attachments/{urllib.parse.quote(b['id'])}")
                if j.get("contentBytes"):
                    b["tekst"] = bijlage_tekst(b["naam"], b["soort"], base64.b64decode(j["contentBytes"]))
            bijlagen.append(b)
    van = adres(m.get("from") or {})
    return {"tekst": ((m.get("body") or {}).get("content") or "")[:12000], "bijlagen": bijlagen, "draad": m.get("conversationId", ""),
            "van": van, "antwoord_aan": ", ".join(adres(x) for x in m.get("replyTo") or []) or van,
            "aan": ", ".join(adres(x) for x in m.get("toRecipients") or []), "cc": ", ".join(adres(x) for x in m.get("ccRecipients") or []),
            "onderwerp": m.get("subject", "")}


def ms_antwoord(toegang, mid, tekst):
    """Antwoord aan de afzender; Outlook zet de oorspronkelijke mail eronder en bewaart het in Verzonden items."""
    _graph(toegang, f"/me/messages/{urllib.parse.quote(mid)}/reply", body={"comment": tekst}, methode="POST")


# ---------------------------------------------------------------- verwijderen en uitschrijven (na een klik)

def gmail_verwijder(toegang, mid):
    """Naar de prullenbak (Gmail haalt hem daar na 30 dagen zelf weg). Niet definitief."""
    _gmail(toegang, f"/messages/{mid}/trash", body={})


def ms_verwijder(toegang, mid):
    """Naar Verwijderde items. Outlook geeft het bericht daarbij een nieuw id."""
    return _graph(toegang, f"/me/messages/{urllib.parse.quote(mid)}/move", body={"destinationId": "deleteditems"}, methode="POST").get("id", "")


def gmail_uitschrijfkoppen(toegang, mid):
    m = _gmail(toegang, f"/messages/{mid}?format=metadata&metadataHeaders=List-Unsubscribe&metadataHeaders=List-Unsubscribe-Post")
    koppen = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}
    return koppen.get("list-unsubscribe", ""), koppen.get("list-unsubscribe-post", "")


def ms_uitschrijfkoppen(toegang, mid):
    m = _graph(toegang, f"/me/messages/{urllib.parse.quote(mid)}?$select=internetMessageHeaders")
    koppen = {(h.get("name") or "").lower(): h.get("value", "") for h in m.get("internetMessageHeaders") or []}
    return koppen.get("list-unsubscribe", ""), koppen.get("list-unsubscribe-post", "")


def uitschrijfwijze(kop, post_kop=""):
    """Hoe je je uitschrijft volgens de koppen List-Unsubscribe (RFC 2369) en List-Unsubscribe-Post (RFC 8058).
    Geeft {"een_klik": url, "mailto": {...}, "link": url}; wat er niet is, is None. Pure functie."""
    adressen = re.findall(r"<([^>]+)>", kop or "") or [x.strip() for x in (kop or "").split(",") if x.strip()]
    webs = [a for a in adressen if a.lower().startswith("https://")]
    mail = next((a for a in adressen if a.lower().startswith("mailto:")), None)
    uit = {"een_klik": None, "mailto": None, "link": webs[0] if webs else None}
    if webs and "list-unsubscribe=one-click" in (post_kop or "").lower().replace(" ", ""):
        uit["een_klik"] = webs[0]
    if mail:
        deel = urllib.parse.urlsplit(mail)
        q = urllib.parse.parse_qs(deel.query)
        adres = urllib.parse.unquote(deel.path)
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", adres):
            uit["mailto"] = {"aan": adres, "onderwerp": (q.get("subject") or ["unsubscribe"])[0][:200],
                             "tekst": (q.get("body") or ["unsubscribe"])[0][:1000]}
    return uit


class _GeenDoorverwijzing(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


def _openbaar_adres(url):
    """Alleen https naar een openbaar adres: nooit naar de server zelf of het interne netwerk (n8n, de database)."""
    import ipaddress  # pylint: disable=import-outside-toplevel
    import socket  # pylint: disable=import-outside-toplevel
    deel = urllib.parse.urlsplit(url)
    if deel.scheme != "https" or not deel.hostname:
        return False
    try:
        adressen = {x[4][0] for x in socket.getaddrinfo(deel.hostname, deel.port or 443)}
    except OSError:
        return False
    return bool(adressen) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in adressen)


def een_klik_uitschrijven(url):
    """Uitschrijven met één klik (RFC 8058): een POST met List-Unsubscribe=One-Click, zonder doorverwijzingen te volgen."""
    if not _openbaar_adres(url):
        raise BronFout("Dit uitschrijfadres is niet veilig of niet bereikbaar; uitschrijven kan via de link in de mail.")
    opener = urllib.request.build_opener(_GeenDoorverwijzing)
    verzoek = urllib.request.Request(url, data=b"List-Unsubscribe=One-Click", method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Buurmanitor/1.0 (Bram)"})
    try:
        with opener.open(verzoek, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return e.code  # doorverwijzing na het uitschrijven: het verzoek is aangekomen
        raise BronFout(f"De afzender weigerde het uitschrijven (fout {e.code}); probeer de link in de mail.") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise BronFout(f"Het uitschrijfadres is niet bereikbaar ({getattr(e, 'reason', e)}).") from e


def gmail_stuur(toegang, account, aan, onderwerp, tekst):
    from email.message import EmailMessage  # pylint: disable=import-outside-toplevel
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = account, aan, onderwerp
    m.set_content(tekst)
    return _gmail(toegang, "/messages/send", body={"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}).get("id", "")


def ms_stuur(toegang, aan, onderwerp, tekst):
    _graph(toegang, "/me/sendMail", body={"message": {"subject": onderwerp, "body": {"contentType": "Text", "content": tekst},
                                                      "toRecipients": [{"emailAddress": {"address": aan}}]}, "saveToSentItems": True}, methode="POST")
