"""Vergaderingen en agenda's van de gemeente Helmond uit de openbare Notubiz-API.

Helmond is organisatie 1949. Een commissieavond is een 'assembly' met daarin een vergadering per
commissie (Commissie A in de Raadzaal, Commissie B in de Commissiekamer). De raad is een gewone
'meeting'. Agendapunten met heading=true zijn tussenkoppen, zoals Adviserend of Hamerstukken.
"""
import datetime as dt
import json
import re
import urllib.error
import urllib.request

API = "https://api.notubiz.nl"
ORGANISATIE = 1949
VERSIE = "1.10.8"
WEB = "https://helmond.notubiz.nl"

# Vaste agendapunten die nooit in een Whaller-bericht komen.
STANDAARD_PUNTEN = re.compile(
    r"^(opening|sluiting|vaststellen (ontwerp)?agenda|voorstel hamerstuk|vaststellen besluitenlijst|"
    r"aanwijzen van een lid|behandeling ingekomen stukken|ter kennisname|voor advies naar college|"
    r"ter afdoening college|vng$|mededelingen)", re.I)
TIJD = re.compile(r"\s*-?\s*\(?\s*(\d{1,2})[.:](\d{2})\s*(?:-|tot)\s*(\d{1,2})[.:](\d{2})\s*(?:uur)?\s*\)?\s*$")
PAUZE = re.compile(r"^\s*\d+\s*min|pauze|uitloop|schorsing|wissel", re.I)
BEHANDELVORMEN = ("adviserend", "informerend", "beeldvormend", "oordeelvormend", "besluitvormend",
                  "consulterend", "hamerstukken", "bespreekstukken")


def _haal(pad, wachttijd=30, pogingen=1, **params):
    params.update(format="json", version=VERSIE)
    query = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    verzoek = urllib.request.Request(f"{API}/{pad}?{query}", headers={"User-Agent": "Promonitor (PRO Helmond)"})
    for poging in range(pogingen):
        try:
            with urllib.request.urlopen(verzoek, timeout=wachttijd) as r:
                return json.load(r)
        except (TimeoutError, OSError) as e:
            # Een 401 (niet openbaar) of 404 heeft geen zin om te herhalen.
            if isinstance(e, urllib.error.HTTPError) or poging == pogingen - 1:
                raise


def niet_openbaar(fout):
    """Notubiz geeft 401 bij vergaderingen die alleen voor raadsleden zijn (werkbezoeken, besloten sessies)."""
    return isinstance(fout, urllib.error.HTTPError) and fout.code in (401, 403)


def _attr(attributen, nummer):
    for a in attributen or []:
        if a.get("id") == nummer:
            return (a.get("value") or "").strip()
    return ""


def _start(event):
    p = (event.get("plannings") or [{}])[0].get("start_date")
    return dt.datetime.strptime(p, "%Y-%m-%d %H:%M:%S") if p else None


def soort_van(naam):
    n = (naam or "").lower()
    if n.startswith("commissieavond") or n.startswith("commissie"):
        return "commissie"
    if n.startswith("raadsvergadering"):
        return "raad"
    return "overig"


def evenementen(van, tot):
    """Alle vergaderingen en bijeenkomsten tussen twee datums."""
    j = _haal("events", organisation_id=ORGANISATIE,
              date_from=f"{van:%Y-%m-%d} 00:00:00", date_to=f"{tot:%Y-%m-%d} 23:59:59")
    uit = []
    for e in j.get("events", []):
        if e.get("canceled") or e.get("inactive"):
            continue
        naam = _attr(e.get("attributes"), 1)
        uit.append({"id": e["id"], "type": e["type"], "naam": naam, "soort": soort_van(naam),
                    "start": _start(e), "plek": _attr(e.get("attributes"), 50),
                    "toelichting": _schoon_html(_attr(e.get("attributes"), 3)),
                    # Niet openbaar: de stukken zijn alleen te zien als je bent ingelogd in Notubiz.
                    "alleen_leden": (e.get("permission_group") or "public") != "public",
                    "url": web_url(e["type"], e["id"], naam, _start(e))})
    return sorted(uit, key=lambda x: x["start"] or dt.datetime.max)


def web_url(soort, nummer, naam="", start=None):
    if soort == "assembly":
        achter = f"/{re.sub(r'[^A-Za-z0-9]+', '_', naam).strip('_')}_{start:%d-%m-%Y}" if naam and start else ""
        return f"{WEB}/bijeenkomst/{nummer}{achter}"
    return f"{WEB}/vergadering/{nummer}"


def _schoon_html(tekst):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tekst or "")).strip()


def splits_tijd(titel):
    """'Afwegingskader sportaccommodaties - (19.35-20.50 uur)' -> ('Afwegingskader sportaccommodaties', '19.35-20.50')."""
    m = TIJD.search(titel or "")
    if not m:
        return (titel or "").strip(" -"), ""
    onderwerp = titel[:m.start()].strip(" -")
    return onderwerp, f"{int(m.group(1))}.{m.group(2)}-{int(m.group(3))}.{m.group(4)}"


