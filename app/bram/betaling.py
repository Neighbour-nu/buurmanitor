"""Betaalgegevens uit een factuurmail halen: bedrag, vervaldatum, factuurnummer en de betaallink.

Alleen uit wat er echt staat (de mail en de tekst van de pdf-bijlagen), zonder Claude: zo is de uitkomst voorspelbaar en
gaat er geen factuur naar buiten. Wat niet gevonden wordt, blijft leeg; de taak zegt dat dan. Pure functies.
"""
import datetime as dt
import html as htmlmod
import re

MAANDEN = {"januari": 1, "jan": 1, "january": 1, "februari": 2, "feb": 2, "february": 2, "maart": 3, "mrt": 3, "march": 3, "mar": 3,
           "april": 4, "apr": 4, "mei": 5, "may": 5, "juni": 6, "jun": 6, "june": 6, "juli": 7, "jul": 7, "july": 7, "augustus": 8,
           "aug": 8, "august": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "october": 10, "oct": 10,
           "november": 11, "nov": 11, "december": 12, "dec": 12}
_DATUM = (r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})|(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\s+(" + "|".join(sorted(MAANDEN, key=len, reverse=True))
          + r")\.?\s+(\d{4})")
DATUM = re.compile(_DATUM, re.I)
VERVAL = re.compile(r"(vervaldatum|verval\s*datum|uiterlijk(?:\s+op)?|betalen\s+(?:voor|vóór|v[oó][oó]r)|te\s+(?:voldoen|betalen)\s+(?:voor|vóór|uiterlijk)|"
                    r"betaal\s*datum|due\s*date|payment\s+due|pay\s+by|due\s+on|before)\s*:?\s*(?:op\s+)?(?:" + _DATUM + ")", re.I)
BINNEN = re.compile(r"binnen\s+(\d{1,3})\s+dagen|within\s+(\d{1,3})\s+days|betalingstermijn\s*:?\s*(\d{1,3})\s+dagen", re.I)
_BEDRAG = r"(?:€|eur|euro)\s*(-?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|-?\d+(?:[.,]\d{2}))|(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*(?:€|eur|euro)"
TOTAAL = re.compile(r"(te\s+betalen|totaal(?:\s*bedrag)?(?:\s+incl\.?(?:usief)?\s+btw)?|openstaand(?:\s+bedrag)?|factuurbedrag|amount\s+due|"
                    r"total(?:\s+due)?|bedrag)\s*:?\s*(?:" + _BEDRAG + ")", re.I)
BEDRAG = re.compile(_BEDRAG, re.I)
NUMMER = re.compile(r"(?:factuur\s*nummer|factuurnr\.?|factuur\s+nr\.?|factuur\s*#|invoice\s*(?:number|no\.?|#|nr\.?))\s*:?\s*([A-Z0-9][A-Z0-9/_-]{2,30})", re.I)
HREF = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
BETAAL_DOMEIN = re.compile(r"(ideal|tikkie|bunq\.me|mollie|buckaroo|adyen|multisafepay|sisow|pay\.nl|stripe|paypal|betaalverzoek|"
                           r"ing\.nl/payreq|rabobank\.nl/betaalverzoek|abnamro\.nl/betaalverzoek|checkout|/pay\b|/betaal|/payment|"
                           r"betalen|betaling)", re.I)
BETAAL_TEKST = re.compile(r"(nu\s+betalen|direct\s+betalen|betaal\s+(nu|direct|hier|online|met\s+ideal)|online\s+betalen|"
                          r"pay\s+(now|online|invoice)|betaalverzoek|ideal|tikkie)", re.I)
NIET = re.compile(r"(unsubscribe|afmelden|uitschrijven|privacy|voorwaarden|mailto:|tel:|facebook|linkedin|instagram|twitter|youtube|"
                  r"\.(png|jpe?g|gif|svg)(\?|$))", re.I)


def _datum(m, groep=0):
    """Een gevonden datum (uit DATUM of VERVAL, vanaf groep) als date, of None."""
    g = m.groups()[groep:groep + 9]
    try:
        if g[0]:
            d, mnd, j = int(g[0]), int(g[1]), int(g[2])
            return dt.date(j + 2000 if j < 100 else j, mnd, d)
        if g[3]:
            return dt.date(int(g[3]), int(g[4]), int(g[5]))
        if g[6]:
            return dt.date(int(g[8]), MAANDEN[g[7].lower()], int(g[6]))
    except (ValueError, KeyError):
        return None
    return None


def vervaldatum(tekst, ontvangen=None):
    """De datum waarop betaald moet zijn: een genoemde vervaldatum, of 'binnen N dagen' na ontvangst."""
    for m in VERVAL.finditer(tekst or ""):
        d = _datum(m, 1)
        if d:
            return d
    m = BINNEN.search(tekst or "")
    if m and ontvangen:
        return ontvangen + dt.timedelta(days=int(next(x for x in m.groups() if x)))
    return None


def _bedrag_tekst(ruw):
    """'1.234,50' of '1234.50' -> '€ 1.234,50'."""
    ruw = ruw.replace(" ", "")
    if re.fullmatch(r"-?\d+\.\d{2}", ruw):  # Engelse notatie
        ruw = ruw.replace(".", ",")
    heel, _, cent = ruw.partition(",")
    heel = heel.replace(".", "")
    try:
        heel = f"{int(heel):,}".replace(",", ".")
    except ValueError:
        return ""
    return f"€ {heel},{cent or '00'}"


def bedrag(tekst):
    """Het te betalen bedrag: liefst bij 'totaal' of 'te betalen'; anders het grootste bedrag in euro's."""
    kandidaten = [m for m in TOTAAL.finditer(tekst or "")]
    for m in reversed(kandidaten):  # het laatste 'totaal' is meestal het eindtotaal
        ruw = m.group(2) or m.group(3)
        if ruw and not ruw.startswith("-"):
            return _bedrag_tekst(ruw)
    alle = []
    for m in BEDRAG.finditer(tekst or ""):
        ruw = (m.group(1) or m.group(2) or "").replace(" ", "")
        if ruw and not ruw.startswith("-"):
            waarde = float(ruw.replace(".", "").replace(",", ".")) if "," in ruw else float(ruw)
            alle.append((waarde, ruw))
    return _bedrag_tekst(max(alle)[1]) if alle else ""


def factuurnummer(tekst):
    m = NUMMER.search(tekst or "")
    return m.group(1) if m else ""


def betaallink(html, tekst=""):
    """De beste betaallink: een knop of link met 'betalen' of 'iDEAL', of naar een betaaldienst. Alleen links die in de mail staan."""
    beste, score_beste = "", 0
    links = [(htmlmod.unescape(u), re.sub(r"<[^>]+>", " ", t)) for u, t in HREF.findall(html or "")]
    links += [(u, "") for u in URL.findall(tekst or "")]
    for url, ankertekst in links:
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")) or NIET.search(url) or NIET.search(ankertekst or ""):
            continue
        score = (3 if BETAAL_TEKST.search(ankertekst or "") else 0) + (2 if BETAAL_DOMEIN.search(url) else 0)
        if score > score_beste:
            beste, score_beste = url, score
    return beste[:1000]


def gegevens(tekst, html="", bijlagen=(), ontvangen=None):
    """Alles bij elkaar, uit de mail en de tekst van de bijlagen: {bedrag, vervaldatum, nummer, link}."""
    alles = "\n".join([tekst or "", *[b for b in bijlagen if b]])
    return {"bedrag": bedrag(alles), "vervaldatum": vervaldatum(alles, ontvangen), "nummer": factuurnummer(alles),
            "link": betaallink(html, tekst)}
