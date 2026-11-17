"""Regels voor binnenkomende mail: Bram verwerkt bepaalde mails meteen, zoals Martijn het heeft ingesteld.

Een regel heeft voorwaarden en acties. Bijvoorbeeld: "lijkt op een factuur (met bijlage)" -> gelezen en naar de map
Facturen. Elke vijf minuten, na het ophalen, kijkt Bram bij elke nieuwe mail welke regel past. De eerste regel die past
(in de volgorde op het scherm) wordt uitgevoerd; daarna kijkt Bram niet verder.

Voorwaarden (allemaal moeten kloppen; in één veld meerdere woorden met komma's: één ervan is genoeg):
- van: afzenderadres of naam bevat ...       - aan: ontvanger bevat ...
- onderwerp bevat ...                        - tekst: onderwerp of begin van de mail bevat ...
- bijlage: met of zonder bijlage             - naam van een bijlage bevat ... (bijvoorbeeld factuur, .pdf)
- lijkt op een factuur: een bijlage, en 'factuur', 'invoice', 'nota' en dergelijke in onderwerp of bijlagenaam
Acties: gelezen markeren, een label (Gmail) of categorie (Outlook) zetten, verplaatsen naar een map, archiveren, of
verwijderen (naar de prullenbak of Verwijderde items: nog 30 dagen terug te halen, nooit definitief).
Met de bijlagen: opslaan in een map in Google Drive (met {jaar}, {maand} of {afzender} in het pad, en desgewenst de
datum en afzender voor de bestandsnaam), en/of doorsturen naar een vast adres (zoals de inbox van je boekhouding).
Eventueel alleen bijlagen waarvan de naam iets bevat ('.pdf, factuur'). De bijlagen gaan eerst; lukt dat niet, dan
blijft de mail in de inbox staan en zie je het in het logboek.
Een taak maken: met een eigen titel ({afzender}, {onderwerp}, {bedrag}, {nummer}), prioriteit en deadline. Bij een
factuur zoekt Bram bedrag, vervaldatum, factuurnummer en betaallink in de mail en de pdf; de deadline wordt dan de
vervaldatum, de betaallink een knop op de taak en de in Drive opgeslagen factuur een link op de taak.
Een map of label staat op naam in de regel, zodat één regel voor Gmail en Outlook tegelijk kan gelden; bestaat de map
nog niet, dan maakt Bram hem aan als dat in de regel is aangevinkt.

Dit is iets wat Martijn zelf aanzet: zonder regel doet Bram niets uit zichzelf in de mailbox. Alles wat een regel deed,
staat in het logboek op het scherm Regels.
"""
import datetime as dt
import json
import re

from ..kern import config, meldingen
from . import betaling, bronnen, drive

