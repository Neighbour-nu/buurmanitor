"""Vera, de nieuwsredactie. Drie stromen op één plek:

- Keepersnieuws: uit de Doellijn (werkruimte ZEROHERO, tabel items), daar al gewogen en samengevat.
- Politiek nieuws: uit Promonitor (werkruimte Politiek, tabel stukken met bron 'nieuws'), daar al gewogen.
- Overig nieuws: Vera haalt het zelf op uit haar eigen bronnen (RSS en Google News) en weegt het met Claude
  (licht model, met een dagplafond). Zonder Claude-sleutel staat het nieuwste bovenaan.

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

UA = "Mozilla/5.0 (compatible; Buurmanitor; Vera)"
GOOGLE = "https://news.google.com/rss/search?hl=nl&gl=NL&ceid=NL:nl&q="
DAGPLAFOND_EUR = float(os.environ.get("VERA_DAGPLAFOND_EUR", "0.30"))
BEWAAR_DAGEN = 30

SOORTEN = (("keepers", "Keepersnieuws"), ("politiek", "Politiek nieuws"), ("overig", "Overig nieuws"))
SOORT_NAMEN = dict(SOORTEN)

STANDAARD_BRONNEN = [
    # (naam, url of zoekopdracht, soort, rubriek); soort: feed of google (zoekopdracht in Google News)
    ("NOS Nieuws", "https://feeds.nos.nl/nosnieuwsalgemeen", "feed", "Algemeen"),
    ("NOS Tech", "https://feeds.nos.nl/nosnieuwstech", "feed", "Tech"),
    ("Tweakers", "https://feeds.feedburner.com/tweakers/mixed", "feed", "Tech"),
    ("Google: informatiemanagement", '"functioneel beheer" OR informatiemanagement OR informatievoorziening OR informatiebeheer', "google", "FB/IM"),
    ("Google: digitale overheid", 'site:ibestuur.nl OR "digitale overheid" OR informatiehuishouding', "google", "FB/IM"),
    ("Google: AI in organisaties", '"kunstmatige intelligentie" OR "generatieve AI" overheid OR organisaties OR werkvloer', "google", "Tech"),
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


def sleutel(titel):
    """Kop zonder bronvermelding achteraan, voor het herkennen van hetzelfde nieuws bij twee bronnen. Pure functie."""
    t = re.sub(r"\s[-|]\s[^-|]{2,40}$", "", titel or "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t.lower())).strip()


def ophalen(c, haal=_haal):
    """Alle actieve bronnen van Vera ophalen. Geeft (nieuw, fouten)."""
    bronnen = c.execute("SELECT * FROM vera_bronnen WHERE actief ORDER BY id").fetchall()
    recent = [(r["id"], sleutel(r["titel"])) for r in c.execute(
        "SELECT id, titel FROM vera_berichten WHERE binnen > now() - interval '4 days'").fetchall()]
    grens = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    nieuw, fouten = 0, []
    for b in bronnen:
        try:
            berichten = lees_feed(haal(feed_url(b)), b["soort"])
            aantal = 0
            for m in berichten:
                if m["datum"] and m["datum"] < grens:
                    continue
                if c.execute("SELECT 1 FROM vera_berichten WHERE guid = %s OR url = %s", (m["guid"], m["link"])).fetchone():
                    continue
                k = sleutel(m["titel"])
                dubbel = next((i for i, s in recent if s == k or difflib.SequenceMatcher(None, s, k).ratio() > 0.88), None)
                if dubbel:
                    naam = m["bronnaam"] or b["naam"]
                    c.execute("""UPDATE vera_berichten SET ook_bij = left(ook_bij || CASE WHEN ook_bij = '' THEN '' ELSE ', ' END || %s, 300)
                                 WHERE id = %s AND bron <> %s AND position(%s in ook_bij) = 0""", (naam, dubbel, naam, naam))
                    continue
                r = c.execute("""INSERT INTO vera_berichten (bron_id, guid, titel, url, samenvatting, bron, rubriek, gepubliceerd)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (guid) DO NOTHING RETURNING id""",
                              (b["id"], m["guid"], m["titel"], m["link"], m["samenvatting"], m["bronnaam"] or b["naam"], b["rubriek"],
                               m["datum"])).fetchone()
                if r:
                    recent.append((r["id"], k))
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
    rijen = c.execute("""SELECT id, titel, samenvatting, bron, rubriek FROM vera_berichten WHERE score IS NULL
                         ORDER BY id DESC LIMIT %s""", (maximum,)).fetchall()
    gewogen = 0
    systeem = SYSTEEM.format(profiel=PROFIEL)
    for i in range(0, len(rijen), per_keer):
        if vandaag_besteed(c) >= DAGPLAFOND_EUR:
            return f"{gewogen} gewogen; dagplafond van € {DAGPLAFOND_EUR:.2f} bereikt"
        groep = rijen[i:i + per_keer]
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


def overig(c, dagen):
    rijen = c.execute("""SELECT * FROM vera_berichten WHERE binnen > now() - make_interval(days => %s) AND NOT verborgen
                         ORDER BY score DESC NULLS LAST, coalesce(gepubliceerd, binnen) DESC LIMIT 80""", (dagen,)).fetchall()
    return [{"soort": "overig", "id": r["id"], "titel": r["titel"], "url": r["url"], "bron": r["bron"], "tijd": r["gepubliceerd"] or r["binnen"],
             "score": r["score"], "samenvatting": r["samenvatting"], "waarom": r["waarom"] or "",
             "labels": [r["rubriek"]] + ([f"ook bij {r['ook_bij']}"] if r["ook_bij"] else [])} for r in rijen]


def verzamel(c, dagen, lezers=None):
    """Alle drie de stromen, met wat al gelezen is. Een werkruimte die niet bereikbaar is, geeft een foutregel."""
    lezers = lezers or {"keepers": keepers, "politiek": politiek}
    uit, fouten = {}, {}
    for soort in ("keepers", "politiek"):
        try:
            uit[soort] = lezers[soort](dagen)
        except Exception as e:  # noqa: BLE001  (een werkruimte die hapert, mag de rest niet tegenhouden)
            uit[soort], fouten[soort] = [], str(e)[:200]
    uit["overig"] = overig(c, dagen)
    gelezen = {(r["soort"], r["bericht"]) for r in c.execute("SELECT soort, bericht FROM vera_gelezen").fetchall()}
    for lijst in uit.values():
        for b in lijst or []:
            b["gelezen"] = (b["soort"], b["id"]) in gelezen
    return uit, fouten


def selectie(lijst, drempel=40, aantal=12):
    """Wat Vera in een kolom laat zien: eerst wat gewogen is (hoogste score), ongewogen nieuw erna. Pure functie.
    Berichten onder de drempel vallen weg (minder nieuws, beter nieuws), tenzij er nog niets gewogen is."""
    if lijst is None:
        return None
    lijst = [b for b in lijst if b["score"] is None or b["score"] >= drempel]
    nul = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    lijst.sort(key=lambda b: (b["score"] is None, -(b["score"] or 0), -(b["tijd"] or nul).timestamp()))
    return lijst[:aantal]


def uitgelicht(stromen, nu, uren=36, drempel=65):
    """Het beste bericht per stroom van de laatste anderhalve dag, voor bovenaan de pagina. Pure functie."""
    grens = nu - dt.timedelta(hours=uren)
    uit = []
    for soort, _ in SOORTEN:
        kandidaten = [b for b in stromen.get(soort) or [] if b["score"] is not None and b["score"] >= drempel
                      and b["tijd"] and b["tijd"] >= grens and not b.get("gelezen")]
        if kandidaten:
            uit.append(max(kandidaten, key=lambda b: (b["score"], b["tijd"])))
    return uit


def koppen(c, nu, aantal=5):
    """De belangrijkste koppen van vandaag over alle stromen, voor Vandaag en Iris."""
    stromen, _ = verzamel(c, 2)
    alles = [b for lijst in stromen.values() for b in (lijst or []) if b["score"] is not None and b["score"] >= 60]
    alles.sort(key=lambda b: (-b["score"], -(b["tijd"] or nu).timestamp()))
    return alles[:aantal]


def koppen_per_soort(stromen, nu, per=3, uren=36, drempel=40):
    """De belangrijkste koppen per stroom van de laatste anderhalve dag, voor Vandaag. Pure functie.
    Ongelezen eerst, dan op belang; ongewogen nieuws telt mee als er (nog) niets gewogen is. None: stroom niet beschikbaar."""
    grens, uit = nu - dt.timedelta(hours=uren), {}
    nul = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    for soort, _ in SOORTEN:
        lijst = stromen.get(soort)
        if lijst is None:
            uit[soort] = None
            continue
        recent = [b for b in lijst if b["tijd"] and b["tijd"] >= grens and (b["score"] is None or b["score"] >= drempel)]
        recent.sort(key=lambda b: (bool(b.get("gelezen")), b["score"] is None, -(b["score"] or 0), -(b["tijd"] or nul).timestamp()))
        uit[soort] = recent[:per]
    return uit


def url_van(c, soort, bid, lezers_url=None):
    """Het adres van één bericht (voor doorklikken). None als het niet (meer) bestaat."""
    if soort == "overig":
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
