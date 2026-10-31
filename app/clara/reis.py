"""Reistijd: hoe lang Martijn onderweg is naar afspraken buiten de deur, en blokken om die tijd vrij te houden.

- Vertrekpunt: het adres dat Martijn bij Koppelingen invult (staat in de database, niet in de code).
- Vervoer volgens zijn eigen regels: dichtbij (zoals het Huis voor de Stad) met de fiets; verder weg met de auto;
  midden in een grote stad met het openbaar vervoer.
- Routedienst: Google Maps als er een sleutel is (Routes API en Geocoding API, met echte OV-reistijd voor het tijdstip
  van de afspraak), anders OpenRouteService (api.heigit.org). OpenRouteService kent geen openbaar vervoer: dan maakt
  Clara voor OV een schatting op basis van de autotijd, en zegt dat erbij. Vindt Google een adres niet, dan probeert
  Clara OpenRouteService (als die sleutel er ook is).
- Uitkomsten worden bewaard (adressen voor altijd, routes 30 dagen, ook 'geen route'), zodat de dienst weinig vragen
  krijgt en je binnen het gratis maandvolume van Google blijft. Het scherm rekent alleen met wat al bewaard is; de
  planner-taak vult aan.
"""
import datetime as dt
import math
import re
import urllib.parse

from ..kern import config
from . import analyse, bronnen

ORS = "https://api.heigit.org"
GOOGLE_ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
MODUS = {"fiets": "BICYCLE", "auto": "DRIVE", "lopen": "WALK", "ov": "TRANSIT"}
GEEN_ROUTE = -1  # bewaard als er geen route is (bijvoorbeeld 's nachts geen OV), zodat Clara het niet steeds opnieuw vraagt
MARKER = "[clara-reistijd:"
PROFIEL = {"fiets": "cycling-regular", "auto": "driving-car", "lopen": "foot-walking"}
VERVOER = {"fiets": "fiets", "auto": "auto", "ov": "openbaar vervoer", "lopen": "lopen", "geen": "geen reistijd"}
STANDAARD = {"vertrekpunt": "", "fiets_km": 6, "marge": 5, "thuis_tussendoor": 180, "reis_kleur": "8"}  # 8 = Grafiet
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


def dienst():
    """Welke routedienst Clara gebruikt: 'google', 'ors' of None."""
    if config.GOOGLE_MAPS_KEY:
        return "google"
    return "ors" if config.ORS_API_KEY else None


def dienst_naam():
    return {"google": "Google Maps", "ors": "OpenRouteService"}.get(dienst(), "geen routedienst")


def actief():
    return dienst() is not None


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


def gaat_mee(a, gekozen):
    """Een afspraak uit een agenda die niet 'bezet voor mij' is (zoals die van Sanne) wordt toch Martijns afspraak
    zodra hij er vervoer voor kiest: hij brengt haar of gaat mee. Kiest hij 'geen reistijd' of 'automatisch', dan niet."""
    return gekozen.get(keuze_sleutel(a)) not in (None, "geen")


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


def _google(url, params=None, body=None, kop=None):
    try:
        return bronnen._http(url + ("?" + urllib.parse.urlencode(params) if params else ""), headers=kop or {},  # pylint: disable=protected-access
                             body=body, soort="Google Maps")
    except bronnen.OpnieuwKoppelen as e:
        raise ReisFout("Google Maps accepteert de sleutel niet. Controleer hem met clara.sh.") from e


def _google_adres(tekst, focus):
    params = {"address": tekst, "key": config.GOOGLE_MAPS_KEY, "region": "nl", "language": "nl"}
    if focus:  # voorkeur voor de omgeving van het vertrekpunt (geen harde grens)
        lo, la = focus
        params["bounds"] = f"{la - 0.25:.4f},{lo - 0.4:.4f}|{la + 0.25:.4f},{lo + 0.4:.4f}"
    j = _google(GOOGLE_GEOCODE, params)
    status = j.get("status", "")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        raise ReisFout(f"Google Maps (adressen) gaf {status}: {j.get('error_message', '')[:150]}")
    r = j["results"][0]
    precies = {"establishment", "point_of_interest", "premise", "street_address", "subpremise", "route", "locality"}
    if r.get("partial_match") and not precies & set(r.get("types", [])):
        return None
    plek = r["geometry"]["location"]
    return (plek["lng"], plek["lat"]), r.get("formatted_address", tekst)


