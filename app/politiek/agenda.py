"""Agenda (M3): vergadercyclus, woordvoering en mijn agendapunten met stappenplan en deadlines.

De vergaderingen en agendapunten komen uit Notubiz, de woordvoerders uit de toewijzingen die op het
scherm Whaller worden vastgelegd. De voortgang per agendapunt staat in agenda_voortgang en hangt aan
het onderwerp, zodat een punt dat eerst in de commissie en daarna in de raad komt één stappenplan houdt.
"""
import datetime as dt
import difflib
import re
import threading
import time

from . import notubiz, whaller

STAPPEN = [
    ("stukken", "Stukken binnen"),
    ("analyse", "Eerste analyse"),
    ("technisch", "Technische vragen ingediend"),
    ("standpunt", "Standpunt gekozen in de fractie"),
    ("bijdrage", "Bijdrage klaar en geoefend"),
    ("motie", "Motie of amendement (als nodig)"),
    ("stemverklaring", "Stemverklaring en terugblik"),
]
STAP_NAAM = dict(STAPPEN)
# Welke stappen bij welke vergadering horen.
PER_SOORT = {
    "commissie": ["stukken", "analyse", "technisch", "standpunt", "bijdrage"],
    "raad": ["stukken", "analyse", "standpunt", "bijdrage", "motie", "stemverklaring"],
}
# Welke soort concept bij welke stap hoort (een concept is nog niet 'gedaan', wel een hulp).
CONCEPT_STAP = {"analyse": "analyse", "technische_vraag": "technisch", "vraag_agendapunt": "technisch",
                "bijdrage": "bijdrage", "amendement": "motie", "motie": "motie", "stemverklaring": "stemverklaring"}

# Formele punten (benoemingen, beëdigingen en dergelijke) horen niet in onze agenda en stappenplannen.
FORMEEL = re.compile(r"\b(benoem\w*|benoeming\w*|be[eë]dig\w*|toelating\w*|installatie|afscheid|ontslag\w*|"
                     r"geloofsbrieven|vacature\w*|verkiezing van|herbenoem\w*)\b", re.I)


def is_formeel(onderwerp):
    return bool(FORMEEL.search(onderwerp or ""))


_CACHE = {}
_SLOT = threading.Lock()
BEWAARTIJD = 600  # seconden; Notubiz is traag en de agenda verandert niet elke minuut


def _bewaard(sleutel, ophalen):
    with _SLOT:
        oud = _CACHE.get(sleutel)
        if oud and time.time() - oud[0] < BEWAARTIJD:
            return oud[1]
    waarde = ophalen()
    with _SLOT:
        _CACHE[sleutel] = (time.time(), waarde)
    return waarde


def leeg_cache():
    with _SLOT:
        _CACHE.clear()


def _dag_voor(datum, dagnaam):
    """De laatste dag met deze naam vóór datum (bijvoorbeeld de maandag vóór een dinsdag)."""
    doel = whaller.DAGEN.index(dagnaam) if dagnaam in whaller.DAGEN else 0
    stap = (datum.weekday() - doel) % 7 or 7
    return datum - dt.timedelta(days=stap)


def deadlines(soort, datum, inst):
    """Deadlines per stap voor een agendapunt in een vergadering op datum."""
    uit = {}
    if soort == "commissie":
        try:
            dagen = int(str(inst.get("agenda_technische_vragen_dagen") or "7").strip())
        except ValueError:
            dagen = 7
        if dagen > 0:
            uit["technisch"] = datum - dt.timedelta(days=dagen)
    uit["standpunt"] = _dag_voor(datum, (inst.get("fractie_dag") or "maandag").lower())
    uit["bijdrage"] = _dag_voor(datum, "zondag")
    # Motie, amendement en stemverklaring zijn 'als nodig' en horen bij de vergadering zelf: geen aparte deadline.
    return uit


def _vergadering(ev):
    """Notubiz-gegevens van één vergadering (bewaard)."""
    if ev["soort"] == "commissie" and ev["type"] == "assembly":
        return _bewaard(("avond", ev["id"]), lambda: notubiz.commissieavond(ev["id"]))
    if ev["soort"] == "raad" and ev["type"] == "meeting":
        return _bewaard(("raad", ev["id"]), lambda: notubiz.vergadering(ev["id"]))
    return None


def _voortgang_van(onderwerp, voortgang):
    k = whaller.sleutel(onderwerp)
    if k in voortgang:
        return voortgang[k]
    beste = difflib.get_close_matches(k, list(voortgang), n=1, cutoff=0.82)
    return voortgang[beste[0]] if beste else None


