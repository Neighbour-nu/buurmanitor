"""Regels voor binnenkomende mail: Bram verwerkt bepaalde mails meteen, zoals Martijn het heeft ingesteld.

Een regel heeft voorwaarden en acties. Bijvoorbeeld: "lijkt op een factuur (met bijlage)" -> gelezen en naar de map
Facturen. Elke vijf minuten, na het ophalen, kijkt Bram bij elke nieuwe mail welke regel past. De eerste regel die past
(in de volgorde op het scherm) wordt uitgevoerd; daarna kijkt Bram niet verder.

Voorwaarden (allemaal moeten kloppen; in één veld meerdere woorden met komma's: één ervan is genoeg):
- van: afzenderadres of naam bevat ...       - aan: ontvanger bevat ...
- onderwerp bevat ...                        - tekst: onderwerp of begin van de mail bevat ...
- bijlage: met of zonder bijlage             - naam van een bijlage bevat ... (bijvoorbeeld factuur, .pdf)
- lijkt op een factuur: een bijlage, en 'factuur', 'invoice', 'nota' en dergelijke in onderwerp of bijlagenaam
Acties: gelezen markeren, een label (Gmail) of categorie (Outlook) zetten, verplaatsen naar een map, archiveren.
Een map of label staat op naam in de regel, zodat één regel voor Gmail en Outlook tegelijk kan gelden; bestaat de map
nog niet, dan maakt Bram hem aan als dat in de regel is aangevinkt.

Dit is iets wat Martijn zelf aanzet: zonder regel doet Bram niets uit zichzelf in de mailbox. Alles wat een regel deed,
staat in het logboek op het scherm Regels.
"""
import datetime as dt
import json
import re

from ..kern import meldingen
from . import bronnen

VOORWAARDEN = ("van", "aan", "onderwerp", "tekst", "bijlage", "bijlage_naam", "factuur")
TEKSTVELDEN = ("van", "aan", "onderwerp", "tekst", "bijlage_naam")
FACTUUR = re.compile(r"factu(u)?r|invoice|\bnota\b|rekening|receipt|kwitantie|betaalbewijs|creditnota|\bbon\b|\binv[-_ ]?\d", re.I)
PROEF_MAX = 60  # zoveel bijlagenlijsten haalt een proef hooguit op
LOG_BEWAREN = 1000


# ---------------------------------------------------------------- regels lezen en beschrijven

def termen(tekst):
    """'factuur, invoice' -> ['factuur', 'invoice']. Pure functie."""
    return [t.strip().casefold() for t in str(tekst or "").split(",") if t.strip()]


def uit_formulier(f):
    """Voorwaarden en acties uit het formulier, gecontroleerd. Geeft (voorwaarden, acties, fout). Pure functie."""
    v = {k: str(f.get(k) or "").strip()[:300] for k in TEKSTVELDEN}
    v["bijlage"] = str(f.get("bijlage") or "") if str(f.get("bijlage") or "") in ("ja", "nee") else ""
    v["factuur"] = str(f.get("factuur") or "") == "1"
    v = {k: w for k, w in v.items() if w}
    a = {"gelezen": str(f.get("gelezen") or "") == "1", "archiveren": str(f.get("archiveren") or "") == "1",
         "map": str(f.get("map") or "").strip()[:200], "label": str(f.get("label") or "").strip()[:200],
         "maken": str(f.get("maken") or "") == "1"}
    if a["map"]:
        a["archiveren"] = False  # verplaatsen haalt hem al uit de inbox
    a = {k: w for k, w in a.items() if w}
    if not v:
        return v, a, "Geef minstens één voorwaarde, anders geldt de regel voor alle mail."
    if not ({"gelezen", "archiveren", "map", "label"} & set(a)):
        return v, a, "Kies minstens één actie."
    return v, a, ""


