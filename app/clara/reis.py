"""Reistijd: hoe lang Martijn onderweg is naar afspraken buiten de deur, en blokken om die tijd vrij te houden.

- Vertrekpunt: het adres dat Martijn bij Koppelingen invult (staat in de database, niet in de code).
- Vervoer volgens zijn eigen regels: dichtbij (zoals het Huis voor de Stad) met de fiets; verder weg met de auto;
  midden in een grote stad met het openbaar vervoer.
- Routes en adressen komen van OpenRouteService (api.heigit.org). Die kent geen openbaar vervoer: voor OV maakt Clara
  een schatting op basis van de autotijd, en zegt dat erbij.
- Uitkomsten worden bewaard (adressen voor altijd, routes 30 dagen), zodat de dienst weinig vragen krijgt. Het scherm
  rekent alleen met wat al bewaard is; de planner-taak vult aan.
"""
import datetime as dt
import math
import re
import urllib.parse

from ..kern import config
from . import analyse, bronnen

ORS = "https://api.heigit.org"
MARKER = "[clara-reistijd:"
PROFIEL = {"fiets": "cycling-regular", "auto": "driving-car", "lopen": "foot-walking"}
VERVOER = {"fiets": "fiets", "auto": "auto", "ov": "openbaar vervoer", "lopen": "lopen", "geen": "geen reistijd"}
STANDAARD = {"vertrekpunt": "", "fiets_km": 6, "marge": 5, "thuis_tussendoor": 180}
# Centra van grote steden (breedte, lengte, straal in km): ligt een afspraak daarbinnen en is hij niet dichtbij, dan OV.
GROTE_STEDEN = {
    "Amsterdam": (52.3731, 4.8922, 3.0), "Rotterdam": (51.9225, 4.4792, 2.5), "Den Haag": (52.0786, 4.3150, 2.5),
    "Utrecht": (52.0907, 5.1214, 2.0), "Eindhoven": (51.4381, 5.4780, 1.5), "Groningen": (53.2194, 6.5665, 1.5),
    "Tilburg": (51.5606, 5.0919, 1.5), "Nijmegen": (51.8450, 5.8640, 1.5), "Arnhem": (51.9851, 5.8987, 1.5),
    "'s-Hertogenbosch": (51.6889, 5.3036, 1.2), "Breda": (51.5891, 4.7759, 1.2), "Maastricht": (50.8514, 5.6909, 1.5),
    "Haarlem": (52.3812, 4.6366, 1.2), "Leiden": (52.1601, 4.4970, 1.2), "Zwolle": (52.5168, 6.0830, 1.2),
    "Amersfoort": (52.1561, 5.3878, 1.2), "Den Bosch": (51.6889, 5.3036, 1.2),
}


class ReisFout(bronnen.BronFout):
    pass


def actief():
    return bool(config.ORS_API_KEY)