def _stam(w):
    return w[:7] if len(w) > 7 else w


def voorstel(onderwerp, personen, dossiers, alleen=None):
    """Voorstel voor een woordvoerder als er nog niemand is: eerst het dossier, dan de portefeuille.
    alleen: verzameling Whaller-namen die in aanmerking komen (bij de raad alleen raadsleden).
    Geeft (whaller-naam, reden) of ("", "")."""
    tekst = (onderwerp or "").lower()
    kandidaten = [p for p in personen if p.get("whaller") and (alleen is None or p["whaller"] in alleen)]
    for d in dossiers:
        termen = [t.strip().lower() for t in re.split(r"[,\n;]+", d.get("zoektermen") or "") if len(t.strip()) > 3]
        if d.get("woordvoerder") and (d["naam"].lower() in tekst or any(t in tekst for t in termen)):
            p = next((p for p in kandidaten if p["naam"] == d["woordvoerder"]), None)
            if p:
                return p["whaller"], f"dossier {d['naam']}"
    for p in kandidaten:
        for w in re.split(r"[,;]+", p.get("portefeuilles") or ""):
            w = w.strip().lower()
            if len(w) > 3 and _stam(w) in tekst:
                return p["whaller"], f"portefeuille {w}"
    return "", ""


def overzicht(c, van, tot, inst, personen):
    """Alle vergaderingen tussen van en tot, met agendapunten, woordvoering, voortgang en deadlines.

    c: databaseverbinding (dict_row). personen: rijen met naam, whaller, rol.
    Geeft (vergaderingen, fouten) terug.
    """
    toew = c.execute("SELECT * FROM toewijzingen").fetchall()
    dossiers = c.execute("SELECT naam, zoektermen, woordvoerder FROM dossiers WHERE status = 'actief'").fetchall()
    if personen and "portefeuilles" not in personen[0]:
        personen = c.execute("""SELECT naam, whaller, rol, portefeuilles FROM personen
                                WHERE actief AND coalesce(whaller, '') <> '' ORDER BY volgorde, naam""").fetchall()
    raadsleden = {p["whaller"] for p in personen if (p["rol"] or "").startswith("raadslid")}
    namen = {p["whaller"]: p["naam"] for p in personen}
    voortgang = {r["sleutel"]: r for r in c.execute("SELECT * FROM agenda_voortgang").fetchall()}
    besloten = {r["extern_id"] for r in c.execute("SELECT extern_id FROM stukken WHERE soort = 'besloten'").fetchall()}
    concepten = c.execute("""SELECT id, soort, titel, status, bron->>'punt_id' AS punt_id, bron->>'onderwerp' AS onderwerp
                             FROM concepten WHERE status <> 'fout' ORDER BY id""").fetchall()
    evenementen = sorted(_bewaard(("ev", van, tot), lambda: notubiz.evenementen(van, tot)),
                         key=lambda e: e["start"] or dt.datetime.max)
    vergaderingen, fouten = [], []
    for ev in evenementen:
        v = {**ev, "alleen_leden": ev.get("alleen_leden") or f"leden-{ev['id']}" in besloten, "punten": []}
        vergaderingen.append(v)
        if v["alleen_leden"]:
            continue
        try:
            gegevens = _vergadering(ev)
        except Exception as e:  # noqa: BLE001 - één vergadering mag de rest niet tegenhouden
            fouten.append(f"{ev['naam']} {ev['start']:%d-%m}: {e}")
            continue
        if not gegevens:
            continue
        datum = gegevens["start"].date()
        if ev["soort"] == "commissie":
            punten = [{**p, "soort": "commissie"} for p in whaller.commissie_punten(gegevens, toew)]
        else:
            ind = whaller.raad_indeling(gegevens, toew, raadsleden)
            punten = []
            for rubriek in ("bijzonder", "bespreek", "hamer"):
                for p in ind[rubriek]:
                    if rubriek == "bijzonder" and not p.get("woordvoerder"):
                        continue  # zoals benoemingen: die kiezen we per raad in het Whaller-bericht
                    punten.append({**p, "soort": "raad", "rubriek": rubriek, "zit_erbij": "",
                                   "woordvoerder": p.get("woordvoerder", "") if rubriek == "bespreek" else "",
                                   "bron": p.get("bron", "") if rubriek == "bespreek" else ""})
        punten = [p for p in punten if not is_formeel(p["onderwerp"])]
        for p in punten:
            # vast: op deze vergadering vastgelegd; anders een voorstel (eerdere vergadering, dossier of portefeuille).
            if p["soort"] == "commissie":
                p["vast"] = p.get("bron") == "vastgelegd"
            else:
                p["vast"] = p.get("bron") == "raad"
            if p["soort"] == "raad" and p.get("rubriek") != "bespreek":
                continue
            if not p.get("woordvoerder"):
                wv, reden = voorstel(p["onderwerp"], personen, dossiers, raadsleden if p["soort"] == "raad" else None)
                p["voorstel_wv"], p["voorstel"] = wv, reden  # pas na opslaan echt toegewezen
            elif not p["vast"]:
                p["voorstel"] = "eerdere vergadering" if p["soort"] == "commissie" else "commissie"
        dl_basis = deadlines(ev["soort"], datum, inst)
        for p in punten:
            stappen = PER_SOORT[p["soort"]]
            vg = _voortgang_van(p["onderwerp"], voortgang)
            gedaan = set((vg or {}).get("gedaan") or [])
            if p.get("documenten"):
                gedaan.add("stukken")
            eigen = [k for k in concepten if (k["punt_id"] and str(k["punt_id"]) == str(p.get("id")))
                     or (k["onderwerp"] and whaller.sleutel(k["onderwerp"]) == whaller.sleutel(p["onderwerp"]))]
            hulp = {CONCEPT_STAP[k["soort"]] for k in eigen if k["soort"] in CONCEPT_STAP and k["status"] == "klaar"}
            if "analyse" in hulp:
                gedaan.add("analyse")
            p.update({
                "datum": datum, "vergadering": v["naam"], "stappen": stappen, "gedaan": gedaan,
                "nvt": set((vg or {}).get("nvt") or []), "concepten": eigen, "hulp": hulp,
                "deadlines": {k: d for k, d in dl_basis.items() if k in stappen},
                "notitie": (vg or {}).get("notitie") or "",
                "namen": [namen.get(x, x) for x in (p.get("woordvoerder"), p.get("zit_erbij")) if x],
                "sleutel": whaller.sleutel(p["onderwerp"]),
            })
            p["open"] = [s for s in stappen if s not in p["gedaan"] and s not in p["nvt"]]
            p["klaar"] = not p["open"]
        v["punten"] = punten
        v["datum"] = datum
    return vergaderingen, fouten


