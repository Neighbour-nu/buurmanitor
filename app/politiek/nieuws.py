"""Nieuwsradar (M1): nieuws ophalen uit de bronnen, dubbele berichten eruit, wegen met Claude.

Bronnen staan in de tabel bronnen en zijn te beheren op het scherm Filter. Een bron is een RSS- of
Atom-feed, een website (Promonitor zoekt dan zelf de feed) of een Google News-zoekopdracht.
Berichten komen in de tabel stukken (bron 'nieuws'), dus in dezelfde Inbox als de raadsinformatie.
"""
import datetime as dt
import difflib
import html
import re
import time
import urllib.parse
import urllib.request

from . import claude

UA = "Mozilla/5.0 (compatible; Promonitor; PRO Helmond)"
GOOGLE = "https://news.google.com/rss/search?hl=nl&gl=NL&ceid=NL:nl&q="

STANDAARD_BRONNEN = [
    # (naam, url, soort) ; soort: feed, site (feed zoeken), google (zoekopdracht)
    ("Gemeente Helmond", "https://www.helmond.nl", "site"),
    ("ED Helmond", "https://www.ed.nl/helmond/rss.xml", "feed"),
    ("Omroep Brabant", "https://www.omroepbrabant.nl/rss", "feed"),
    ("Dit is Helmond", "https://ditishelmond.nl/feed/", "feed"),
    ("Google: Binnenlands Bestuur", "site:binnenlandsbestuur.nl gemeente OR gemeenten OR raadsleden", "google"),
    ("Google: Helmond politiek", 'Helmond gemeenteraad OR wethouder OR college OR burgemeester', "google"),
    ("Google: PRO Helmond", '"PRO Helmond" OR "GroenLinks-PvdA Helmond"', "google"),
    ("Google: coalitiepartijen", '"Helder Helmond" OR "VVD Helmond" OR "D66 Helmond" OR "CDA Helmond" OR "Lokaal Sterk"', "google"),
    ("Google: wijken en projecten", 'Helmond Brandevoort OR Dierdonk OR "Mierlo-Hout" OR Stiphout OR Rijpelberg OR Suytkade OR "Automotive Campus" OR EHAD', "google"),
    ("Google: portefeuilles Martijn", 'Helmond digitalisering OR pleegzorg OR mantelzorg OR ICT', "google"),
    ("Google: wonen Helmond", 'Helmond woningbouw OR huurwoningen OR woCom OR Woonpartners', "google"),
    ("Google: sport en cultuur Helmond", 'Helmond sportaccommodaties OR sportclub OR cultuur OR theater', "google"),
    ("Google: Metropoolregio Eindhoven", '"Metropoolregio Eindhoven" OR Brainport OR "regio Eindhoven" gemeenten', "google"),
    ("Google: De Loop", 'site:deloop.nl Helmond', "google"),
    ("Google: Studio040", 'site:studio040.nl Helmond', "google"),
    ("Google: provincie Noord-Brabant", '"provincie Noord-Brabant" Helmond OR Peel OR Zuidoost-Brabant', "google"),
    ("Google: GGD en veiligheidsregio", '"GGD Brabant-Zuidoost" OR "Veiligheidsregio Brabant-Zuidoost" OR "Aa en Maas"', "google"),
    ("Google: VNG en gemeentefinanciën", 'VNG gemeentefonds OR ravijnjaar OR gemeenten begroting', "google"),
]

