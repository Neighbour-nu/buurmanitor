"""Wat Clara in je agenda ziet. Alleen rekenwerk, zonder database of netwerk, zodat het goed te testen is.

Een afspraak is hier een dict met minstens start, eind (tijdzonebewust), heledag, status, vrij, antwoord, titel,
locatie, online, ical_uid, soort, koppeling en domein.
"""
import datetime as dt
import re

KWARTIER = dt.timedelta(minutes=15)
STANDAARD = {
    "werkdagen": [0, 1, 2, 3, 4],
    "begin": "08:30",
    "eind": "17:30",
    "avond": False,           # ook 19:00 tot 22:00 als vrije tijd zoeken (politiek, ZEROHERO)
    "buffer": 10,             # minuten lucht rond afspraken bij het zoeken naar vrije tijd
    "reistijd": 30,           # minuten tussen twee afspraken op verschillende plekken
}


def verborgen(a, regels):
    """Valt deze afspraak onder een verbergregel? Een regel heeft 'organisator' en/of 'tekst' (titel of omschrijving);
    beide zijn 'bevat', zonder verschil tussen hoofd- en kleine letters. Lege velden tellen niet mee."""
    org = (a.get("organisator") or "").lower()
    tekst = f"{a.get('titel') or ''} {a.get('omschrijving') or ''}".lower()
    for r in regels or []:
        o, t = (r.get("organisator") or "").strip().lower(), (r.get("tekst") or "").strip().lower()
        if (o or t) and (not o or o in org) and (not t or t in tekst):
            return True
    return False


def bezet(a):
    """Telt deze afspraak mee als bezette tijd?"""
    return (not a.get("heledag") and a.get("status") != "geannuleerd" and not a.get("vrij")
            and a.get("antwoord") != "afgewezen")


def _plek(a):
    p = re.sub(r"\s+", " ", (a.get("locatie") or "").lower()).strip()
    return "" if a.get("online") else p


def samenvoegen(afspraken):
    """Dezelfde afspraak in meer agenda's (je bent op twee adressen uitgenodigd) wordt één regel, met 'ook in'.
    Een raadsvergadering uit Notubiz valt weg als dezelfde vergadering al in een andere agenda staat."""
    gezien, uit = {}, []
    for a in sorted(afspraken, key=lambda x: (x["start"], x.get("soort") == "raad", x.get("antwoord") != "organisator")):
        sleutel = (a.get("ical_uid") or "").lower()
        if sleutel:
            k = (sleutel, a["start"])
            if k in gezien:
                eerder = gezien[k]
                naam = a.get("koppeling") or ""
                if naam and naam != eerder.get("koppeling") and naam not in eerder["ook"]:
                    eerder["ook"].append(naam)
                continue
        a = dict(a, ook=[])
        if sleutel:
            gezien[(sleutel, a["start"])] = a
        uit.append(a)
    andere = [a for a in uit if a.get("soort") != "raad" and bezet(a)]
    return [a for a in uit if a.get("soort") != "raad" or not any(
        abs((b["start"] - a["start"]).total_seconds()) <= 45 * 60 for b in andere if b.get("domein") == "politiek")]


def conflicten(afspraken):
    """Dubbele boekingen: twee bezette afspraken die elkaar overlappen."""
    lijst = sorted((a for a in afspraken if bezet(a)), key=lambda x: x["start"])
    uit = []
    for i, a in enumerate(lijst):
        for b in lijst[i + 1:]:
            if b["start"] >= a["eind"]:
                break
            if b["eind"] > a["start"] and b["start"] < a["eind"]:
                uit.append({"a": a, "b": b, "van": max(a["start"], b["start"]), "tot": min(a["eind"], b["eind"])})
    return uit


def overgangen(afspraken, reistijd=STANDAARD["reistijd"]):
    """Krappe overgangen: twee afspraken op verschillende plekken met minder dan de reistijd ertussen."""
    lijst = sorted((a for a in afspraken if bezet(a) and _plek(a)), key=lambda x: x["start"])
    uit = []
    for vorige, volgende in zip(lijst, lijst[1:]):
        if vorige["eind"].date() != volgende["start"].date():
            continue
        tussen = (volgende["start"] - vorige["eind"]).total_seconds() / 60
        p1, p2 = _plek(vorige), _plek(volgende)
        if 0 <= tussen < reistijd and p1 != p2 and p1 not in p2 and p2 not in p1:
            uit.append({"van": vorige, "naar": volgende, "minuten": int(tussen)})
    return uit