VOORWAARDEN = ("van", "aan", "onderwerp", "tekst", "bijlage", "bijlage_naam", "factuur")
TEKSTVELDEN = ("van", "aan", "onderwerp", "tekst", "bijlage_naam")
FACTUUR = re.compile(r"factu(u)?r|invoice|\bnota\b|rekening|receipt|kwitantie|betaalbewijs|creditnota|\bbon\b|\binv[-_ ]?\d", re.I)
EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[a-z]{2,}$", re.I)
KLEIN_PLAATJE = 15_000  # zonder filter: kleinere afbeeldingen (logo's in een handtekening) niet opslaan of doorsturen
PROEF_MAX = 15  # zoveel bijlagenlijsten haalt een proef hooguit op (het scherm wacht erop)
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
         "maken": str(f.get("maken") or "") == "1", "verwijderen": str(f.get("verwijderen") or "") == "1",
         "drive": int(f.get("drive")) if str(f.get("drive") or "").isdigit() else 0,
         "drive_map": str(f.get("drive_map") or "").strip()[:400],
         "bestandsnaam": "datum" if str(f.get("bestandsnaam") or "") == "datum" else "",
         "bijlage_filter": str(f.get("bijlage_filter") or "").strip()[:200],
         "doorsturen": ", ".join(termen(f.get("doorsturen")))[:300],
         "taak": str(f.get("taak") or "") == "1", "taak_titel": str(f.get("taak_titel") or "").strip()[:160],
         "taak_prio": str(f.get("taak_prio") or "") if str(f.get("taak_prio") or "") in ("laag", "hoog") else "",
         "taak_dagen": int(f.get("taak_dagen")) if str(f.get("taak_dagen") or "").isdigit() and 0 < int(f.get("taak_dagen")) <= 365 else 0,
         "taak_betaling": str(f.get("taak_betaling") or "") == "1"}
    if a["verwijderen"]:  # naar de prullenbak: verplaatsen, archiveren en een label hebben dan geen zin meer
        a.update(map="", archiveren=False, label="", maken=False)
    if a["map"]:
        a["archiveren"] = False  # verplaatsen haalt hem al uit de inbox
    a = {k: w for k, w in a.items() if w}
    if not v:
        return v, a, "Geef minstens één voorwaarde, anders geldt de regel voor alle mail."
    if a.get("drive") and not a.get("drive_map"):
        return v, a, "Geef de map in Google Drive waar de bijlagen in moeten (bijvoorbeeld Administratie/Facturen/{jaar})."
    if a.get("drive_map") and not a.get("drive"):
        return v, a, "Kies het Google-account met de Drive waarin de bijlagen moeten."
    adressen = termen(a.get("doorsturen"))
    if any(not EMAIL.match(x) for x in adressen) or len(adressen) > 3:
        return v, a, "Doorsturen kan naar hooguit drie geldige mailadressen, met komma's ertussen."
    if not a.get("taak"):
        for k in ("taak_titel", "taak_prio", "taak_dagen", "taak_betaling"):
            a.pop(k, None)
    if not ({"gelezen", "archiveren", "map", "label", "drive", "doorsturen", "verwijderen", "taak"} & set(a)):
        return v, a, "Kies minstens één actie."
    if not ({"drive", "doorsturen"} & set(a)):
        for k in ("bestandsnaam", "bijlage_filter"):
            a.pop(k, None)
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
    welke = f'bijlagen met "{a["bijlage_filter"]}"' if a.get("bijlage_filter") else "bijlagen"
    if a.get("drive"):
        dan.append(f'{welke} opslaan in Drive: {a.get("drive_map")}' + (" (met datum en afzender)" if a.get("bestandsnaam") == "datum" else ""))
    if a.get("doorsturen"):
        dan.append(f'{welke if not a.get("drive") else "bijlagen"} doorsturen naar {a["doorsturen"]}')
    if a.get("taak"):
        dan.append("taak maken" + (f' "{a["taak_titel"]}"' if a.get("taak_titel") else "")
                   + (" (betaling: bedrag, vervaldatum en betaallink zoeken)" if a.get("taak_betaling") else ""))
    if a.get("verwijderen"):
        dan.append("verwijderen (naar de prullenbak)")
    return " en ".join(als), ", ".join(dan)


def actieve(c, koppeling_id=None):
    return c.execute("""SELECT * FROM post_regels WHERE actief AND (koppeling_id IS NULL OR %s::int IS NULL OR koppeling_id = %s)
                        ORDER BY volgorde, id""", (koppeling_id, koppeling_id)).fetchall()


