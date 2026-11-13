"""Vera, de nieuwsredactie. Vier stromen op één plek, in drie categorieën:

- Keepersnieuws: uit de Doellijn (werkruimte ZEROHERO, tabel items), daar al gewogen en samengevat.
- Politiek, lokaal en regionaal: uit Promonitor (werkruimte Politiek, tabel stukken met bron 'nieuws'), daar al gewogen.
  Dit is het belangrijkste politieke nieuws, voor het raadswerk.
- Politiek, landelijk: Vera haalt het zelf op uit haar eigen bronnen (stroom 'landelijk') en weegt het vanuit het
  raadswerk: wat er in Den Haag speelt en wat dat voor gemeenten betekent.
- Overig nieuws: ook uit Vera's eigen bronnen (stroom 'overig').
Vera weegt haar eigen berichten met Claude (licht model, met een dagplafond). Zonder Claude-sleutel staat het nieuwste bovenaan.

Vera leest de werkruimtes alleen; ze verandert daar niets. Wat je aanklikt, onthoudt ze zelf (vera_gelezen).
"""
import datetime as dt
import difflib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

from ..kern import claude, config, planner
from ..kern import tekst as kern_tekst

UA = "Mozilla/5.0 (compatible; Buurmanitor; Vera)"
GOOGLE = "https://news.google.com/rss/search?hl=nl&gl=NL&ceid=NL:nl&q="
DAGPLAFOND_EUR = float(os.environ.get("VERA_DAGPLAFOND_EUR", "0.30"))
BEWAAR_DAGEN = 30

SOORTEN = (("keepers", "Keepersnieuws"), ("politiek", "Politiek lokaal"), ("landelijk", "Politiek landelijk"), ("overig", "Overig nieuws"))
SOORT_NAMEN = dict(SOORTEN)
# De categorieën op het scherm, met de stromen erin (de eerste is de belangrijkste).
CATEGORIEEN = (("keepers", "Keepersnieuws", ("keepers",)), ("politiek", "Politiek nieuws", ("politiek", "landelijk")),
               ("overig", "Overig nieuws", ("overig",)))
DEEL_NAMEN = {"politiek": "Lokaal en regionaal", "landelijk": "Landelijk"}
STROMEN = {"overig": "Overig nieuws", "landelijk": "Politiek landelijk"}  # Vera's eigen bronnen

STANDAARD_BRONNEN = [
    # (naam, url of zoekopdracht, soort, rubriek); soort: feed of google (zoekopdracht in Google News)
    ("NOS Nieuws", "https://feeds.nos.nl/nosnieuwsalgemeen", "feed", "Algemeen"),
    ("NOS Tech", "https://feeds.nos.nl/nosnieuwstech", "feed", "Tech"),
    ("Tweakers", "https://feeds.feedburner.com/tweakers/mixed", "feed", "Tech"),
    ("Google: informatiemanagement", '"functioneel beheer" OR informatiemanagement OR informatievoorziening OR informatiebeheer', "google", "FB/IM"),
    ("Google: digitale overheid", 'site:ibestuur.nl OR "digitale overheid" OR informatiehuishouding', "google", "FB/IM"),
    ("Google: AI in organisaties", '"kunstmatige intelligentie" OR "generatieve AI" overheid OR organisaties OR werkvloer', "google", "Tech"),
]

STANDAARD_LANDELIJK = [
    ("NOS Politiek", "https://feeds.nos.nl/nosnieuwspolitiek", "feed", "Den Haag"),
    ("Google: Tweede Kamer en kabinet", '"Tweede Kamer" OR kabinet OR minister OR coalitie', "google", "Den Haag"),
    ("Google: Rijk en gemeenten", 'kabinet OR Kamer gemeenten OR gemeentefonds OR jeugdzorg OR Wmo OR "Binnenlandse Zaken"', "google", "Gemeenten"),
    ("Google: GroenLinks-PvdA landelijk", '"GroenLinks-PvdA" Kamer OR Kamerfractie OR partij', "google", "Partij"),
]