def km(a, b):
    """Afstand hemelsbreed tussen (lengte, breedte)-paren."""
    (lo1, la1), (lo2, la2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


def grote_stad(punt):
    for naam, (la, lo, straal) in GROTE_STEDEN.items():
        if km(punt, (lo, la)) <= straal:
            return naam
    return None


def vervoer(van, naar, inst):
    """Martijns regels: dichtbij de fiets, midden in een grote stad het OV, anders de auto."""
    if km(van, naar) <= float(inst.get("fiets_km", 6)):
        return "fiets"
    return "ov" if grote_stad(naar) else "auto"


def keuze_sleutel(a):
    """Bij een terugkerende afspraak delen alle keren dezelfde iCalUID: de keuze geldt dan voor de hele reeks."""
    uid = (a.get("ical_uid") or "").strip().lower()
    return f"uid:{uid}" if uid else f"id:{a['koppeling_id']}:{a['extern_id']}"


def keuzes(c):
    return {r["sleutel"]: r["vervoer"] for r in c.execute("SELECT sleutel, vervoer FROM agenda_vervoer").fetchall()}


def kies(c, a, vervoer):
    """Vervoer voor een afspraak vastleggen; 'automatisch' haalt de keuze weg (dan gelden de regels weer)."""
    sleutel = keuze_sleutel(a)
    if vervoer not in VERVOER:
        c.execute("DELETE FROM agenda_vervoer WHERE sleutel = %s", (sleutel,))
        return None
    c.execute("""INSERT INTO agenda_vervoer (sleutel, vervoer, titel) VALUES (%s, %s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET vervoer = EXCLUDED.vervoer, titel = EXCLUDED.titel, bijgewerkt = now()""",
              (sleutel, vervoer, (a.get("titel") or "")[:200]))
    return vervoer


def _sleutel(tekst):
    return re.sub(r"\s+", " ", (tekst or "").lower()).strip(" ,")


def _ors(pad, params=None, body=None):
    kop = {"Authorization": config.ORS_API_KEY, "Accept": "application/json"}
    url = f"{ORS}{pad}" + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        return bronnen._http(url, headers=kop, body=body, soort="OpenRouteService")  # pylint: disable=protected-access
    except bronnen.OpnieuwKoppelen as e:
        raise ReisFout("OpenRouteService accepteert de sleutel niet. Controleer hem met clara.sh.") from e


def adres(c, tekst, focus=None, alleen_cache=False):
    """Coördinaten (lengte, breedte) en een leesbaar label voor een plek, of None als hij niet te vinden is."""
    sleutel = _sleutel(tekst)
    if not sleutel:
        return None
    r = c.execute("SELECT * FROM plekken WHERE sleutel = %s", (sleutel,)).fetchone()
    if r:
        return {"punt": (r["lon"], r["lat"]), "label": r["label"]} if r["gevonden"] else None
    if alleen_cache or not actief():
        return None
    params = {"text": tekst, "boundary.country": "NL,BE,DE", "size": "1"}
    if focus:
        params.update({"focus.point.lon": f"{focus[0]:.5f}", "focus.point.lat": f"{focus[1]:.5f}"})
    j = _ors("/pelias/v1/search", params)
    f = (j.get("features") or [None])[0]
    gevonden = bool(f) and (f.get("properties", {}).get("confidence") or 0) >= 0.6
    lon, lat = (f["geometry"]["coordinates"] if f else (None, None))
    label = (f or {}).get("properties", {}).get("label", "") if f else ""
    c.execute("""INSERT INTO plekken (sleutel, tekst, lon, lat, label, gevonden) VALUES (%s, %s, %s, %s, %s, %s)
                 ON CONFLICT (sleutel) DO NOTHING""", (sleutel, tekst[:300], lon, lat, label[:300], gevonden))
    return {"punt": (lon, lat), "label": label} if gevonden else None


def route(c, van, naar, soort, alleen_cache=False):
    """Reistijd in minuten met fiets of auto (met bewaarde uitkomst), of None."""
    sleutel = f"{soort}:{van[0]:.4f},{van[1]:.4f}>{naar[0]:.4f},{naar[1]:.4f}"
    r = c.execute("SELECT duur FROM routes WHERE sleutel = %s AND bijgewerkt > now() - interval '30 days'", (sleutel,)).fetchone()
    if r:
        return r["duur"]
    if alleen_cache or not actief():
        return None
    j = _ors(f"/openrouteservice/v2/directions/{PROFIEL[soort]}", body={"coordinates": [list(van), list(naar)]})
    try:
        seconden = j["routes"][0]["summary"]["duration"]
    except (KeyError, IndexError, TypeError):
        try:
            seconden = j["features"][0]["properties"]["summary"]["duration"]
        except (KeyError, IndexError, TypeError) as e:
            raise ReisFout("OpenRouteService vond geen route.") from e
    minuten = max(1, round(seconden / 60))
    c.execute("""INSERT INTO routes (sleutel, duur) VALUES (%s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET duur = EXCLUDED.duur, bijgewerkt = now()""", (sleutel, minuten))
    return minuten


def reistijd(c, van, naar, soort, alleen_cache=False):
    """(minuten, geschat). Openbaar vervoer kent OpenRouteService niet: autotijd keer 1,5 plus 20 minuten
    (lopen, wachten, overstappen), afgerond op 5 minuten."""
    if soort != "ov":
        return route(c, van, naar, soort, alleen_cache), False
    auto = route(c, van, naar, "auto", alleen_cache)
    if auto is None:
        return None, True
    return int(math.ceil((auto * 1.5 + 20) / 5) * 5), True


def is_reisblok(a):
    return MARKER in (a.get("omschrijving") or "")


def buiten_de_deur(a):
    return analyse.bezet(a) and not a.get("online") and bool((a.get("locatie") or "").strip()) and not is_reisblok(a)


def plek_tekst(a):
    """De tekst om op te zoeken. Vergaderzalen uit de raadsagenda liggen in Helmond."""
    t = (a.get("locatie") or "").strip()
    return f"{t}, Helmond" if a.get("soort") == "raad" and "helmond" not in t.lower() else t


def _sleutel_blok(a, richting):
    return f"{a['koppeling_id']}:{a['extern_id']}:{richting}"


def plan(c, afspraken, inst, nu=None, alleen_cache=True):
    """Per afspraak buiten de deur: reis heen (vanaf de vorige afspraak of thuis) en, na de laatste van een blok,
    terug naar huis. Elke regel zegt of het past en of het blok al in de agenda staat."""
    inst = {**STANDAARD, **(inst or {})}
    tz = config.TZ
    if not inst["vertrekpunt"]:
        return {"fout": "Vul je vertrekpunt in bij Koppelingen › Reistijd.", "regels": []}
    thuis = adres(c, inst["vertrekpunt"], alleen_cache=alleen_cache)
    if not thuis:
        return {"fout": "Het vertrekpunt is (nog) niet gevonden." if actief() else "Er is nog geen sleutel voor OpenRouteService (clara.sh).",
                "regels": []}
    ingepland = {}
    for a in afspraken:
        m = re.search(re.escape(MARKER) + r"([^\]]+)\]", a.get("omschrijving") or "")
        if m:
            ingepland[m.group(1)] = a
    reis = sorted((a for a in afspraken if buiten_de_deur(a)), key=lambda a: a["start"])
    bezette = sorted((a for a in afspraken if analyse.bezet(a) and not is_reisblok(a)), key=lambda a: a["start"])
    regels, onbekend = [], []
    gekozen = keuzes(c)
    marge, tussendoor = int(inst["marge"]), dt.timedelta(minutes=int(inst["thuis_tussendoor"]))
    for i, a in enumerate(reis):
        doel = adres(c, plek_tekst(a), focus=thuis["punt"], alleen_cache=alleen_cache)
        if not doel:
            onbekend.append(a)
            continue
        if km(thuis["punt"], doel["punt"]) < 0.15:
            continue  # afspraak thuis
        vorige = reis[i - 1] if i else None
        vorige_doel = adres(c, plek_tekst(vorige), focus=thuis["punt"], alleen_cache=True) if vorige else None
        vanaf_vorige = bool(vorige and vorige_doel and vorige["eind"].astimezone(tz).date() == a["start"].astimezone(tz).date()
                            and a["start"] - vorige["eind"] < tussendoor and vorige["eind"] <= a["start"])
        if vanaf_vorige and km(vorige_doel["punt"], doel["punt"]) < 0.15:
            continue  # zelfde plek als de vorige afspraak
        van, van_naam = (vorige_doel["punt"], vorige["locatie"]) if vanaf_vorige else (thuis["punt"], "thuis")
        keuze = gekozen.get(keuze_sleutel(a))
        if keuze == "geen":
            regels.append({"afspraak": a, "richting": "heen", "vervoer": "geen", "gekozen": True, "minuten": None,
                           "sleutel": _sleutel_blok(a, "heen"), "ingepland": False, "past": True, "van": van_naam, "naar": a["locatie"]})
            continue
        soort = keuze or vervoer(van, doel["punt"], inst)
        minuten, geschat = reistijd(c, van, doel["punt"], soort, alleen_cache)
        if minuten is not None:
            minuten += marge
            start = a["start"] - dt.timedelta(minutes=minuten)
            in_de_weg = [b for b in bezette if b is not a and b["start"] < a["start"] and b["eind"] > start]
            regels.append({"afspraak": a, "richting": "heen", "van": van_naam, "naar": a["locatie"], "vervoer": soort,
                           "gekozen": bool(keuze), "minuten": minuten, "geschat": geschat, "start": start, "eind": a["start"], "past": not in_de_weg,
                           "botst": in_de_weg[0]["titel"] if in_de_weg else "",
                           "sleutel": _sleutel_blok(a, "heen"), "ingepland": _sleutel_blok(a, "heen") in ingepland})
        else:
            regels.append({"afspraak": a, "richting": "heen", "van": van_naam, "naar": a["locatie"], "vervoer": soort,
                           "gekozen": bool(keuze), "minuten": None, "sleutel": _sleutel_blok(a, "heen"), "ingepland": False, "past": True})
        volgende = reis[i + 1] if i + 1 < len(reis) else None
        door = bool(volgende and volgende["start"].astimezone(tz).date() == a["eind"].astimezone(tz).date()
                    and volgende["start"] - a["eind"] < tussendoor and volgende["start"] >= a["eind"])
        if not door:
            terug = keuze or vervoer(doel["punt"], thuis["punt"], inst)
            m2, g2 = reistijd(c, doel["punt"], thuis["punt"], terug, alleen_cache)
            if m2 is not None:
                regels.append({"afspraak": a, "richting": "terug", "van": a["locatie"], "naar": "thuis", "vervoer": terug,
                               "gekozen": bool(keuze), "minuten": m2 + marge, "geschat": g2, "start": a["eind"],
                               "eind": a["eind"] + dt.timedelta(minutes=m2 + marge), "past": True, "botst": "",
                               "sleutel": _sleutel_blok(a, "terug"), "ingepland": _sleutel_blok(a, "terug") in ingepland})
    return {"fout": "", "regels": regels, "onbekend": onbekend, "thuis": thuis}


def blok_titel(r):
    woord = {"fiets": "fietsen", "auto": "rijden", "ov": "OV", "lopen": "lopen"}[r["vervoer"]]
    if r["richting"] == "heen":
        return f"Reistijd naar {r['naar']} ({woord})"
    return f"Terugreis naar huis ({woord})"


def blok_omschrijving(r):
    return (f"{MARKER}{r['sleutel']}] Ingepland door Clara (Buurmanitor). Vervoer: {r['vervoer']}, {r['minuten']} min"
            + (" (schatting: OpenRouteService kent geen OV)" if r.get("geschat") else " (OpenRouteService)") + ".")