def uitnodigingen(afspraken, nu):
    """Uitnodigingen waar je nog niet op hebt gereageerd."""
    return sorted((a for a in afspraken if a.get("antwoord") == "geen" and a["eind"] >= nu
                   and a.get("status") != "geannuleerd"), key=lambda x: x["start"])


def _tijd(tekst, standaard):
    try:
        u, m = str(tekst).split(":")
        return dt.time(int(u), int(m))
    except (ValueError, TypeError):
        return standaard


def werkvensters(dag, inst, tz):
    """De tijdvakken op een dag waarin Clara vrije tijd zoekt."""
    inst = {**STANDAARD, **(inst or {})}
    uit = []
    if dag.weekday() in inst["werkdagen"]:
        uit.append((dt.datetime.combine(dag, _tijd(inst["begin"], dt.time(8, 30)), tzinfo=tz),
                    dt.datetime.combine(dag, _tijd(inst["eind"], dt.time(17, 30)), tzinfo=tz)))
    if inst.get("avond"):
        uit.append((dt.datetime.combine(dag, dt.time(19), tzinfo=tz), dt.datetime.combine(dag, dt.time(22), tzinfo=tz)))
    return uit


def _naar_kwartier(t):
    rest = (t.minute % 15) * 60 + t.second + t.microsecond / 1e6
    return t if rest == 0 else t + dt.timedelta(seconds=15 * 60 - rest)


def vrije_blokken(afspraken, van, dagen, duur_minuten, inst=None, tz=None, max_aantal=8):
    """Vrije blokken van ten minste duur_minuten, vanaf 'van', binnen de werkvensters, met lucht rond afspraken.
    Per dag hoogstens twee voorstellen, zodat je ook keuze krijgt over dagen heen."""
    inst = {**STANDAARD, **(inst or {})}
    tz = tz or van.tzinfo
    duur, lucht = dt.timedelta(minutes=duur_minuten), dt.timedelta(minutes=int(inst["buffer"]))
    bezette = sorted(((a["start"] - lucht, a["eind"] + lucht) for a in afspraken if bezet(a)), key=lambda x: x[0])
    uit = []
    for d in range(dagen):
        dag = (van + dt.timedelta(days=d)).astimezone(tz).date()
        per_dag = 0
        for begin, eind in werkvensters(dag, inst, tz):
            t = _naar_kwartier(max(begin, van))
            for b_start, b_eind in bezette:
                if b_eind <= t or b_start >= eind:
                    continue
                if b_start - t >= duur and per_dag < 2:
                    uit.append({"start": t, "eind": t + duur, "ruimte": int((min(b_start, eind) - t).total_seconds() // 60)})
                    per_dag += 1
                t = _naar_kwartier(max(t, b_eind))
            if eind - t >= duur and per_dag < 2:
                uit.append({"start": t, "eind": t + duur, "ruimte": int((eind - t).total_seconds() // 60)})
                per_dag += 1
            if len(uit) >= max_aantal:
                return uit
    return uit


def per_dag(afspraken, tz):
    """Afspraken gegroepeerd per dag (hele dagen eerst), voor het weekoverzicht."""
    dagen = {}
    for a in afspraken:
        d = a["start"].astimezone(tz).date()
        dagen.setdefault(d, []).append(a)
        if a.get("heledag"):  # meerdaagse afspraken ook op de volgende dagen
            dag = d + dt.timedelta(days=1)
            while dt.datetime.combine(dag, dt.time(0), tzinfo=tz) < a["eind"]:
                dagen.setdefault(dag, []).append(a)
                dag += dt.timedelta(days=1)
    return {d: sorted(v, key=lambda a: (not a.get("heledag"), a["start"])) for d, v in sorted(dagen.items())}