def _ors_adres(tekst, focus):
    params = {"text": tekst, "boundary.country": "NL,BE,DE", "size": "1"}
    if focus:
        params.update({"focus.point.lon": f"{focus[0]:.5f}", "focus.point.lat": f"{focus[1]:.5f}"})
    j = _ors("/pelias/v1/search", params)
    f = (j.get("features") or [None])[0]
    if not f or (f.get("properties", {}).get("confidence") or 0) < 0.6:
        return None
    return tuple(f["geometry"]["coordinates"]), f.get("properties", {}).get("label", tekst)


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
    uitkomst = _google_adres(tekst, focus) if config.GOOGLE_MAPS_KEY else None
    if uitkomst is None and config.ORS_API_KEY:
        uitkomst = _ors_adres(tekst, focus)
    gevonden = uitkomst is not None
    (lon, lat), label = uitkomst if gevonden else ((None, None), "")
    c.execute("""INSERT INTO plekken (sleutel, tekst, lon, lat, label, gevonden) VALUES (%s, %s, %s, %s, %s, %s)
                 ON CONFLICT (sleutel) DO NOTHING""", (sleutel, tekst[:300], lon, lat, label[:300], gevonden))
    return {"punt": (lon, lat), "label": label} if gevonden else None


def _vijf_minuten(t):
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


def _google_route(van, naar, soort, tijd, aankomst):
    def punt(p):
        return {"location": {"latLng": {"latitude": p[1], "longitude": p[0]}}}
    body = {"origin": punt(van), "destination": punt(naar), "travelMode": MODUS[soort], "languageCode": "nl", "units": "METRIC"}
    if soort == "auto":
        body["routingPreference"] = "TRAFFIC_UNAWARE"  # zonder files: blijft in het goedkope (gratis) deel
    if soort == "ov" and tijd:
        body["arrivalTime" if aankomst else "departureTime"] = tijd.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    j = _google(GOOGLE_ROUTES, body=body, kop={"X-Goog-Api-Key": config.GOOGLE_MAPS_KEY,
                                               "X-Goog-FieldMask": "routes.duration,routes.distanceMeters"})
    routes = j.get("routes") or []
    if not routes or not routes[0].get("duration"):
        return None
    return float(str(routes[0]["duration"]).rstrip("s"))


def _ors_route(van, naar, soort):
    j = _ors(f"/openrouteservice/v2/directions/{PROFIEL[soort]}", body={"coordinates": [list(van), list(naar)]})
    try:
        return j["routes"][0]["summary"]["duration"]
    except (KeyError, IndexError, TypeError):
        try:
            return j["features"][0]["properties"]["summary"]["duration"]
        except (KeyError, IndexError, TypeError):
            return None


def route(c, van, naar, soort, alleen_cache=False, tijd=None, aankomst=True):
    """Reistijd in minuten (met bewaarde uitkomst), of None. OV alleen met Google Maps, voor het tijdstip van de
    afspraak (aankomen voor het begin, of vertrekken na het einde)."""
    bron = dienst()
    if bron is None or (soort == "ov" and bron != "google"):
        return None
    sleutel = f"{bron}:{soort}:{van[0]:.4f},{van[1]:.4f}>{naar[0]:.4f},{naar[1]:.4f}"
    if soort == "ov":
        sleutel += f"@{'a' if aankomst else 'v'}{_vijf_minuten(tijd):%Y%m%d%H%M}" if tijd else "@?"
    r = c.execute("SELECT duur FROM routes WHERE sleutel = %s AND bijgewerkt > now() - interval '30 days'", (sleutel,)).fetchone()
    if r:
        return None if r["duur"] == GEEN_ROUTE else r["duur"]
    if alleen_cache:
        return None
    seconden = _google_route(van, naar, soort, tijd, aankomst) if bron == "google" else _ors_route(van, naar, soort)
    minuten = GEEN_ROUTE if seconden is None else max(1, round(seconden / 60))
    c.execute("""INSERT INTO routes (sleutel, duur) VALUES (%s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET duur = EXCLUDED.duur, bijgewerkt = now()""", (sleutel, minuten))
    return None if minuten == GEEN_ROUTE else minuten