def beschrijf(v, a):
    """De regel in gewone woorden: (als, dan). Pure functie."""
    als = []
    for veld, naam in (("van", "van"), ("aan", "aan"), ("onderwerp", "onderwerp bevat"), ("tekst", "tekst bevat"),
                       ("bijlage_naam", "bijlage heet")):
        if v.get(veld):
            als.append(f"{naam} {' of '.join(repr(t) for t in termen(v[veld]))}".replace("'", '"'))
    if v.get("factuur"):
        als.append("lijkt op een factuur")
    if v.get("bijlage") == "ja":
        als.append("met bijlage")
    elif v.get("bijlage") == "nee":
        als.append("zonder bijlage")
    dan = []
    if a.get("gelezen"):
        dan.append("gelezen")
    if a.get("label"):
        dan.append(f'label "{a["label"]}"')
    if a.get("map"):
        dan.append(f'naar map "{a["map"]}"' + (" (maken als hij er niet is)" if a.get("maken") else ""))
    elif a.get("archiveren"):
        dan.append("archiveren")
    return " en ".join(als), ", ".join(dan)


def actieve(c, koppeling_id=None):
    return c.execute("""SELECT * FROM post_regels WHERE actief AND (koppeling_id IS NULL OR %s::int IS NULL OR koppeling_id = %s)
                        ORDER BY volgorde, id""", (koppeling_id, koppeling_id)).fetchall()


def heeft_bijlagen_nodig(regel):
    v = regel["voorwaarden"] or {}
    return bool(v.get("bijlage") or v.get("bijlage_naam") or v.get("factuur"))


# ---------------------------------------------------------------- passen

def _bevat(tekst, lijst):
    t = (tekst or "").casefold()
    return any(x in t for x in lijst)


def past(v, b):
    """Past de mail b bij voorwaarden v? True, False, of None als dat afhangt van de bijlagen en die nog niet bekend
    zijn. Pure functie."""
    if v.get("van") and not _bevat(f"{b.get('van_adres', '')} {b.get('van_naam', '')}", termen(v["van"])):
        return False
    if v.get("aan") and not _bevat(b.get("aan"), termen(v["aan"])):
        return False
    if v.get("onderwerp") and not _bevat(b.get("onderwerp"), termen(v["onderwerp"])):
        return False
    if v.get("tekst") and not _bevat(f"{b.get('onderwerp', '')} {b.get('fragment', '')} {b.get('tekst', '')}", termen(v["tekst"])):
        return False
    if not (v.get("bijlage") or v.get("bijlage_naam") or v.get("factuur")):
        return True
    namen = b.get("bijlagen")
    if namen is None:
        return None
    if v.get("bijlage") == "ja" and not namen:
        return False
    if v.get("bijlage") == "nee" and namen:
        return False
    if v.get("bijlage_naam") and not any(_bevat(n, termen(v["bijlage_naam"])) for n in namen):
        return False
    if v.get("factuur"):
        if not namen:
            return False
        if not (FACTUUR.search(b.get("onderwerp") or "") or any(FACTUUR.search(n) for n in namen)):
            return False
    return True


def kies(regels, b):
    """De eerste regel die past, of None. Geeft ('?', regel) als een regel afhangt van onbekende bijlagen. Pure functie."""
    for r in regels:
        if r["koppeling_id"] and r["koppeling_id"] != b.get("koppeling_id"):
            continue
        uitslag = past(r["voorwaarden"] or {}, b)
        if uitslag is None:
            return "?", r
        if uitslag:
            return "ja", r
    return None, None


def zoek_naam(lijst, naam):
    """Een map of label op naam: precies (hoofdletters maken niet uit), anders op het laatste deel van het pad
    ('Administratie/Facturen', 'Postvak IN / Facturen'). Geeft het id of None. Pure functie."""
    def norm(x):
        return re.sub(r"\s*/\s*", "/", (x or "").strip()).casefold()
    doel = norm(naam)
    for i, n in lijst:
        if norm(n) == doel:
            return i
    eind = [i for i, n in lijst if norm(n).rsplit("/", 1)[-1] == doel.rsplit("/", 1)[-1]]
    return eind[0] if len(eind) == 1 else None


# ---------------------------------------------------------------- uitvoeren