def heeft_bijlage_acties(a):
    return bool((a or {}).get("drive") or (a or {}).get("doorsturen"))


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
    """Doe wat de regel zegt met mail b, in Gmail of Outlook. Geeft (korte uitslag, uit de inbox, link naar Drive).
    b is een rij of dict met id, extern_id en labels."""
    from . import sync  # pylint: disable=import-outside-toplevel
    a = regel["acties"] or {}
    g = k["soort"] == "google"
    tok = sync.toegang(c, k)
    # Eerst de bijlagen: lukt dat niet, dan blijft de mail staan waar hij staat (en kun je het later opnieuw proberen).
    bijlagen_gedaan, link, bestanden = bijlagen_verwerken(c, k, tok, b, regel) if heeft_bijlage_acties(a) else ("", "", [])
    taak_gedaan = taak_maken(c, k, tok, b, regel, bestanden) if a.get("taak") else ""
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
    gedaan = [x for x in (bijlagen_gedaan, taak_gedaan) if x]
    nieuw_id = None
    if g:
        erbij = [x for x in (label_id, map_id) if x]
        eraf = (["UNREAD"] if a.get("gelezen") else []) + (["INBOX"] if map_id or a.get("archiveren") else [])
        if erbij or eraf:
            bronnen.gmail_wijzig(tok, b["extern_id"], erbij=erbij, eraf=eraf)
        if a.get("verwijderen"):
            bronnen.gmail_verwijder(tok, b["extern_id"])
    else:
        if a.get("gelezen"):
            bronnen.ms_gelezen(tok, b["extern_id"])
        if label_id:
            bronnen.ms_label(tok, b["extern_id"], label_id, b.get("labels") or [])
        if map_id:
            nieuw_id = bronnen.ms_verplaats(tok, b["extern_id"], map_id)
        elif a.get("archiveren"):
            nieuw_id = bronnen.ms_archiveer(tok, b["extern_id"])
        if a.get("verwijderen"):
            nieuw_id = bronnen.ms_verwijder(tok, b["extern_id"])
    if a.get("gelezen"):
        gedaan.append("gelezen")
    if a.get("label"):
        gedaan.append(f"label {a['label']}")
    if map_id:
        gedaan.append(f"naar {a['map']}")
    elif a.get("archiveren"):
        gedaan.append("gearchiveerd")
    if a.get("verwijderen"):
        gedaan.append("naar de prullenbak")
    weg = bool(map_id or a.get("archiveren") or a.get("verwijderen"))
    c.execute("""UPDATE post_berichten SET gelezen = gelezen OR %s, status = CASE WHEN %s THEN 'weg' ELSE status END,
                    extern_id = coalesce(nullif(%s, ''), extern_id) WHERE id = %s""",
              (bool(a.get("gelezen")), weg, nieuw_id or "", b["id"]))
    return ", ".join(gedaan), weg, link


def bestandsnaam(naam, b, stijl):
    """De naam in Drive: zoals hij was, of met datum en afzender ervoor ('2026-11-15 KPN factuur.pdf'). Pure functie."""
    if stijl != "datum":
        return drive.schone_naam(naam)
    ontvangen = b.get("ontvangen") if isinstance(b.get("ontvangen"), dt.datetime) else dt.datetime.now(config.TZ)
    return drive.schone_naam(f"{ontvangen.astimezone(config.TZ):%Y-%m-%d} {afzender(b)} {naam}")


def afzender(b):
    """Een korte naam voor de afzender: de naam, anders het domein ('leverancier' uit een adres bij mail.leverancier.nl). Pure functie."""
    naam = (b.get("van_naam") or "").strip().strip('"')
    if naam and "@" not in naam:
        return drive.schone_naam(naam, 40)
    domein = (b.get("van_adres") or "").rsplit("@", 1)[-1].split(".")
    return drive.schone_naam(domein[-2] if len(domein) >= 2 else (b.get("van_adres") or "onbekend"), 40)


def kies_bijlagen(bijlagen, filter_tekst):
    """Welke bijlagen de regel meeneemt: met een filter alleen namen die iets ervan bevatten; zonder filter alles behalve
    kleine plaatjes (logo's in een handtekening). Pure functie."""
    lijst = termen(filter_tekst)
    if lijst:
        return [x for x in bijlagen if _bevat(x["naam"], lijst)]
    return [x for x in bijlagen if not ((x["soort"] or "").startswith("image/") and 0 < x["grootte"] < KLEIN_PLAATJE)]