def reistijd(c, van, naar, soort, alleen_cache=False, tijd=None, aankomst=True):
    """(minuten, geschat). OV met Google Maps is echt (voor het tijdstip van de afspraak). Zonder Google, of als Google
    op dat tijdstip geen OV-verbinding vindt: autotijd keer 1,5 plus 20 minuten (lopen, wachten, overstappen),
    afgerond op 5 minuten, als schatting."""
    if soort != "ov":
        return route(c, van, naar, soort, alleen_cache), False
    echt = route(c, van, naar, "ov", alleen_cache, tijd=tijd, aankomst=aankomst)
    if echt is not None:
        return echt, False
    auto = route(c, van, naar, "auto", alleen_cache)
    if auto is None:
        return None, True
    return int(math.ceil((auto * 1.5 + 20) / 5) * 5), True


def is_reisblok(a):
    return MARKER in (a.get("omschrijving") or "")


def buiten_de_deur(a):
    if not analyse.bezet(a) or is_reisblok(a):
        return False
    if (a.get("reisadres") or "").strip():  # zelf ingevuld adres: altijd reizen, ook als er een online link in staat
        return True
    return not a.get("online") and bool((a.get("locatie") or "").strip())


THUIS_WOORDEN = {"thuis", "home", "bij mij thuis", "thuiswerken", "thuiskantoor", "werken vanuit huis"}


def is_thuis_tekst(a, inst=None):
    """Staat er in de afspraak dat hij thuis is (of het eigen adres)?"""
    t = _sleutel(plek_naam(a))
    return bool(t) and (t in THUIS_WOORDEN or (bool(inst) and t == _sleutel((inst or {}).get("vertrekpunt", ""))))


def gedekte_reeksen(c):
    """Reeksen (koppeling, reeks-id) waarvoor al een herhalend reistijdblok van Clara staat."""
    try:
        return {(r["bron_koppeling"], r["bron_reeks"]) for r in c.execute(
            "SELECT bron_koppeling, bron_reeks FROM agenda_reeksen WHERE soort = 'reistijd' AND status = 'actief'").fetchall()}
    except Exception:  # noqa: BLE001  (zonder database, zoals in sommige tests: niets gedekt)
        return set()


def plek_naam(a):
    return (a.get("reisadres") or "").strip() or (a.get("locatie") or "").strip()


def reisinfo(c):
    return {r["sleutel"]: r for r in c.execute("SELECT * FROM agenda_reisinfo").fetchall()}


def reisinfo_opslaan(c, a, adres=None, auto=None):
    sleutel = keuze_sleutel(a)
    oud = c.execute("SELECT * FROM agenda_reisinfo WHERE sleutel = %s", (sleutel,)).fetchone() or {}
    adres = (oud.get("adres", "") if adres is None else adres.strip())[:300]
    auto = bool(oud.get("auto", False) if auto is None else auto)
    c.execute("""INSERT INTO agenda_reisinfo (sleutel, adres, auto, titel) VALUES (%s, %s, %s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET adres = EXCLUDED.adres, auto = EXCLUDED.auto, titel = EXCLUDED.titel,
                 bijgewerkt = now()""", (sleutel, adres, auto, (a.get("titel") or "")[:200]))