def van_mij(punt, ik):
    return bool(ik) and ik in (punt.get("woordvoerder"), punt.get("zit_erbij"))


def mijn_deadlines(vergaderingen, ik, vandaag=None, dagen=2):
    """Open stappen van mijn agendapunten met een deadline tot en met vandaag + dagen (ook verlopen)."""
    vandaag = vandaag or dt.date.today()
    uit = []
    for v in vergaderingen:
        for p in v["punten"]:
            if not van_mij(p, ik):
                continue
            for stap in p["open"]:
                d = p["deadlines"].get(stap)
                if d and d <= vandaag + dt.timedelta(days=dagen) and p["datum"] >= vandaag:
                    uit.append({"datum": d, "stap": stap, "naam": STAP_NAAM[stap], "punt": p,
                                "verlopen": d < vandaag})
    return sorted(uit, key=lambda x: (x["datum"], x["punt"]["datum"]))


def persoon_bij_naam(naam, personen):
    """Een fractielid bij een naam uit de notulen (volledige naam, voornaam of Whaller-naam)."""
    n = (naam or "").strip().lower()
    if not n:
        return None
    for p in personen:
        if n in (p["naam"].lower(), (p["whaller"] or "").lower()):
            return p
    voornamen = [p for p in personen if p["naam"].split()[0].lower() == n.split()[0]]
    return voornamen[0] if len(voornamen) == 1 else None


def zoek_punt(onderwerp, vergadering, vergaderingen):
    """Het agendapunt in de komende vergaderingen dat het best past bij een regel uit de notulen."""
    k = whaller.sleutel(onderwerp)
    if not k:
        return None
    soort = "raad" if "raad" in (vergadering or "").lower() else "commissie" if "commissie" in (vergadering or "").lower() else None
    kandidaten = [p for v in vergaderingen for p in v["punten"]
                  if (soort is None or p["soort"] == soort) and (p["soort"] == "commissie" or p.get("rubriek") == "bespreek")]
    beste, score = None, 0.0
    for p in kandidaten:
        pk = p["sleutel"]
        r = 1.0 if min(len(k), len(pk)) >= 12 and (k in pk or pk in k) else difflib.SequenceMatcher(None, k, pk).ratio()
        if r > score:
            beste, score = p, r
    return beste if score >= 0.75 else None