def agenda(meeting):
    """Platte lijst agendapunten met de tussenkop (sectie) waar ze onder vallen."""
    uit, sectie = [], ""

    def loop(items, diepte):
        nonlocal sectie
        for i in items or []:
            td = i.get("type_data") or {}
            titel = _attr(td.get("attributes"), 1)
            toelichting = _schoon_html(_attr(td.get("attributes"), 3))
            if td.get("heading"):
                # Tussenkoppen als 'Adviserend' of 'Hamerstukken'; pauzes en uitloop tellen niet.
                if diepte == 0 and not PAUZE.search(titel):
                    sectie = titel
                continue
            onderwerp, tijd = splits_tijd(titel)
            wethouder = ""
            m = re.search(r"(?:portefeuillehouder:\s*)?(wethouder|burgemeester)\s+([A-Z][\w\-' ]+)", toelichting, re.I)
            if m:
                wethouder = f"{m.group(1).lower()} {m.group(2).strip()}"
            documenten = [{"id": dd.get("id"), "titel": (dd.get("title") or "").strip(), "url": dd.get("url") or "",
                           "typen": [t.get("value") for t in dd.get("types") or [] if isinstance(t, dict)]}
                          for dd in i.get("documents") or [] if not dd.get("confidential")]
            uit.append({"id": i.get("id"), "nummer": (td.get("title_prefix") or "").strip(), "titel": titel,
                        "documenten": documenten,
                        "onderwerp": onderwerp, "tijd": tijd, "sectie": sectie, "diepte": diepte,
                        "toelichting": toelichting, "portefeuillehouder": wethouder,
                        "standaard": bool(STANDAARD_PUNTEN.match(onderwerp)) or onderwerp.lower().startswith("hamerstuk:")})
            loop(i.get("agenda_items"), diepte + 1)

    loop(meeting.get("agenda_items"), 0)
    return uit


def vergadering(nummer):
    m = _haal(f"events/meetings/{nummer}")["meeting"]
    return {"id": m["id"], "naam": _attr(m.get("attributes"), 1), "plek": _attr(m.get("attributes"), 50),
            "start": _start(m), "agenda": agenda(m), "url": web_url("meeting", m["id"])}


def commissieavond(nummer):
    """Een commissieavond met per commissie de agenda."""
    a = _haal(f"events/assemblies/{nummer}")["assembly"]
    naam, start = _attr(a.get("attributes"), 1), _start(a)
    delen = [vergadering(m["id"]) for m in sorted(a.get("meetings", []), key=lambda m: m.get("order") or 0)]
    delen.sort(key=lambda v: v["naam"])
    return {"id": a["id"], "naam": naam, "start": start, "plek": _attr(a.get("attributes"), 50),
            "commissies": delen, "url": web_url("assembly", a["id"], naam, start)}


# ---------- modules: raadsinformatiebrieven, vragen, moties, toezeggingen, ingekomen stukken ----------

MODULES = {8: "rib", 4: "vraag", 6: "motie", 3: "toezegging", 1: "ingekomen"}


def _lijst(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


def module_items(module, limiet=None):
    params = {"limit": limiet} if limiet else {}
    # De lijsten zijn groot (de raadsvragen zijn bijna 2 MB): ruim de tijd, en één keer opnieuw proberen.
    j = _haal(f"organisations/{ORGANISATIE}/modules/{module}/items", wachttijd=180, pogingen=2, **params)
    return _lijst(j.get("item"))


def lees_item(item, module):
    """Een module-item als platte dict: velden (label -> tekst) en documenten."""
    velden, documenten = {}, []
    for a in _lijst((item.get("attributes") or {}).get("attribute")):
        label = (a.get("label") or "").strip()
        if "values" in a:
            waarden = [v.get("@cdata") if isinstance(v, dict) else v for v in _lijst((a.get("values") or {}).get("value"))]
            velden[label] = ", ".join(str(w).strip() for w in waarden if w)
            continue
        waarde = a.get("value")
        if isinstance(waarde, dict) and waarde.get("url"):
            documenten.append({"id": (waarde.get("@attributes") or {}).get("id"), "titel": (waarde.get("title") or "").strip(),
                               "url": waarde["url"], "label": label})
            velden[label] = (waarde.get("title") or "").strip()
        elif isinstance(waarde, str):
            velden[label] = _schoon_html(waarde)
    meta = item.get("@attributes") or {}
    return {"id": meta.get("id"), "module": module, "gewijzigd": meta.get("last_modified"),
            "velden": velden, "documenten": documenten}


def document_ophalen(url, maximum=30 * 1024 * 1024):
    verzoek = urllib.request.Request(url, headers={"User-Agent": "Promonitor (PRO Helmond)"})
    with urllib.request.urlopen(verzoek, timeout=90) as r:
        inhoud = r.read(maximum + 1)
    if len(inhoud) > maximum:
        raise ValueError("document te groot")
    return inhoud