STANDAARD_PROFIEL = """PRO Helmond is de lokale partij van GroenLinks en PvdA in Helmond (Noord-Brabant) en zit in de oppositie.
De coalitie bestaat uit Helder Helmond, VVD, D66, CDA en Lokaal Sterk.
Martijn Buurman is raadslid. Zijn portefeuilles: digitalisering, pleegzorg en mantelzorg. Hij werkt ook aan sport, wonen en
de begroting.
Lopende dossiers van PRO Helmond: deprivatisering en betaalbaarheid van sportaccommodaties, bouwplan Baroniehof (bomen),
programmabegroting 2027-2030, grondexploitaties (Mierloseweg, Woonzorg zone), ambtelijk horen bij bezwaarschriften,
digitale weerbaarheid, het EHAD-terrein en het Stationskwartier, participatie, winteropvang daklozen.
Thema's waar PRO Helmond voor staat: bestaanszekerheid en armoede, betaalbaar wonen, klimaat en groen, gelijke kansen,
toegankelijke sport en cultuur, goede zorg en jeugdzorg, een open en eerlijk bestuur."""

SYSTEEM = """Je bent de nieuwsredactie van Promonitor, de politieke cockpit van een gemeenteraadslid.
Je beoordeelt nieuwsberichten op hoe belangrijk ze zijn voor deze fractie:

{profiel}

Geef elk bericht een score van 0 tot 100. Wees streng: de meeste berichten scoren onder de 50.
- 80 tot 100: direct van belang. Gaat over Helmonds gemeentepolitiek, een lopend dossier of een portefeuille van
  Martijn, of vraagt om actie (vragen stellen, reageren, agenderen).
- 60 tot 79: belangrijk om te weten. Helmondse besluiten, plannen of problemen; regio met gevolgen voor Helmond.
- 40 tot 59: achtergrond. Algemeen Helmonds nieuws met een politieke kant, landelijk gemeentenieuws.
- 0 tot 39: niet relevant (sport-uitslagen, ongelukken, evenementen, misdaad zonder beleidskant, andere gemeenten
  zonder link met Helmond, reclame).

Geef per bericht ook:
- reden: één korte zin in het Nederlands waarom dit (niet) belangrijk is voor PRO Helmond.
- onderwerp: twee tot vier woorden.
- actie: "vragen" (schriftelijke vragen overwegen), "agenderen", "reageren", "volgen" of "geen".

{regels}
Antwoord alleen met JSON: [{{"id": 1, "score": 72, "reden": "...", "onderwerp": "...", "actie": "volgen"}}, ...]"""


# ---------- ophalen ----------

