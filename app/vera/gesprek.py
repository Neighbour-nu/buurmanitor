"""Praten met Vera over haar bronnen. Martijn zegt wat hij wil volgen ("ik wil meer over archivering", "zet Tweakers
uit", "iets uit Nijmegen"); Vera stelt bronnen voor: een RSS-feed, een website (het systeem zoekt de feed) of een
Google News-zoekopdracht. Elk voorstel wordt meteen getest: werkt de feed, hoeveel berichten, een voorbeeld. Martijn
ziet het als knop; pas na zijn klik verandert er iets.

Vera kan zelf niet op het web zoeken. Ze stelt voor wat ze kent; de test laat zien of het klopt.
"""
import html
import json
import re
import urllib.parse
import urllib.request

from .. import personas
from ..kern import claude, config
from . import nieuws

TAG = re.compile(r"<(bron|wijzig|bronactie)>\s*(\{.*?\})\s*</\1>", re.S)
ACTIES = ("aan", "uit", "weg")
UITLEG = """# Bronnen voor overig nieuws
Martijn vraagt je bronnen toe te voegen, aan te passen of uit te zetten voor zijn overige nieuws. Keepersnieuws (de
Doellijn) en politiek nieuws (Promonitor) hebben hun eigen bronnen; daar ga jij niet over. Zeg dat als hij erom vraagt.

Je kunt voorstellen klaarzetten; Martijn ziet ze als knop en beslist zelf. Per voorstel precies één regel aan het eind.

Een nieuwe bron:
<bron>{"naam": "Computable", "soort": "site", "url": "https://www.computable.nl", "rubriek": "FB/IM"}</bron>
"soort" is "feed" (het adres van een RSS- of Atom-feed), "site" (het adres van een website; het systeem zoekt dan zelf
de feed) of "google" (een zoekopdracht voor Google News, met OR en aanhalingstekens, bijvoorbeeld
"archiefwet" OR "Woo" OR informatiehuishouding). Weet je het feedadres niet zeker, kies dan "site" met de homepage,
of een Google News-zoekopdracht (eventueel met site:domein.nl). Een zoekopdracht werkt altijd en is vaak de beste keus
voor een onderwerp; een feed is beter voor één vaste bron.
"rubriek" is een kort label, zoals Algemeen, Tech, FB/IM, Nijmegen of Sport. Gebruik bij voorkeur een bestaande rubriek.

Een bestaande bron aanpassen (alleen wat verandert), bijvoorbeeld een betere zoekopdracht:
<wijzig>{"id": 3, "url": "...", "naam": "...", "rubriek": "..."}</wijzig>
Een bron aan- of uitzetten of verwijderen:
<bronactie>{"id": 3, "actie": "uit"}</bronactie>
"actie" is aan, uit of weg.

Elke nieuwe of aangepaste bron wordt na je antwoord getest; Martijn ziet dan of hij werkt. Je kunt zelf niet op het web
zoeken: verzin geen adressen die je niet kent, en zeg eerlijk als je twijfelt. Stel liever twee of drie gerichte bronnen
voor dan tien. Gebruik alleen id's uit de lijst. Zeg nooit dat je iets hebt toegevoegd: dat doet Martijn met de knop."""


def voorstellen_uit(tekst, bekend=()):
    """De voorstellen uit het antwoord van Vera, gecontroleerd. Geeft (tekst zonder de regels, lijst). Pure functie."""
    uit = []
    for m in TAG.finditer(tekst):
        try:
            v = json.loads(m.group(2))
        except ValueError:
            continue
        if not isinstance(v, dict):
            continue
        naam = str(v.get("naam") or "").strip()[:100]
        url = str(v.get("url") or "").strip()[:500]
        rubriek = str(v.get("rubriek") or "").strip()[:40]
        if m.group(1) == "bron":
            soort = str(v.get("soort") or "")
            if soort not in ("feed", "site", "google") or not naam or not url:
                continue
            if soort in ("feed", "site") and urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                continue
            uit.append({"soort": "nieuw", "bron_id": None, "gegevens": {"naam": naam, "soort": soort, "url": url, "rubriek": rubriek or "Algemeen"}})
            continue
        try:
            bid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        if bid not in bekend:
            continue
        if m.group(1) == "bronactie":
            actie = str(v.get("actie") or "")
            if actie in ACTIES:
                uit.append({"soort": actie, "bron_id": bid, "gegevens": {}})
            continue
        g = {k: w for k, w in (("naam", naam), ("url", url), ("rubriek", rubriek)) if w}
        if g:
            uit.append({"soort": "wijzig", "bron_id": bid, "gegevens": g})
    return TAG.sub("", tekst).strip(), uit


# ---------------------------------------------------------------- testen