class RegelFout(Exception):
    pass


def uitvoeren(c, k, b, regel):
    """Doe wat de regel zegt met mail b, in Gmail of Outlook. Geeft een korte uitslag. b is een rij of dict met id,
    extern_id en labels."""
    from . import sync  # pylint: disable=import-outside-toplevel
    a = regel["acties"] or {}
    g = k["soort"] == "google"
    tok = sync.toegang(c, k)
    map_id = label_id = None
    if a.get("map"):
        map_id = zoek_naam(sync.mappen(c, k), a["map"]) or zoek_naam(sync.mappen(c, k, vers=True), a["map"])
        if not map_id:
            if not a.get("maken"):
                raise RegelFout(f'De map "{a["map"]}" bestaat niet in {k["naam"]}.')
            map_id = bronnen.gmail_label_maken(tok, a["map"]) if g else bronnen.ms_map_maken(tok, a["map"])
            sync.vergeet_mappen(k["id"])
    if a.get("label"):
        if g:
            label_id = zoek_naam(sync.labels(c, k), a["label"])
            if not label_id:
                label_id = bronnen.gmail_label_maken(tok, a["label"])
                sync.vergeet_mappen(k["id"])
        else:
            label_id = a["label"]  # een categorie in Outlook is gewoon een naam
    gedaan = []
    nieuw_id = None
    if g:
        erbij = [x for x in (label_id, map_id) if x]
        eraf = (["UNREAD"] if a.get("gelezen") else []) + (["INBOX"] if map_id or a.get("archiveren") else [])
        bronnen.gmail_wijzig(tok, b["extern_id"], erbij=erbij, eraf=eraf)
    else:
        if a.get("gelezen"):
            bronnen.ms_gelezen(tok, b["extern_id"])
        if label_id:
            bronnen.ms_label(tok, b["extern_id"], label_id, b.get("labels") or [])
        if map_id:
            nieuw_id = bronnen.ms_verplaats(tok, b["extern_id"], map_id)
        elif a.get("archiveren"):
            nieuw_id = bronnen.ms_archiveer(tok, b["extern_id"])
    if a.get("gelezen"):
        gedaan.append("gelezen")
    if a.get("label"):
        gedaan.append(f"label {a['label']}")
    if map_id:
        gedaan.append(f"naar {a['map']}")
    elif a.get("archiveren"):
        gedaan.append("gearchiveerd")
    weg = bool(map_id or a.get("archiveren"))
    c.execute("""UPDATE post_berichten SET gelezen = gelezen OR %s, status = CASE WHEN %s THEN 'weg' ELSE status END,
                    extern_id = coalesce(nullif(%s, ''), extern_id) WHERE id = %s""",
              (bool(a.get("gelezen")), weg, nieuw_id or "", b["id"]))
    return ", ".join(gedaan), weg


def _log(c, regel, k, b, wat="", fout=""):
    c.execute("""INSERT INTO post_regel_log (regel_id, koppeling_id, van, onderwerp, wat, fout) VALUES (%s, %s, %s, %s, %s, %s)""",
              (regel["id"], k["id"], (b.get("van_naam") or b.get("van_adres") or "")[:200], (b.get("onderwerp") or "")[:300],
               wat[:300], fout[:500]))
    if not fout:
        c.execute("UPDATE post_regels SET aantal = aantal + 1, laatst = now() WHERE id = %s", (regel["id"],))


def _bijlagen_ophalen(c, k, b):
    from . import sync  # pylint: disable=import-outside-toplevel
    tok = sync.toegang(c, k)
    namen = bronnen.gmail_bijlage_namen(tok, b["extern_id"]) if k["soort"] == "google" else bronnen.ms_bijlage_namen(tok, b["extern_id"])
    c.execute("UPDATE post_berichten SET bijlagen = %s::jsonb WHERE id = %s", (json.dumps(namen), b["id"]))
    return namen