def _haal(url, maximum=4 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, text/xml, text/html;q=0.8, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(maximum), (r.headers.get("Content-Type") or "").lower(), r.geturl()


def zoek_feed(site):
    """Zoekt de RSS- of Atom-feed van een website (link rel=alternate, anders /feed/ of /rss)."""
    inhoud, soort, eind = _haal(site)
    tekst = inhoud.decode("utf-8", "ignore")
    if "xml" in soort or tekst.lstrip().startswith("<?xml") or "<rss" in tekst[:500]:
        return eind
    for m in re.finditer(r"<link[^>]+>", tekst, re.I):
        tag = m.group(0)
        if re.search(r"application/(rss|atom)\+xml", tag, re.I):
            href = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if href:
                return urllib.parse.urljoin(eind, html.unescape(href.group(1)))
    for pad in ("/feed/", "/rss", "/rss.xml", "/nieuws/rss"):
        poging = urllib.parse.urljoin(eind, pad)
        try:
            inhoud, soort, eind2 = _haal(poging, 200_000)
            if "<rss" in inhoud[:2000].decode("utf-8", "ignore") or "<feed" in inhoud[:2000].decode("utf-8", "ignore"):
                return eind2
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("geen feed gevonden op deze website")


def _schoon(tekst, lengte=600):
    t = html.unescape(re.sub(r"<[^>]+>", " ", tekst or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:lengte]


def lees_feed(url, bron_soort="feed"):
    import feedparser  # pylint: disable=import-outside-toplevel
    inhoud, _soort, _eind = _haal(url)
    f = feedparser.parse(inhoud)
    if f.bozo and not f.entries:
        raise ValueError(f"geen geldige feed ({f.bozo_exception})")
    uit = []
    for e in f.entries:
        titel = _schoon(e.get("title", ""), 300)
        bronnaam = ""
        if bron_soort == "google":
            bronnaam = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
            if bronnaam and titel.endswith(" - " + bronnaam):
                titel = titel[: -len(bronnaam) - 3]
        t = e.get("published_parsed") or e.get("updated_parsed")
        datum = dt.datetime.fromtimestamp(time.mktime(t)) if t else None
        link = e.get("link") or ""
        if titel and link:
            uit.append({"titel": titel, "link": link, "samenvatting": _schoon(e.get("summary", "")),
                        "datum": datum, "bronnaam": bronnaam, "guid": e.get("id") or link})
    return uit


def feed_url(bron):
    if bron["soort"] == "google":
        q = bron["url"] + ("" if "when:" in bron["url"] else " when:3d")
        return GOOGLE + urllib.parse.quote(q)
    return bron.get("feed_url") or bron["url"]


def sleutel(titel):
    # Zonder bronvermelding achteraan ("... - Eindhovens Dagblad"), zodat hetzelfde nieuws bij twee bronnen herkend wordt.
    t = re.sub(r"\s[-|]\s[^-|]{2,40}$", "", titel or "")
    s = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return re.sub(r"\s+", " ", s).strip()


def ophalen(c, log=print):
    """Haalt alle actieve bronnen op en zet nieuwe berichten in stukken. Geeft (nieuw, fouten) terug."""
    bronnen = c.execute("SELECT * FROM bronnen WHERE actief ORDER BY id").fetchall()
    recent = [(r["id"], sleutel(r["titel"])) for r in c.execute(
        "SELECT id, titel FROM stukken WHERE bron = 'nieuws' AND toegevoegd > now() - interval '4 days'").fetchall()]
    grens = dt.datetime.now() - dt.timedelta(hours=48)
    nieuw, fouten = 0, []
    for b in bronnen:
        try:
            if b["soort"] == "site" and not b["feed_url"]:
                gevonden = zoek_feed(b["url"])
                c.execute("UPDATE bronnen SET feed_url = %s WHERE id = %s", (gevonden, b["id"]))
                b = dict(b, feed_url=gevonden)
            berichten = lees_feed(feed_url(b), b["soort"])
            aantal = 0
            for m in berichten:
                extern = "nieuws-" + m["guid"][:400]
                if c.execute("SELECT 1 FROM stukken WHERE extern_id = %s OR (bron = 'nieuws' AND document_url = %s)",
                             (extern, m["link"])).fetchone():
                    continue
                k = sleutel(m["titel"])
                dubbel = next((i for i, s in recent if s == k or difflib.SequenceMatcher(None, s, k).ratio() > 0.88), None)
                if dubbel:
                    naam = m["bronnaam"] or b["naam"]
                    c.execute("""UPDATE stukken SET context = left(context || CASE WHEN context = '' THEN '' ELSE ' ' END
                                   || %s, 2000) WHERE id = %s AND position(%s in context) = 0""",
                              (f"Ook bij {naam}.", dubbel, naam))
                    continue
                oud = m["datum"] is not None and m["datum"] < grens
                r = c.execute("""INSERT INTO stukken (bron, extern_id, soort, titel, datum, document_url, context,
                                   bronnaam, gezien, score, reden)
                                 VALUES ('nieuws', %s, 'nieuws', %s, %s, %s, %s, %s, %s, %s, %s)
                                 ON CONFLICT (extern_id) DO NOTHING RETURNING id""",
                              (extern, m["titel"], m["datum"].date() if m["datum"] else dt.date.today(), m["link"],
                               m["samenvatting"], m["bronnaam"] or b["naam"], oud,
                               -1 if oud else None,
                               "Ouder dan twee dagen; niet gewogen." if oud else None)).fetchone()
                if r:
                    recent.append((r["id"], k))
                    aantal += 1
                    nieuw += 0 if oud else 1
            c.execute("UPDATE bronnen SET laatst = now(), status = %s, fout = NULL WHERE id = %s",
                      (f"{len(berichten)} berichten in de feed, {aantal} nieuw", b["id"]))
        except Exception as e:  # noqa: BLE001 - één bron mag de rest niet tegenhouden
            fouten.append(f"{b['naam']}: {e}")
            c.execute("UPDATE bronnen SET laatst = now(), fout = %s WHERE id = %s", (str(e)[:300], b["id"]))
    return nieuw, fouten


# ---------- wegen ----------

def instelling(c, sleutel_, standaard=""):
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel_,)).fetchone()
    return r["waarde"] if r and r["waarde"] else standaard