def plek_tekst(a):
    """De tekst om op te zoeken. Vergaderzalen uit de raadsagenda liggen in Helmond."""
    if (a.get("reisadres") or "").strip():
        return a["reisadres"].strip()
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
    regels, onbekend, thuis_afspraken = [], [], []
    gekozen = keuzes(c)
    gedekt = gedekte_reeksen(c)

    def staat_er(a, richting):
        return _sleutel_blok(a, richting) in ingepland or (bool(a.get("reeks")) and (a["koppeling_id"], a["reeks"]) in gedekt)
    marge, tussendoor = int(inst["marge"]), dt.timedelta(minutes=int(inst["thuis_tussendoor"]))
    for i, a in enumerate(reis):
        if is_thuis_tekst(a, inst):
            thuis_afspraken.append(a)
            continue
        doel = adres(c, plek_tekst(a), focus=thuis["punt"], alleen_cache=alleen_cache)
        if not doel:
            onbekend.append(a)
            continue
        if km(thuis["punt"], doel["punt"]) < 0.15:
            thuis_afspraken.append(a)
            continue  # afspraak thuis
        vorige = reis[i - 1] if i else None
        vorige_doel = adres(c, plek_tekst(vorige), focus=thuis["punt"], alleen_cache=True) if vorige else None
        vanaf_vorige = bool(vorige and vorige_doel and vorige["eind"].astimezone(tz).date() == a["start"].astimezone(tz).date()
                            and a["start"] - vorige["eind"] < tussendoor and vorige["eind"] <= a["start"])
        if vanaf_vorige and km(vorige_doel["punt"], doel["punt"]) < 0.15:
            continue  # zelfde plek als de vorige afspraak
        van, van_naam = (vorige_doel["punt"], plek_naam(vorige)) if vanaf_vorige else (thuis["punt"], "thuis")
        keuze = gekozen.get(keuze_sleutel(a))
        if keuze == "geen":
            regels.append({"afspraak": a, "richting": "heen", "vervoer": "geen", "gekozen": True, "minuten": None,
                           "sleutel": _sleutel_blok(a, "heen"), "ingepland": False, "past": True, "van": van_naam, "naar": plek_naam(a)})
            continue
        soort = keuze or vervoer(van, doel["punt"], inst)
        minuten, geschat = reistijd(c, van, doel["punt"], soort, alleen_cache, tijd=a["start"], aankomst=True)
        if minuten is not None:
            minuten += marge
            start = a["start"] - dt.timedelta(minutes=minuten)
            in_de_weg = [b for b in bezette if b is not a and b["start"] < a["start"] and b["eind"] > start]
            regels.append({"afspraak": a, "richting": "heen", "van": van_naam, "naar": plek_naam(a), "vervoer": soort,
                           "gekozen": bool(keuze), "minuten": minuten, "geschat": geschat, "start": start, "eind": a["start"], "past": not in_de_weg,
                           "botst": in_de_weg[0]["titel"] if in_de_weg else "",
                           "sleutel": _sleutel_blok(a, "heen"), "ingepland": staat_er(a, "heen")})
        else:
            regels.append({"afspraak": a, "richting": "heen", "van": van_naam, "naar": plek_naam(a), "vervoer": soort,
                           "gekozen": bool(keuze), "minuten": None, "sleutel": _sleutel_blok(a, "heen"), "ingepland": False, "past": True})
        volgende = reis[i + 1] if i + 1 < len(reis) else None
        door = bool(volgende and volgende["start"].astimezone(tz).date() == a["eind"].astimezone(tz).date()
                    and volgende["start"] - a["eind"] < tussendoor and volgende["start"] >= a["eind"])
        if not door:
            terug = keuze or vervoer(doel["punt"], thuis["punt"], inst)
            m2, g2 = reistijd(c, doel["punt"], thuis["punt"], terug, alleen_cache, tijd=a["eind"], aankomst=False)
            if m2 is not None:
                regels.append({"afspraak": a, "richting": "terug", "van": plek_naam(a), "naar": "thuis", "vervoer": terug,
                               "gekozen": bool(keuze), "minuten": m2 + marge, "geschat": g2, "start": a["eind"],
                               "eind": a["eind"] + dt.timedelta(minutes=m2 + marge), "past": True, "botst": "",
                               "sleutel": _sleutel_blok(a, "terug"), "ingepland": staat_er(a, "terug")})
    return {"fout": "", "regels": regels, "onbekend": onbekend, "thuis": thuis, "thuis_afspraken": thuis_afspraken}


# Icoon vooraan in de titel, zodat reistijd in elke agenda (ook op de telefoon) meteen herkenbaar is.
ICOON = {"ov": "🚆", "auto": "🚗", "fiets": "🚲", "lopen": "🚶"}


def titel_met_icoon(a):
    """De titel van een bestaand reistijdblok met het juiste icoon vooraan (vervoer staat in de omschrijving)."""
    m = re.search(r"Vervoer: (fiets|auto|ov|lopen)", a.get("omschrijving") or "")
    titel = re.sub(r"^[\U0001F680-\U0001F6FF\s]+", "", a.get("titel") or "").strip()
    return f"{ICOON[m.group(1)]} {titel}" if m else titel


def blok_titel(r):
    woord = {"fiets": "fietsen", "auto": "rijden", "ov": "OV", "lopen": "lopen"}[r["vervoer"]]
    if r["richting"] == "heen":
        return f"{ICOON[r['vervoer']]} Reistijd naar {r['naar']} ({woord})"
    return f"{ICOON[r['vervoer']]} Terugreis naar huis ({woord})"


def blok_omschrijving(r):
    return (f"{MARKER}{r['sleutel']}] Ingepland door Clara (Buurmanitor). Vervoer: {r['vervoer']}, {r['minuten']} min"
            + (" (schatting op basis van de autotijd)" if r.get("geschat") else f" ({dienst_naam()})") + ".")