LINK_ALT = re.compile(r"<link[^>]+>", re.I)
PADEN = ("/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/nieuws/rss")


def _is_feed(inhoud):
    begin = inhoud[:3000].decode("utf-8", "ignore").lower()
    return "<rss" in begin or "<feed" in begin or "<rdf:rdf" in begin


def zoek_feed(url, haal):
    """De feed van een website: de pagina zelf als die een feed is, anders link rel=alternate, anders de gangbare
    adressen. Geeft het feedadres, of gooit ValueError."""
    inhoud = haal(url)
    if _is_feed(inhoud):
        return url
    tekst = inhoud.decode("utf-8", "ignore")
    for m in LINK_ALT.finditer(tekst):
        tag = m.group(0)
        if re.search(r"application/(rss|atom)\+xml", tag, re.I):
            href = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if href:
                return urllib.parse.urljoin(url, html.unescape(href.group(1)))
    for pad in PADEN:
        poging = urllib.parse.urljoin(url, pad)
        try:
            if _is_feed(haal(poging)):
                return poging
        except Exception:  # noqa: BLE001  (dit pad bestaat niet; volgende proberen)
            continue
    raise ValueError("geen feed gevonden op deze website")


def _haal_kort(url):
    """Ophalen voor een test: korter wachten dan bij het gewone ophalen, zodat het gesprek vlot blijft."""
    req = urllib.request.Request(url, headers={"User-Agent": nieuws.UA, "Accept": "application/rss+xml, application/atom+xml, text/xml, text/html;q=0.8, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read(2 * 1024 * 1024)


def test_bron(g, haal=None):
    """Probeer een bron uit. Geeft de gegevens aangevuld met het resultaat: werkt (bool), aantal, voorbeeld of fout.
    Een website wordt een feed (met het gevonden adres)."""
    haal = haal or _haal_kort
    g = dict(g)
    try:
        if g.get("soort") == "site":
            g["site"] = g["url"]
            g["url"], g["soort"] = zoek_feed(g["url"], haal), "feed"
        berichten = nieuws.lees_feed(haal(nieuws.feed_url(g)), g.get("soort", "feed"))
        g |= {"werkt": bool(berichten), "aantal": len(berichten), "voorbeeld": berichten[0]["titel"][:140] if berichten else "",
              "fout": "" if berichten else "de feed werkt, maar er staan nu geen berichten in"}
    except Exception as e:  # noqa: BLE001  (elke fout is een uitslag van de test)
        g |= {"werkt": False, "aantal": 0, "voorbeeld": "", "fout": str(e)[:200] or e.__class__.__name__}
    return g


# ---------------------------------------------------------------- gesprek

def context(c):
    bronnen = c.execute("""SELECT b.*, (SELECT count(*) FROM vera_berichten m WHERE m.bron_id = b.id AND m.binnen > now() - interval '7 days') AS week,
                                  (SELECT round(avg(score)) FROM vera_berichten m WHERE m.bron_id = b.id AND m.score IS NOT NULL) AS gemiddeld
                           FROM vera_bronnen b ORDER BY b.rubriek, b.naam""").fetchall()
    regels = [f"- [id {b['id']}] {b['naam']} ({'Google News: ' if b['soort'] == 'google' else 'feed: '}{b['url']}; rubriek {b['rubriek']}; "
              f"{'aan' if b['actief'] else 'UIT'}; {b['week']} berichten deze week"
              + (f"; gemiddelde score {b['gemiddeld']}" if b["gemiddeld"] is not None else "")
              + (f"; FOUT: {b['fout']}" if b["fout"] else "") + ")" for b in bronnen]
    rubrieken = sorted({b["rubriek"] for b in bronnen})
    return "\n\n".join([f"# Wie Martijn is\n{nieuws.PROFIEL}",
                        "# Zijn bronnen voor overig nieuws\n" + ("\n".join(regels) or "(nog geen)"),
                        "Rubrieken die er al zijn: " + (", ".join(rubrieken) or "geen")])


def antwoord(c, gesprek_id, haal=None):
    bronnen = {r["id"] for r in c.execute("SELECT id FROM vera_bronnen").fetchall()}
    historie = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gesprek_id,)).fetchall()
    systeem = "\n\n".join([personas.systeemtekst(personas.get("vera")), UITLEG, context(c)])
    tekst = claude.gesprek(c, "vera", systeem, [{"role": x["rol"], "content": x["tekst"]} for x in historie if (x["tekst"] or "").strip()][-20:],
                           max_tokens=2000)
    tekst = tekst.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    tekst, voorstellen = voorstellen_uit(tekst, bronnen)
    if not tekst:
        tekst = "Hier is mijn voorstel." if voorstellen else "(Geen antwoord ontvangen. Probeer het nog eens.)"
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    for v in voorstellen[:8]:
        g = v["gegevens"]
        if v["soort"] == "nieuw" or (v["soort"] == "wijzig" and "url" in g):
            if v["soort"] == "wijzig":
                oud = c.execute("SELECT soort FROM vera_bronnen WHERE id = %s", (v["bron_id"],)).fetchone()
                g = test_bron({"soort": oud["soort"], **g}, haal) if oud else g
            else:
                g = test_bron(g, haal)
        c.execute("INSERT INTO vera_voorstellen (gesprek_id, soort, bron_id, gegevens) VALUES (%s, %s, %s, %s)",
                  (gesprek_id, v["soort"], v["bron_id"], json.dumps(g)))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst


def toepassen(c, v):
    """Een voorstel van Vera uitvoeren (na de klik). Geeft de uitslag in één zin."""
    g = v["gegevens"]
    if v["soort"] == "nieuw":
        soort = "google" if g.get("soort") == "google" else "feed"
        bid = c.execute("INSERT INTO vera_bronnen (naam, url, soort, rubriek) VALUES (%s, %s, %s, %s) RETURNING id",
                        (g["naam"], g["url"], soort, g.get("rubriek") or "Algemeen")).fetchone()["id"]
        return bid, f"Bron '{g['naam']}' toegevoegd. Vera haalt hem binnen een kwartier op."
    b = c.execute("SELECT * FROM vera_bronnen WHERE id = %s", (v["bron_id"],)).fetchone()
    if not b:
        return None, "Die bron bestaat niet meer."
    if v["soort"] == "weg":
        c.execute("DELETE FROM vera_bronnen WHERE id = %s", (b["id"],))
        return None, f"Bron '{b['naam']}' verwijderd."
    if v["soort"] in ("aan", "uit"):
        c.execute("UPDATE vera_bronnen SET actief = %s WHERE id = %s", (v["soort"] == "aan", b["id"]))
        return b["id"], f"Bron '{b['naam']}' staat {v['soort']}."
    velden = {k: g[k] for k in ("naam", "url", "rubriek") if g.get(k)}
    if g.get("site") and g.get("soort") == "feed":
        velden["soort"] = "feed"
    if velden:
        c.execute(f"UPDATE vera_bronnen SET {', '.join(f'{k} = %({k})s' for k in velden)}, fout = '' WHERE id = %(_id)s",
                  velden | {"_id": b["id"]})
    return b["id"], f"Bron '{velden.get('naam') or b['naam']}' aangepast."


def beschrijf(v, bron=None):
    """Wat een voorstel doet, in één regel voor de knop. Pure functie."""
    g = v["gegevens"]
    naam = (bron or {}).get("naam") or "bron"
    if v["soort"] == "nieuw":
        wat = "Google News: " + g["url"] if g.get("soort") == "google" else (g.get("url") or "")
        return f"Nieuwe bron: {g['naam']} ({g.get('rubriek') or 'Algemeen'}) · {wat}"
    if v["soort"] == "wijzig":
        delen = [f"{naam} aanpassen"] + [f"{k}: {g[k]}" for k in ("naam", "rubriek", "url") if g.get(k)]
        return " · ".join(delen)
    return {"aan": f"{naam} aanzetten", "uit": f"{naam} uitzetten", "weg": f"{naam} verwijderen"}[v["soort"]]


def gesprek_paneel(c, g=None):
    """Het gesprek met Vera over de bronnen: het gekozen of het laatste, met de open en uitgevoerde voorstellen."""
    if g:
        gs = c.execute("SELECT * FROM gesprekken WHERE id = %s AND persona = 'vera'", (g,)).fetchone()
    elif g is None:
        gs = c.execute("SELECT * FROM gesprekken WHERE persona = 'vera' AND NOT gearchiveerd ORDER BY bijgewerkt DESC LIMIT 1").fetchone()
    else:
        gs = None
    uit = {"gesprek": gs, "berichten": [], "voorstellen": [], "klaar": [],
           "eerdere": c.execute("""SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'vera' AND NOT gearchiveerd
                                   ORDER BY bijgewerkt DESC LIMIT 10""").fetchall()}
    if gs:
        uit["berichten"] = c.execute("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gs["id"],)).fetchall()
        for v in c.execute("""SELECT v.*, b.naam AS bron_naam FROM vera_voorstellen v LEFT JOIN vera_bronnen b ON b.id = v.bron_id
                              WHERE v.gesprek_id = %s ORDER BY v.id""", (gs["id"],)).fetchall():
            if v["status"] == "open":
                uit["voorstellen"].append(dict(v, tekst=beschrijf(v, {"naam": v["bron_naam"]})))
            elif v["status"] == "uitgevoerd":
                uit["klaar"].append(v)
    return uit


def sleutel_ok():
    return bool(config.ANTHROPIC_API_KEY)