def bijlagen_verwerken(c, k, tok, b, regel):
    """Opslaan in Drive en/of doorsturen. Geeft (wat er gebeurde, link naar de map in Drive)."""
    from . import sync  # pylint: disable=import-outside-toplevel
    a = regel["acties"] or {}
    alle = bronnen.gmail_bijlagen_data(tok, b["extern_id"]) if k["soort"] == "google" else bronnen.ms_bijlagen_data(tok, b["extern_id"])
    gekozen = kies_bijlagen(alle, a.get("bijlage_filter"))
    te_groot = [x["naam"] for x in gekozen if x["data"] is None]
    gekozen = [x for x in gekozen if x["data"] is not None]
    if not gekozen:
        return ("geen bijlagen om te verwerken" if not te_groot else f"bijlage te groot: {', '.join(te_groot)}"), "", []
    gedaan, link, bestanden = [], "", []
    if a.get("drive"):
        dk = c.execute("SELECT * FROM post_koppelingen WHERE id = %s", (a["drive"],)).fetchone()
        if not dk or dk["soort"] != "google":
            raise RegelFout("Het Google-account voor Drive uit deze regel is niet meer gekoppeld.")
        if not dk["drive"]:
            raise RegelFout(f"{dk['naam']} heeft nog geen toestemming voor Google Drive. Klik bij Post > Instellingen op 'Google Drive toestaan'.")
        dtok = tok if dk["id"] == k["id"] else sync.toegang(c, dk)
        ontvangen = b.get("ontvangen") if isinstance(b.get("ontvangen"), dt.datetime) else dt.datetime.now(config.TZ)
        pad = drive.invullen(a["drive_map"], {"jaar": f"{ontvangen.astimezone(config.TZ):%Y}", "maand": f"{ontvangen.astimezone(config.TZ):%m}",
                                              "afzender": afzender(b), "account": k["naam"]})
        mid = drive.map_id(dtok, dk["id"], pad)
        nieuw = al = 0
        for x in gekozen:
            naam = bestandsnaam(x["naam"], b, a.get("bestandsnaam"))
            fid = drive.bestaat(dtok, mid, naam)
            if fid:
                al += 1
            else:
                fid = drive.uploaden(dtok, mid, naam, x["soort"], x["data"])
                nieuw += 1
            if fid:
                bestanden.append({"naam": naam, "url": drive.bestand_link(fid)})
        gedaan.append(f"{nieuw} {'bijlage' if nieuw == 1 else 'bijlagen'} in Drive ({pad})" + (f", {al} stond(en) er al" if al else ""))
        link = drive.map_link(mid)
    if a.get("doorsturen"):
        al = c.execute("""SELECT 1 FROM post_regel_log WHERE regel_id = %s AND bericht_id = %s AND fout = '' AND wat LIKE '%%doorgestuurd%%'""",
                       (regel["id"], b["id"])).fetchone()
        if al:
            gedaan.append("al eerder doorgestuurd")
        else:
            ruw = bronnen.doorstuur_mime(k["account"] or k["naam"], termen(a["doorsturen"]), b, gekozen, regel["naam"])
            if k["soort"] == "google":
                bronnen.gmail_stuur_mime(tok, ruw)
            else:
                bronnen.ms_stuur_mime(tok, ruw)
            gedaan.append(f"{len(gekozen)} {'bijlage' if len(gekozen) == 1 else 'bijlagen'} doorgestuurd naar {a['doorsturen']}")
    if te_groot:
        gedaan.append(f"overgeslagen (te groot): {', '.join(te_groot)}")
    return ", ".join(gedaan), link, bestanden


def taak_titel(sjabloon, b, g, betaling_):
    """De titel van de taak: het sjabloon ingevuld, of een standaardtitel. Pure functie."""
    if not sjabloon:
        sjabloon = "Betalen: {afzender} {bedrag}" if betaling_ else "{onderwerp}"
    t = re.sub(r"\{(afzender|onderwerp|bedrag|nummer)\}", lambda m: {"afzender": afzender(b), "onderwerp": b.get("onderwerp") or "",
                                                                     "bedrag": g.get("bedrag") or "", "nummer": g.get("nummer") or ""}[m.group(1)],
               sjabloon)
    return re.sub(r"\s{2,}", " ", re.sub(r"\(\s*\)", "", t)).strip(" -:")[:160] or (b.get("onderwerp") or "Taak uit een mail")[:160]


