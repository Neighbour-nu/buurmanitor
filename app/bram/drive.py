"""Bijlagen opslaan in Google Drive, voor regels als 'facturen van KPN naar Administratie/Facturen/{jaar}'.

De map staat in de regel als pad vanaf Mijn Drive ('Administratie/Facturen/{jaar}') of als link naar een map, eventueel
met een pad erachter ('https://drive.google.com/drive/folders/ID/{jaar}'); zo werkt het ook met een gedeelde drive.
Mappen die er nog niet zijn, maakt Bram aan. Een bestand met dezelfde naam in dezelfde map slaat hij niet nog eens op,
zodat een regel veilig opnieuw kan draaien.

Toegang: het Google-account van een Gmail-koppeling, met de extra toestemming voor Drive (Post, Instellingen).
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..clara.bronnen import AGENT, BronFout, OpnieuwKoppelen, _http

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
MAP = "application/vnd.google-apps.folder"
ALLE = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
LINK = re.compile(r"(?:/folders/|[?&]id=)([A-Za-z0-9_-]{10,})")
_mappen = {}  # (koppeling, pad) -> (map-id, tijd)
GELDIG = 600


def _uitleg(e):
    """Een fout van Drive in gewone woorden, met wat je eraan kunt doen."""
    t = str(e)
    if "insufficient" in t.lower() or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in t:
        return BronFout("Buurmanitor mag niet in Google Drive van dit account. Klik bij Post > Instellingen op 'Google Drive toestaan'.")
    if "has not been used" in t or "is disabled" in t or "accessNotConfigured" in t:
        return BronFout("De Google Drive API staat nog niet aan in het Google Cloud-project van Buurmanitor. Zet hem aan bij "
                        "APIs & Services > Library > Google Drive API en probeer het daarna opnieuw.")
    if "storageQuotaExceeded" in t or ("quota" in t.lower() and "storage" in t.lower()):
        return BronFout("Google Drive is vol.")
    return e


def _drive(toegang, pad, params=None, **kw):
    url = f"{DRIVE}{pad}?" + urllib.parse.urlencode({**ALLE, **(params or {})})
    try:
        return _http(url, headers={"Authorization": f"Bearer {toegang}"}, soort="Google Drive", **kw)
    except OpnieuwKoppelen:
        raise
    except BronFout as e:
        raise _uitleg(e) from e


def _q(tekst):
    return tekst.replace("\\", "\\\\").replace("'", "\\'")


def schone_naam(naam, maximaal=180):
    """Een bestands- of mapnaam zonder tekens die in Drive (of later bij downloaden) lastig zijn. Pure functie."""
    naam = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", naam or "")
    naam = re.sub(r"\s{2,}", " ", naam).strip(" .")
    return (naam or "bijlage")[:maximaal]


def delen(map_tekst):
    """'link/Sub/{jaar}' of 'Administratie/Facturen' -> (begin-id of 'root', [mapnamen]). Pure functie."""
    tekst = (map_tekst or "").strip()
    m = LINK.search(tekst)
    if m:
        begin, rest = m.group(1), tekst[m.end():]
        rest = re.sub(r"^[?#][^/]*", "", rest)  # ?usp=sharing en dergelijke
    else:
        begin, rest = "root", tekst
    namen = [schone_naam(x) for x in re.split(r"\s*/\s*", rest) if x.strip()]
    return begin, namen


def invullen(map_tekst, waarden):
    """{jaar}, {maand}, {afzender} en {account} in het pad. Pure functie."""
    return re.sub(r"\{(jaar|maand|afzender|account)\}", lambda m: schone_naam(str(waarden.get(m.group(1), "")), 60) or "onbekend",
                  map_tekst or "")


def _kind(toegang, ouder, naam):
    q = f"'{_q(ouder)}' in parents and name = '{_q(naam)}' and mimeType = '{MAP}' and trashed = false"
    r = _drive(toegang, "/files", {"q": q, "fields": "files(id,name)", "pageSize": "5", "corpora": "allDrives"})
    return (r.get("files") or [{}])[0].get("id")


def map_id(toegang, koppeling_id, map_tekst):
    """Het id van de map (aangemaakt waar nodig)."""
    sleutel = (koppeling_id, map_tekst)
    bewaard = _mappen.get(sleutel)
    if bewaard and time.time() - bewaard[1] < GELDIG:
        return bewaard[0]
    ouder, namen = delen(map_tekst)
    if ouder != "root":
        _drive(toegang, f"/files/{urllib.parse.quote(ouder)}", {"fields": "id"})  # bestaat de map uit de link (en mag ik erbij)?
    for naam in namen:
        gevonden = _kind(toegang, ouder, naam)
        if not gevonden:
            gevonden = _drive(toegang, "/files", {"fields": "id"}, body={"name": naam, "mimeType": MAP, "parents": [ouder]},
                              methode="POST")["id"]
        ouder = gevonden
    _mappen[sleutel] = (ouder, time.time())
    return ouder


def bestaat(toegang, map_, naam):
    q = f"'{_q(map_)}' in parents and name = '{_q(naam)}' and trashed = false"
    return bool(_drive(toegang, "/files", {"q": q, "fields": "files(id)", "pageSize": "1", "corpora": "allDrives"}).get("files"))


def uploaden(toegang, map_, naam, soort, data):
    """Een bestand in de map zetten (hervatbaar uploaden, zodat ook een bijlage van 20 MB in één keer goed gaat).
    Geeft het id."""
    kop = {"Authorization": f"Bearer {toegang}", "User-Agent": AGENT}
    meta = json.dumps({"name": naam, "parents": [map_]}).encode()
    try:
        start = urllib.request.Request(f"{UPLOAD}?uploadType=resumable&supportsAllDrives=true&fields=id", data=meta, method="POST",
                                       headers={**kop, "Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Type": soort,
                                                "X-Upload-Content-Length": str(len(data))})
        with urllib.request.urlopen(start, timeout=30) as r:
            adres = r.headers.get("Location")
        if not adres:
            raise BronFout("Google Drive gaf geen uploadadres terug.")
        with urllib.request.urlopen(urllib.request.Request(adres, data=data, method="PUT", headers={**kop, "Content-Type": soort}),
                                    timeout=120) as r:
            return json.loads(r.read() or b"{}").get("id", "")
    except urllib.error.HTTPError as e:
        tekst = e.read()[:1000].decode("utf-8", "replace")
        if e.code == 401:
            raise OpnieuwKoppelen("Google Drive accepteert de toegang niet meer. Koppel dit Gmail-account opnieuw.") from e
        try:
            uitleg = (json.loads(tekst).get("error") or {}).get("message") or tekst
        except (ValueError, AttributeError):
            uitleg = tekst
        raise _uitleg(BronFout(f"Google Drive gaf fout {e.code}: {uitleg[:200]}")) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise BronFout(f"Google Drive is nu niet bereikbaar ({getattr(e, 'reason', e)}).") from e


def map_link(map_):
    return f"https://drive.google.com/drive/folders/{map_}" if map_ != "root" else "https://drive.google.com/drive/my-drive"