PROFIEL = """Martijn Buurman werkt in informatiemanagement en functioneel beheer (FB/IM): hij schrijft boeken en geeft
opleidingen over regie op de informatievoorziening ("Pak het stuur", "Zet de koers") en werkt voor Upper Spire aan
Cartularius, een documentmanagementsysteem. Hij is ook gemeenteraadslid in Helmond en schrijft over keepers, maar dat
nieuws komt via andere stromen; hier gaat het om het overige nieuws."""

SYSTEEM = """Je bent Vera, de nieuwsredactie van Martijn Buurman. Je weegt nieuwsberichten op hoe belangrijk ze voor hem zijn.

{profiel}

Geef elk bericht een score van 0 tot 100. Wees streng: de meeste berichten scoren onder de 50.
- 80 tot 100: groot nieuws dat iedereen moet weten (grote binnenlandse of buitenlandse gebeurtenissen), of direct van
  belang voor zijn vak (informatiemanagement, functioneel beheer, digitale overheid, documentmanagement, AI in organisaties).
- 60 tot 79: goed om te weten: belangrijk landelijk nieuws, nieuwe regels of trends in zijn vak, grote techontwikkelingen.
- 40 tot 59: achtergrond.
- 0 tot 39: niet relevant (sportuitslagen, ongelukken, misdaad, showbizz, productnieuws en kortingen, reclame).

Geef per bericht ook "waarom": één korte zin in het Nederlands waarom het (niet) voor Martijn telt. Geen gedachtestreepjes.
Antwoord alleen met JSON: [{{"id": 1, "score": 72, "waarom": "..."}}, ...]"""

PROFIEL_LANDELIJK = """Martijn Buurman is gemeenteraadslid in Helmond voor PRO Helmond, de lokale partij van GroenLinks en PvdA,
in de oppositie. Zijn portefeuilles: digitalisering, pleegzorg en mantelzorg; hij werkt ook aan sport, wonen en de
begroting. Het lokale nieuws krijgt hij via een andere stroom; hier gaat het om wat er landelijk speelt."""

SYSTEEM_LANDELIJK = """Je bent Vera, de nieuwsredactie van Martijn Buurman. Je weegt landelijk politiek nieuws op hoe belangrijk het voor hem is.

{profiel}

Geef elk bericht een score van 0 tot 100. Wees streng: de meeste berichten scoren onder de 50.
- 80 tot 100: grote politieke gebeurtenissen (kabinet, coalitie, verkiezingen, begroting en Prinsjesdag), of besluiten
  met directe gevolgen voor gemeenten (gemeentefonds, jeugdzorg, Wmo, pleegzorg, mantelzorg, wonen, bestaanszekerheid,
  digitale overheid en informatiehuishouding).
- 60 tot 79: belangrijke Kamerdebatten, wetsvoorstellen en standpunten, ook van GroenLinks-PvdA in de Kamer.
- 40 tot 59: achtergrond.
- 0 tot 39: niet relevant (peilingen zonder nieuws, personalia, incidenten, buitenlandse politiek zonder Nederlandse kant).

Geef per bericht ook "waarom": één korte zin in het Nederlands waarom het (niet) voor Martijn telt, liefst met wat het
betekent voor gemeenten of voor Helmond. Geen gedachtestreepjes.
Antwoord alleen met JSON: [{{"id": 1, "score": 72, "waarom": "..."}}, ...]"""


# ---------------------------------------------------------------- ophalen