def taak_maken(c, k, tok, b, regel, bestanden):
    """Een taak bij deze mail (één per mail). Bij een betaling: bedrag, vervaldatum, factuurnummer en betaallink uit de mail
    en de pdf; de deadline is de vervaldatum (anders de ingestelde termijn na ontvangst)."""
    from ..taken import web as taken_web  # pylint: disable=import-outside-toplevel
    a = regel["acties"] or {}
    if c.execute("SELECT 1 FROM takenbord WHERE bericht_id = %s", (b["id"],)).fetchone():
        return "taak bestond al"
    ontvangen = b.get("ontvangen") if isinstance(b.get("ontvangen"), dt.datetime) else dt.datetime.now(config.TZ)
    ontvangen_dag = ontvangen.astimezone(config.TZ).date()
    g = {}
    if a.get("taak_betaling"):
        m = (bronnen.gmail_volledig if k["soort"] == "google" else bronnen.ms_volledig)(tok, b["extern_id"])
        g = betaling.gegevens(m.get("tekst") or "", m.get("html") or "", [x.get("tekst") for x in m.get("bijlagen") or []], ontvangen_dag)
    dagen = a.get("taak_dagen") or (14 if a.get("taak_betaling") else 0)
    einddatum = g.get("vervaldatum") or (ontvangen_dag + dt.timedelta(days=dagen) if dagen else None)
    regels_ = [f"Uit de mail van {b.get('van_naam') or b.get('van_adres') or 'onbekend'}: {b.get('onderwerp') or ''}"]
    if a.get("taak_betaling"):
        regels_ += [f"Bedrag: {g['bedrag'] or 'niet gevonden, zie de factuur'}"]
        if g.get("nummer"):
            regels_.append(f"Factuurnummer: {g['nummer']}")
        regels_.append(f"Vervaldatum: {g['vervaldatum']:%d-%m-%Y} (uit de factuur)" if g.get("vervaldatum")
                       else f"Vervaldatum niet gevonden: deadline op {dagen} dagen na ontvangst gezet. Kijk het na op de factuur.")
        regels_.append(f"Betaallink: {g['link']}" if g.get("link") else "Geen betaallink gevonden: betaal via het rekeningnummer op de factuur.")
    if bestanden:
        regels_.append("Factuur in Drive: " + ", ".join(f"{x['naam']} ({x['url']})" for x in bestanden))
    regels_.append(f"Gemaakt door de regel '{regel['naam']}' in Post.")
    velden = {"titel": taak_titel(a.get("taak_titel"), b, g, a.get("taak_betaling")), "beschrijving": "\n".join(regels_),
              "prio": a.get("taak_prio") or "middel", "status": "nieuw", "wacht_op": "", "startdatum": None, "starttijd": None,
              "einddatum": einddatum, "eindtijd": None, "doel": "", "project_id": None}
    tid = taken_web.nieuw(c, velden, b["id"])
    c.execute("UPDATE takenbord SET bijlagen = %s, betaallink = %s WHERE id = %s",
              (json.dumps(bestanden, ensure_ascii=False), g.get("link") or "", tid))
    if einddatum:
        taken_web.agenda_op_achtergrond([tid])
    return "taak gemaakt" + (f" ({g['bedrag']}, uiterlijk {einddatum:%d-%m})" if a.get("taak_betaling") and g.get("bedrag") and einddatum else "")


def _log(c, regel, k, b, wat="", fout="", link=""):
    c.execute("""INSERT INTO post_regel_log (regel_id, koppeling_id, bericht_id, van, onderwerp, wat, fout, link)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
              (regel["id"], k["id"], b.get("id"), (b.get("van_naam") or b.get("van_adres") or "")[:200], (b.get("onderwerp") or "")[:300],
               wat[:300], fout[:500], link[:300]))
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
            wat, weg, link = uitvoeren(c, k, b, regel)
            _log(c, regel, k, b, wat, link=link)
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
            wat, _, link = uitvoeren(c, k, b, regel)
            _log(c, regel, k, b, wat, link=link)
            gelukt += 1
        except (bronnen.BronFout, RegelFout) as e:
            _log(c, regel, k, b, fout=str(e))
            if str(e) not in fouten:
                fouten.append(str(e))
    return gelukt, fouten
