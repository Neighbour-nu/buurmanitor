"""Iris en het CRM: een kans of project voorstellen uit een mail, nieuwsbericht of socialpost, en voorstellen uit het
gesprek met Iris. Iris vult in, Martijn kijkt na en maakt aan: niets wordt aangemaakt zonder zijn klik."""
import datetime as dt
import json
import re

from ..kern import claude, config
from . import model

VELDEN = ("wat", "titel", "activiteit", "omschrijving", "organisatie", "contact_naam", "contact_email", "volgende_stap",
          "volgende_datum", "waarde", "datum", "bericht_id", "bron", "bron_titel", "bron_url")
TAG = re.compile(r"<crm>\s*(\{.*?\})\s*</crm>", re.S)

UITLEG = """Een kans of project in het CRM heeft deze velden (alles in het Nederlands, kort en zakelijk):
- wat: "kans" (iets wat nog binnengehaald of opgezet moet worden) of "project" (iets wat al doorgaat en uitgevoerd wordt)
- titel: kort, bijvoorbeeld "Workshop keeperstraining KNVB" of "Burendag 2027 Helmond"
- activiteit: een van deze codes: {activiteiten}
- omschrijving: twee of drie zinnen: waar gaat het om, wat is gevraagd of aangeboden
- organisatie: de organisatie aan de andere kant (leeg als onbekend)
- contact_naam en contact_email: de contactpersoon (leeg als onbekend)
- volgende_stap: de eerstvolgende concrete actie voor Martijn, bijvoorbeeld "Offerte sturen" of "Burgemeester mailen"
- volgende_datum: wanneer die stap moet (JJJJ-MM-DD), of leeg
- waarde: geschat bedrag in euro's als getal, alleen als het in de bron staat; anders leeg. Bij politiek altijd leeg.
- datum: bij een project de belangrijkste datum (bijvoorbeeld de dag van het evenement), JJJJ-MM-DD, of leeg"""


def _activiteiten_tekst(c):
    return ", ".join(f"{a['code']} ({a['naam']}{', politiek initiatief zonder geld' if a['soort'] == 'initiatief' else ''})"
                     for a in model.activiteiten(c))


def json_uit(tekst):
    """Het eerste JSON-object uit een antwoord. Pure functie."""
    t = tekst or ""
    begin, eind = t.find("{"), t.rfind("}")
    if begin < 0 or eind < begin:
        raise ValueError("geen JSON in het antwoord")
    return json.loads(t[begin:eind + 1])


def schoon(v, codes):
    """Alleen bekende velden, met geldige waarden. Pure functie."""
    v = {k: ("" if v.get(k) is None else str(v.get(k)).strip()) for k in VELDEN if k in v}
    if v.get("wat") not in ("kans", "project"):
        v["wat"] = "kans"
    if v.get("activiteit") not in codes:
        v.pop("activiteit", None)
    for k in ("volgende_datum", "datum"):
        if v.get(k) and not model.datum(v[k]):
            v[k] = ""
    if v.get("waarde") and model.bedrag(v["waarde"]) is None:
        v["waarde"] = ""
    if not v.get("bericht_id", "").isdigit():
        v.pop("bericht_id", None)
    return {k: w[:2000] for k, w in v.items()}


def invullen(c, bron):
    """Laat Iris een kans of project invullen op basis van de bron (dict met soort, titel, van, tekst, activiteit).
    Geeft een dict met velden (alleen wat Iris kon bepalen)."""
    codes = {a["code"] for a in model.activiteiten(c)}
    nu = dt.datetime.now(config.TZ)
    systeem = ("Je bent Iris, de coördinator in Buurmanitor, de persoonlijke assistent van Martijn Buurman (raadslid PRO Helmond, "
               "ZEROHERO keepersplatform en keeperscongres, Upper Spire, IV-professional). Je maakt van een bericht een kans of project "
               f"voor zijn CRM. Vandaag is {nu:%Y-%m-%d}.\n\n" + UITLEG.format(activiteiten=_activiteiten_tekst(c))
               + "\n\nAntwoord alleen met één JSON-object met deze velden, zonder uitleg.")
    inhoud = (f"Soort bron: {bron.get('soort', '')}\nVan: {bron.get('van', '')}\nTitel of onderwerp: {bron.get('titel', '')}\n"
              f"Mailbox of stroom: {bron.get('activiteit', '')}\n\nTekst:\n{(bron.get('tekst') or '')[:6000]}")
    tekst = claude.gesprek(c, "iris", systeem, [{"role": "user", "content": inhoud}], model=config.CLAUDE_MODEL_LICHT, max_tokens=700)
    return schoon(json_uit(tekst), codes)


def uitleg_voor_iris(c):
    """Wat Iris in het gesprek moet weten om een kans of project voor te stellen."""
    return ("# Kansen en projecten voorstellen\nVraagt Martijn je een kans of project aan te maken (bijvoorbeeld 'maak een kans van "
            "die mail van Jan'), zet dan in je antwoord een voorstel tussen <crm> en </crm>, als één JSON-object. Martijn ziet het "
            "voorstel als knop en maakt het zelf aan; zeg dus niet dat het al is aangemaakt. Gaat het om een mail uit de lijst van "
            "Bram, zet dan het mailnummer (de # achter de regel) in het veld bericht_id.\n"
            + UITLEG.format(activiteiten=_activiteiten_tekst(c)))


def uit_antwoord(c, tekst):
    """Haal de voorstellen uit een antwoord van Iris. Geeft (tekst zonder de tags, [voorstellen])."""
    codes = {a["code"] for a in model.activiteiten(c)}
    voorstellen = []
    for m in TAG.finditer(tekst or ""):
        try:
            voorstellen.append(schoon(json.loads(m.group(1)), codes))
        except (ValueError, TypeError):
            continue
    return TAG.sub("", tekst or "").strip(), voorstellen