def _haal(url, maximum=4 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, text/xml, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(maximum)


def schoon(tekst, lengte=500):
    """Tekst zonder HTML en overtollige spaties. Pure functie."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", tekst or ""))
    return re.sub(r"\s+", " ", t).strip()[:lengte]


def feed_url(bron):
    if bron["soort"] == "google":
        q = bron["url"] + ("" if "when:" in bron["url"] else " when:2d")
        return GOOGLE + urllib.parse.quote(q)
    return bron["url"]


def lees_feed(inhoud, soort="feed"):
    """De berichten uit een RSS- of Atom-feed. Pure functie (op de inhoud)."""
    import feedparser  # pylint: disable=import-outside-toplevel
    f = feedparser.parse(inhoud)
    if f.bozo and not f.entries:
        raise ValueError(f"geen geldige feed ({f.bozo_exception})")
    uit = []
    for e in f.entries:
        titel, bronnaam = schoon(e.get("title", ""), 300), ""
        if soort == "google":
            bronnaam = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
            if bronnaam and titel.endswith(" - " + bronnaam):
                titel = titel[: -len(bronnaam) - 3]
        t = e.get("published_parsed") or e.get("updated_parsed")
        datum = dt.datetime.fromtimestamp(time.mktime(t), dt.timezone.utc) if t else None
        link = e.get("link") or ""
        if titel and link.startswith(("http://", "https://")):
            sam = schoon(e.get("summary", ""))
            if soort == "google" or sam.startswith(titel[:40]):  # Google News herhaalt alleen de kop
                sam = ""
            uit.append({"titel": titel, "link": link, "samenvatting": sam, "datum": datum, "bronnaam": bronnaam,
                        "guid": (e.get("id") or link)[:500]})
    return uit


def zonder_cite(tekst):
    """Bronverwijzingen van Claude (<cite index="...">...</cite>) uit een tekst halen; de tekst ertussen blijft. Pure functie."""
    return kern_tekst.schoon(tekst)  # ook andere HTML-code, en &quot; en dergelijke


def sleutel(titel):
    """Kop zonder bronvermelding achteraan, voor het herkennen van hetzelfde nieuws bij twee bronnen. Pure functie."""
    t = re.sub(r"\s[-|]\s[^-|]{2,40}$", "", titel or "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t.lower())).strip()


def ophalen(c, haal=_haal):
    """Alle actieve bronnen van Vera ophalen. Geeft (nieuw, fouten)."""
    # Landelijke politiek eerst: staat hetzelfde bericht ook in een algemene feed, dan hoort het bij de politiek.
    bronnen = c.execute("SELECT * FROM vera_bronnen WHERE actief ORDER BY stroom = 'landelijk' DESC, id").fetchall()
    recent = [(r["id"], sleutel(r["titel"]), r["stroom"]) for r in c.execute(
        "SELECT id, titel, stroom FROM vera_berichten WHERE binnen > now() - interval '4 days'").fetchall()]
    grens = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    nieuw, fouten = 0, []
    for b in bronnen:
        try:
            berichten = lees_feed(haal(feed_url(b)), b["soort"])
            aantal = 0
            for m in berichten:
                if m["datum"] and m["datum"] < grens:
                    continue
                al = c.execute("SELECT id, stroom FROM vera_berichten WHERE guid = %s OR url = %s", (m["guid"], m["link"])).fetchone()
                if al:
                    if b["stroom"] == "landelijk" and al["stroom"] == "overig":
                        _naar_landelijk(c, al["id"])
                    continue
                k = sleutel(m["titel"])
                dubbel, dubbel_stroom = next(((i, st) for i, s, st in recent if s == k or difflib.SequenceMatcher(None, s, k).ratio() > 0.88),
                                             (None, None))
                if dubbel and b["stroom"] == "landelijk" and dubbel_stroom == "overig":
                    _naar_landelijk(c, dubbel)
                if dubbel:
                    naam = m["bronnaam"] or b["naam"]
                    c.execute("""UPDATE vera_berichten SET ook_bij = left(ook_bij || CASE WHEN ook_bij = '' THEN '' ELSE ', ' END || %s, 300)
                                 WHERE id = %s AND bron <> %s AND position(%s in ook_bij) = 0""", (naam, dubbel, naam, naam))
                    continue
                r = c.execute("""INSERT INTO vera_berichten (bron_id, guid, titel, url, samenvatting, bron, rubriek, gepubliceerd, stroom)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (guid) DO NOTHING RETURNING id""",
                              (b["id"], m["guid"], m["titel"], m["link"], m["samenvatting"], m["bronnaam"] or b["naam"], b["rubriek"],
                               m["datum"], b["stroom"])).fetchone()
                if r:
                    recent.append((r["id"], k, b["stroom"]))
                    aantal += 1
            nieuw += aantal
            c.execute("UPDATE vera_bronnen SET laatst = now(), status = %s, fout = '' WHERE id = %s",
                      (f"{len(berichten)} in de feed, {aantal} nieuw", b["id"]))
        except Exception as e:  # noqa: BLE001  (één bron mag de rest niet tegenhouden)
            fouten.append(f"{b['naam']}: {e}")
            c.execute("UPDATE vera_bronnen SET laatst = now(), fout = %s WHERE id = %s", (str(e)[:300], b["id"]))
    c.execute("DELETE FROM vera_berichten WHERE binnen < now() - make_interval(days => %s)", (BEWAAR_DAGEN,))
    c.execute("DELETE FROM vera_gelezen WHERE tijd < now() - make_interval(days => %s)", (BEWAAR_DAGEN * 2,))
    return nieuw, fouten


def _naar_landelijk(c, bid):
    """Een bericht uit een algemene feed blijkt landelijke politiek: verhuizen en opnieuw wegen (met de politieke blik)."""
    c.execute("UPDATE vera_berichten SET stroom = 'landelijk', score = NULL, waarom = '' WHERE id = %s AND stroom = 'overig'", (bid,))


# ---------------------------------------------------------------- wegen

def vandaag_besteed(c):
    return float(c.execute("""SELECT coalesce(sum(kosten_eur), 0) AS s FROM claude_gebruik
                              WHERE persona = 'vera' AND tijd >= date_trunc('day', now() AT TIME ZONE 'Europe/Amsterdam') AT TIME ZONE 'Europe/Amsterdam'""").fetchone()["s"])


def json_uit(tekst):
    """De eerste JSON-lijst uit een antwoord, ook met tekst eromheen. Pure functie."""
    t = tekst.replace("```json", "").replace("```", "")
    begin, eind = t.find("["), t.rfind("]")
    if begin < 0 or eind < begin:
        raise ValueError("geen JSON-lijst in het antwoord")
    return json.loads(t[begin:eind + 1])


def wegen(c, per_keer=25, maximum=150):
    """Ongewogen berichten wegen met Claude, tot het dagplafond. Geeft een korte uitslag."""
    if not config.ANTHROPIC_API_KEY:
        return "geen Claude-sleutel; het nieuwste staat bovenaan"
    rijen = c.execute("""SELECT id, titel, samenvatting, bron, rubriek, stroom FROM vera_berichten WHERE score IS NULL
                         ORDER BY stroom = 'landelijk' DESC, id DESC LIMIT %s""", (maximum,)).fetchall()
    gewogen = 0
    systemen = {"overig": SYSTEEM.format(profiel=PROFIEL), "landelijk": SYSTEEM_LANDELIJK.format(profiel=PROFIEL_LANDELIJK)}
    groepen = []
    for stroom in ("landelijk", "overig"):
        deel = [r for r in rijen if r["stroom"] == stroom]
        groepen += [(systemen[stroom], deel[i:i + per_keer]) for i in range(0, len(deel), per_keer)]
    for systeem, groep in groepen:
        if vandaag_besteed(c) >= DAGPLAFOND_EUR:
            return f"{gewogen} gewogen; dagplafond van € {DAGPLAFOND_EUR:.2f} bereikt"
        lijst = "\n\n".join(f"[{n}] {r['titel']}\nBron: {r['bron']} ({r['rubriek']})\n{(r['samenvatting'] or '')[:300]}"
                            for n, r in enumerate(groep, 1))
        try:
            tekst = claude.gesprek(c, "vera", systeem, [{"role": "user", "content": f"Beoordeel deze {len(groep)} berichten:\n\n{lijst}"}],
                                   model=config.CLAUDE_MODEL_LICHT, max_tokens=90 * len(groep) + 200, inspanning=None)
            uitslag = {int(o["id"]): o for o in json_uit(tekst) if isinstance(o, dict) and "id" in o}
        except (claude.ClaudeFout, ValueError, TypeError) as e:
            return f"{gewogen} gewogen; wegen lukte niet: {e}"
        for n, r in enumerate(groep, 1):
            o = uitslag.get(n)
            if o is None:
                continue
            try:
                score = max(0, min(100, int(o.get("score"))))
            except (TypeError, ValueError):
                continue
            waarom = str(o.get("waarom") or "").replace(" — ", ", ").replace("—", ", ").strip()[:300]
            c.execute("UPDATE vera_berichten SET score = %s, waarom = %s WHERE id = %s", (score, waarom, r["id"]))
            gewogen += 1
    return f"{gewogen} gewogen"


@planner.taak("nieuws", elke=dt.timedelta(minutes=15))
def taak_nieuws(c):
    nieuw, fouten = ophalen(c)
    weging = wegen(c)
    return f"{nieuw} nieuw, {weging}" + (f"; niet gelukt: {'; '.join(fouten[:3])}" if fouten else "")


# ---------------------------------------------------------------- lezen (ook uit de werkruimtes)

WEGGEFILTERD = ("onleesbaar", "geen_inhoud", "zoeken_mislukt", "geleerd_weg", "genegeerd")


def keepers(dagen):
    """Keepersnieuws uit de Doellijn. Geeft een lijst, of None als de werkruimte hier niet draait."""
    if not config.ZEROHERO_ACTIEF:
        return None
    from ..zerohero import main as zh  # pylint: disable=import-outside-toplevel
    with zh.pool.connection() as c:
        rijen = c.execute("""SELECT id, titel, url, bron, coalesce(gepubliceerd, binnen) AS tijd, score, samenvatting, invalshoek,
                                    sport, competitie, keepers FROM items
                             WHERE binnen > now() - make_interval(days => %s) AND NOT (status = ANY(%s))
                               AND coalesce(url, '') ~ '^https?://' AND coalesce(titel, '') <> ''
                             ORDER BY score DESC NULLS LAST, binnen DESC LIMIT 60""", (dagen, list(WEGGEFILTERD))).fetchall()
    return [{"soort": "keepers", "id": r["id"], "titel": r["titel"], "url": r["url"], "bron": r["bron"] or "", "tijd": r["tijd"],
             "score": r["score"], "samenvatting": r["samenvatting"] or "", "waarom": r["invalshoek"] or "",
             "keepers": list(r["keepers"] or []),
             "labels": [x for x in (r["sport"], r["competitie"]) if x] + (["🧤 " + ", ".join(r["keepers"][:3])] if r["keepers"] else [])}
            for r in rijen]


def politiek(dagen):
    """Politiek nieuws uit Promonitor. Geeft een lijst, of None als de werkruimte hier niet draait."""
    if not config.POLITIEK_ACTIEF:
        return None
    from ..politiek import main as pm  # pylint: disable=import-outside-toplevel
    with pm.pool.connection() as c:
        rijen = c.execute("""SELECT id, titel, document_url, bronnaam, toegevoegd, score, reden, onderwerp, actie, context FROM stukken
                             WHERE bron = 'nieuws' AND status <> 'genegeerd' AND coalesce(score, 0) >= 0
                               AND toegevoegd > now() - make_interval(days => %s) AND document_url ~ '^https?://'
                             ORDER BY score DESC NULLS LAST, toegevoegd DESC LIMIT 60""", (dagen,)).fetchall()
    return [{"soort": "politiek", "id": r["id"], "titel": r["titel"], "url": r["document_url"], "bron": r["bronnaam"] or "",
             "tijd": r["toegevoegd"], "score": r["score"], "samenvatting": schoon(r["context"], 300), "waarom": r["reden"] or "",
             "labels": [x for x in (r["onderwerp"], {"vragen": "vragen overwegen", "agenderen": "agenderen", "reageren": "reageren"}.get(r["actie"] or ""))
                        if x]} for r in rijen]


def eigen(c, dagen, stroom="overig"):
    """Vera's eigen berichten van één stroom (overig of landelijk)."""
    rijen = c.execute("""SELECT * FROM vera_berichten WHERE binnen > now() - make_interval(days => %s) AND NOT verborgen AND stroom = %s
                         ORDER BY score DESC NULLS LAST, coalesce(gepubliceerd, binnen) DESC LIMIT 80""", (dagen, stroom)).fetchall()
    return [{"soort": stroom, "id": r["id"], "titel": r["titel"], "url": r["url"], "bron": r["bron"], "tijd": r["gepubliceerd"] or r["binnen"],
             "score": r["score"], "samenvatting": r["samenvatting"], "waarom": r["waarom"] or "",
             "labels": [r["rubriek"]] + ([f"ook bij {r['ook_bij']}"] if r["ook_bij"] else [])} for r in rijen]


def verzamel(c, dagen, lezers=None):
    """Alle stromen, met wat al gelezen is. Een werkruimte die niet bereikbaar is, geeft een foutregel."""
    lezers = lezers or {"keepers": keepers, "politiek": politiek}
    uit, fouten = {}, {}
    for soort in ("keepers", "politiek"):
        try:
            uit[soort] = lezers[soort](dagen)
        except Exception as e:  # noqa: BLE001  (een werkruimte die hapert, mag de rest niet tegenhouden)
            uit[soort], fouten[soort] = [], str(e)[:200]
    uit["landelijk"] = eigen(c, dagen, "landelijk")
    uit["overig"] = eigen(c, dagen, "overig")
    gelezen = {(r["soort"], r["bericht"]) for r in c.execute("SELECT soort, bericht FROM vera_gelezen").fetchall()}
    for lijst in uit.values():
        for b in lijst or []:
            b["gelezen"] = (b["soort"], b["id"]) in gelezen
            # Samenvattingen uit de werkruimtes (Claude met webzoeken) kunnen <cite>-tags bevatten: weg ermee.
            for veld in ("titel", "samenvatting", "waarom"):
                b[veld] = zonder_cite(b.get(veld) or "")
            b["labels"] = [zonder_cite(x) for x in b.get("labels") or []]
    return uit, fouten


STOPWOORDEN = set("""de het een en van in op te voor met bij aan om is zijn was wordt na naar over uit door als dat die dit
    niet ook nog maar dan wel tegen tot onder the a an of to for on at by with from as is are was be his her its after""".split())


def woorden(titel):
    """De woorden die iets zeggen in een kop (zonder lidwoorden en voorzetsels). Pure functie."""
    return {w for w in sleutel(titel).split() if len(w) > 2 and w not in STOPWOORDEN}


def _achternamen(namen):
    return {n.split()[-1].lower() for n in namen if n and len(n.split()[-1]) >= 4}


def dubbel(a, b):
    """Gaan twee berichten over hetzelfde? Pure functie.
    Ja bij (bijna) dezelfde kop, bij een kop die voor het grootste deel uit dezelfde woorden bestaat, of (keepersnieuws)
    als ze over dezelfde keeper gaan: in de lijst van de Doellijn of met de achternaam in de kop."""
    ka, wa, na, sa = _kenmerken(a)
    kb, wb, nb, sb = _kenmerken(b)
    if ka == kb:
        return True
    vgl = difflib.SequenceMatcher(None, ka[:100], kb[:100], autojunk=False)  # het begin zegt genoeg (socials: lange teksten)
    if vgl.real_quick_ratio() > 0.85 and vgl.quick_ratio() > 0.85 and vgl.ratio() > 0.85:
        return True
    if len(wa) >= 3 and len(wb) >= 3 and len(wa & wb) / min(len(wa), len(wb)) >= 0.7:
        return True
    if na & nb:
        return True
    return bool((na and na & sb) or (nb and nb & sa))


def _kenmerken(b):
    """Kop, woorden en achternamen van een bericht, één keer uitgerekend (ontdubbelen vergelijkt elk bericht met
    alle eerdere)."""
    k = b.get("_kenmerken")
    if k is None or k[0] != b.get("titel"):
        kop = sleutel(b["titel"])
        k = (b.get("titel"), (kop, woorden(b["titel"]), _achternamen(b.get("keepers") or []), set(kop.split())))
        b["_kenmerken"] = k
    return k[1]


def ontdubbel(lijst, al=(), hooguit=None):
    """Houd per onderwerp alleen het eerste bericht over (de lijst is al op belang gesorteerd). De bronnen van wat
    wegvalt, komen bij het bewaarde bericht als 'ook' te staan. Berichten in 'al' tellen als al getoond. Met hooguit
    stopt het zodra er genoeg zijn (wat daarna komt, wordt toch niet getoond). Pure functie."""
    gezien, uit = list(al), []
    for b in lijst:
        if hooguit is not None and len(uit) >= hooguit:
            break
        eerder = next((x for x in gezien if dubbel(x, b)), None)
        if eerder is not None:
            bron = b.get("bron") or ""
            if eerder in uit and bron and bron != eerder.get("bron") and bron not in eerder["ook"]:
                eerder["ook"].append(bron)
            continue
        kopie = dict(b, ook=[])
        gezien.append(kopie)
        uit.append(kopie)
    return uit


def selectie(lijst, drempel=40, aantal=12, al=(), dubbels=False):
    """Wat Vera in een kolom laat zien: eerst wat gewogen is (hoogste score), ongewogen nieuw erna. Pure functie.
    Berichten onder de drempel vallen weg (minder nieuws, beter nieuws), tenzij er nog niets gewogen is."""
    if lijst is None:
        return None
    lijst = [b for b in lijst if b["score"] is None or b["score"] >= drempel]
    nul = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    lijst.sort(key=lambda b: (b["score"] is None, -(b["score"] or 0), -(b["tijd"] or nul).timestamp()))
    return (lijst if dubbels else ontdubbel(lijst, al, hooguit=aantal))[:aantal]


def uitgelicht(stromen, nu, uren=36, drempel=65):
    """Het beste bericht per categorie van de laatste anderhalve dag, voor bovenaan de pagina. Pure functie.
    Bij politiek gaat lokaal voor: landelijk komt alleen bovenaan als er geen belangrijk lokaal nieuws is."""
    grens = nu - dt.timedelta(hours=uren)
    uit = []
    for _cat, _naam, delen in CATEGORIEEN:
        for soort in delen:
            kandidaten = [b for b in stromen.get(soort) or [] if b["score"] is not None and b["score"] >= drempel
                          and b["tijd"] and b["tijd"] >= grens and not b.get("gelezen")]
            if kandidaten:
                uit.append(max(kandidaten, key=lambda b: (b["score"], b["tijd"])))
                break
    return uit


def koppen(c, nu, aantal=5):
    """De belangrijkste koppen van vandaag over alle stromen, voor Vandaag en Iris."""
    stromen, _ = verzamel(c, 2)
    alles = [b for lijst in stromen.values() for b in (lijst or []) if b["score"] is not None and b["score"] >= 60]
    alles.sort(key=lambda b: (-b["score"], -(b["tijd"] or nu).timestamp()))
    return ontdubbel(alles, hooguit=aantal)[:aantal]


def koppen_per_soort(stromen, nu, per=3, uren=36, drempel=40):
    """De belangrijkste koppen per stroom van de laatste anderhalve dag, voor Vandaag. Pure functie.
    Ongelezen eerst, dan op belang; ongewogen nieuws telt mee als er (nog) niets gewogen is. None: stroom niet beschikbaar.
    per: een aantal voor elke stroom, of per stroom ({"politiek": 3, "landelijk": 2}). Landelijk nieuws dat ook lokaal
    al staat, valt weg."""
    grens, uit = nu - dt.timedelta(hours=uren), {}
    nul = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    for soort, _ in SOORTEN:
        n = per.get(soort, 3) if isinstance(per, dict) else per
        lijst = stromen.get(soort)
        if lijst is None:
            uit[soort] = None
            continue
        recent = [b for b in lijst if b["tijd"] and b["tijd"] >= grens and (b["score"] is None or b["score"] >= drempel)]
        recent.sort(key=lambda b: (bool(b.get("gelezen")), b["score"] is None, -(b["score"] or 0), -(b["tijd"] or nul).timestamp()))
        al = (uit.get("politiek") or ()) if soort == "landelijk" else ()
        uit[soort] = ontdubbel(recent, al, hooguit=n)[:n]
    return uit


def url_van(c, soort, bid, lezers_url=None):
    """Het adres van één bericht (voor doorklikken). None als het niet (meer) bestaat."""
    if soort in STROMEN:
        r = c.execute("SELECT url FROM vera_berichten WHERE id = %s", (bid,)).fetchone()
        return r["url"] if r else None
    if lezers_url:
        return lezers_url(soort, bid)
    if soort == "keepers" and config.ZEROHERO_ACTIEF:
        from ..zerohero import main as zh  # pylint: disable=import-outside-toplevel
        with zh.pool.connection() as zc:
            r = zc.execute("SELECT url FROM items WHERE id = %s", (bid,)).fetchone()
        return r["url"] if r else None
    if soort == "politiek" and config.POLITIEK_ACTIEF:
        from ..politiek import main as pm  # pylint: disable=import-outside-toplevel
        with pm.pool.connection() as pc:
            r = pc.execute("SELECT document_url FROM stukken WHERE id = %s AND bron = 'nieuws'", (bid,)).fetchone()
        return r["document_url"] if r else None
    return None