def systeemtekst(c):
    regels = [r.strip() for r in instelling(c, "filter_regels").split("\n") if r.strip()]
    redenen = c.execute("""SELECT titel, negeer_reden FROM stukken WHERE bron = 'nieuws' AND coalesce(negeer_reden, '') <> ''
                           ORDER BY bijgewerkt DESC NULLS LAST, id DESC LIMIT 40""").fetchall()
    blok = ""
    if regels:
        blok += "Vaste regels van Martijn (volg die altijd):\n" + "\n".join(f"- {r}" for r in regels) + "\n\n"
    if redenen:
        blok += ("Martijn negeerde eerder deze berichten, met deze reden. Leer daarvan en geef vergelijkbare berichten "
                 "een lage score:\n" + "\n".join(f"- {r['titel'][:120]} (reden: {r['negeer_reden'][:120]})" for r in redenen) + "\n\n")
    return SYSTEEM.format(profiel=instelling(c, "nieuws_profiel", STANDAARD_PROFIEL), regels=blok)


def wegen(c, log=print, per_keer=20, maximum=200):
    """Weegt ongewogen nieuws met Claude, tot het dagplafond. Geeft (gewogen, melding) terug."""
    if not claude.actief():
        return 0, "geen Claude-sleutel"
    plafond = float(instelling(c, "claude_dagplafond_eur", "1.00").replace(",", ".") or 1)
    model = instelling(c, "claude_model_weging", "claude-haiku-4-5")
    rijen = c.execute("""SELECT id, titel, context, bronnaam FROM stukken WHERE bron = 'nieuws' AND score IS NULL
                         ORDER BY id DESC LIMIT %s""", (maximum,)).fetchall()
    gewogen, systeem = 0, None
    for i in range(0, len(rijen), per_keer):
        if claude.vandaag_uitgegeven(c, ["nieuws wegen"]) >= plafond:
            return gewogen, f"dagplafond van € {plafond:.2f} bereikt"
        groep = rijen[i:i + per_keer]
        systeem = systeem or systeemtekst(c)
        lijst = "\n\n".join(f"[{n}] {r['titel']}\nBron: {r['bronnaam']}\n{(r['context'] or '')[:400]}"
                            for n, r in enumerate(groep, 1))
        antwoord = claude.vraag(c, "nieuws wegen", systeem, f"Beoordeel deze {len(groep)} berichten:\n\n{lijst}",
                                model, max_tokens=150 * len(groep) + 200)
        try:
            uitslag = {int(o["id"]): o for o in claude.json_uit(antwoord)}
        except Exception as e:  # noqa: BLE001
            log(f"wegen: onleesbaar antwoord ({e})")
            continue
        for n, r in enumerate(groep, 1):
            o = uitslag.get(n)
            if not o:
                continue
            score = max(0, min(100, int(o.get("score", 0))))
            c.execute("""UPDATE stukken SET score = %s, reden = %s, onderwerp = %s, actie = %s,
                           pro = pro OR %s, gezien = gezien OR %s WHERE id = %s""",
                      (score, str(o.get("reden", ""))[:300], str(o.get("onderwerp", ""))[:80],
                       str(o.get("actie", ""))[:20], bool(re.search(r"\bPRO Helmond\b", r["titel"])), score < 20, r["id"]))
            gewogen += 1
    return gewogen, ""