def toepassen(c, k, berichten, regels=None, nu=None):
    """De regels toepassen op (nieuwe) berichten van account k. Geeft de berichten die nog in de inbox staan
    (voor het sorteren en de meldingen) en het aantal dat een regel kreeg."""
    nu = nu or dt.datetime.now()
    regels = regels if regels is not None else actieve(c, k["id"])
    if not regels:
        return berichten, 0
    over, aantal = [], 0
    for b in berichten:
        b = dict(b) | {"koppeling_id": k["id"]}
        uitslag, regel = kies(regels, b)
        if uitslag == "?":
            try:
                b["bijlagen"] = _bijlagen_ophalen(c, k, b)
            except bronnen.BronFout:
                b["bijlagen"] = []
            uitslag, regel = kies(regels, b)
        if uitslag != "ja":
            over.append(b)
            continue
        try:
            wat, weg = uitvoeren(c, k, b, regel)
            _log(c, regel, k, b, wat)
            aantal += 1
            if not weg:
                over.append(b)
        except (bronnen.BronFout, RegelFout) as e:
            _log(c, regel, k, b, fout=str(e))
            meldingen.melden(c, "normaal", "bram", f"Regel '{regel['naam']}' lukte niet", f"{e} Mail: {b.get('onderwerp', '')}",
                             "/bram/regels", sleutel=f"bram-regel-{regel['id']}-{k['id']}-{nu:%Y-%m-%d}")
            over.append(b)
    c.execute("DELETE FROM post_regel_log WHERE id < (SELECT coalesce(max(id), 0) - %s FROM post_regel_log)", (LOG_BEWAREN,))
    return over, aantal


def proef(c, regel, met_ophalen=True):
    """Welke mails in de inbox nu aan deze regel voldoen (voor 'Probeer' en 'Nu toepassen'). Haalt zo nodig de
    namen van bijlagen op (hooguit PROEF_MAX). Geeft (passend, onbekend: aantal dat niet beoordeeld kon worden)."""
    from . import sync  # pylint: disable=import-outside-toplevel
    rijen = c.execute("""SELECT b.*, k.naam AS koppeling FROM post_berichten b JOIN post_koppelingen k ON k.id = b.koppeling_id
                         WHERE b.status = 'open' AND k.actief AND (%s::int IS NULL OR b.koppeling_id = %s)
                         ORDER BY b.ontvangen DESC""", (regel["koppeling_id"], regel["koppeling_id"])).fetchall()
    ks = {k["id"]: k for k in sync.koppelingen(c, alleen_actief=True)}
    passend, onbekend, opgehaald = [], 0, 0
    for b in rijen:
        uitslag = past(regel["voorwaarden"] or {}, b)
        if uitslag is None and met_ophalen and opgehaald < PROEF_MAX:
            opgehaald += 1
            try:
                b = dict(b) | {"bijlagen": _bijlagen_ophalen(c, ks[b["koppeling_id"]], b)}
                uitslag = past(regel["voorwaarden"] or {}, b)
            except (bronnen.BronFout, KeyError):
                uitslag = None
        if uitslag is None:
            onbekend += 1
        elif uitslag:
            passend.append(b)
    return passend, onbekend


def nu_toepassen(c, regel, ids):
    """De regel uitvoeren op deze berichten uit de inbox (na 'Probeer'). Geeft (gelukt, fouten)."""
    from . import sync  # pylint: disable=import-outside-toplevel
    ks = {k["id"]: k for k in sync.koppelingen(c, alleen_actief=True)}
    gelukt, fouten = 0, []
    for b in c.execute("SELECT * FROM post_berichten WHERE id = ANY(%s) AND status = 'open'", (list(ids),)).fetchall():
        k = ks.get(b["koppeling_id"])
        if not k or past(regel["voorwaarden"] or {}, b) is False:
            continue
        try:
            wat, _ = uitvoeren(c, k, b, regel)
            _log(c, regel, k, b, wat)
            gelukt += 1
        except (bronnen.BronFout, RegelFout) as e:
            _log(c, regel, k, b, fout=str(e))
            if str(e) not in fouten:
                fouten.append(str(e))
    return gelukt, fouten
