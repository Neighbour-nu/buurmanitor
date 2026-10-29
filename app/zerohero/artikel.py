"""Eigen artikelen: zelf geschreven teksten laten structureren en verbeteren, of een artikel laten schrijven vanuit een idee.

Route: invoer -> Claude maakt een voorstel (met wat er veranderde, suggesties en te controleren feiten) -> Martijn redigeert
in de cockpit, eventueel met extra rondes -> Maak concept. Dan volgt hetzelfde als bij nieuwsberichten: LinkedIn-post,
Instagram-tekst, SEO-velden, een concept in WordPress met het huisstijlbeeld, en de knoppen Voorbeeld, Publiceren en
Controleer de feiten op de conceptpagina. De tekst van het artikel zelf neemt de cockpit letterlijk over.
"""
import base64
import datetime as dt
import json
import re
import threading
import time
import traceback

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import claude_api, opmaak, wp
from .main import _huisstijl_png, _zet_beeld, controleer_herkomst, ctx, q, q1, templates

router = APIRouter()

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS artikelen (
        id serial PRIMARY KEY, aangemaakt timestamptz NOT NULL DEFAULT now(), bijgewerkt timestamptz NOT NULL DEFAULT now(),
        soort text NOT NULL, niveau text, invoer_titel text, invoer text, aanwijzing text,
        titel text, tekst text, wijzigingen jsonb, suggesties jsonb, te_controleren jsonb, bronnen jsonb,
        status text NOT NULL DEFAULT 'bezig', fout text, ronde int NOT NULL DEFAULT 0, concept_id int,
        tokens_in int NOT NULL DEFAULT 0, tokens_out int NOT NULL DEFAULT 0)""",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS artikel_id int",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS onderwerp text NOT NULL DEFAULT 'artikel'",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS wp_post_id int",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS wp_link text",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS wp_status text",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS wp_gewijzigd text",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS vaste_blokken jsonb",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS wp_opgeslagen timestamptz",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS bestaand_bericht boolean NOT NULL DEFAULT false",
    "ALTER TABLE artikelen ADD COLUMN IF NOT EXISTS zoekopdrachten int",
    "CREATE TABLE IF NOT EXISTS instellingen (sleutel text PRIMARY KEY, waarde text, bijgewerkt timestamptz NOT NULL DEFAULT now())",
    """CREATE TABLE IF NOT EXISTS artikel_bestanden (
        id serial PRIMARY KEY, artikel_id int NOT NULL, naam text, mime text, grootte int, data bytea, tekst text,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
]

NIVEAUS = {
    "geen": "Niet aanpassen: mijn tekst gaat zo door naar het concept",
    "licht": "Licht: structuur, tussenkoppen, spelling en zinsbouw. Mijn woorden blijven staan",
    "stevig": "Stevig: ook herschrijven voor ritme en helderheid in de ZEROHERO-toon",
}
LENGTES = {"kort": "600 tot 800 woorden", "middel": "800 tot 1100 woorden", "lang": "1100 tot 1500 woorden"}
ONDERWERPEN = {
    "artikel": "Keeperartikel (analyse, verhaal, achtergrond)",
    "congres": "ZEROHERO Keeperscongres (spreker, programma, aankondiging)",
    "zerohero": "ZEROHERO-nieuws (boek, lezing, podcast, samenwerking)",
    "aankondiging": "Aankondiging (lezing, optreden, aflevering, actie, evenement)",
    "persbericht": "Persbericht (voor media en redacties)",
    "marketing": "Marketingtekst (promotie boek of lezing, nieuwsbrief, partner- of sponsoraanbod)",
}
ONDERWERP_GROEPEN = [("Redactioneel", ["artikel", "congres", "zerohero"]), ("Marketing", ["aankondiging", "persbericht", "marketing"])]
MARKETING = {"aankondiging", "persbericht", "marketing"}
ONDERWERP_REGELS = {
    "artikel": """

Soort tekst: een keeperartikel. Puur sport: geen verwijzingen naar het bedrijfsleven of naar het boek De Keeper-mindset, tenzij Martijn dat zelf in zijn tekst doet. Geen meningen van Martijn of ZEROHERO; wel perspectief en algemene inzichten.""",
    "congres": """

Soort tekst: een aankondiging of artikel over het ZEROHERO Keeperscongres (bijvoorbeeld een spreker, het programma, praktische informatie of een terugblik). Dit gaat voor op de aanwijzingen over opbouw hierboven.
- Doel: mensen enthousiast maken en kaarten laten kopen, zonder schreeuwerig te worden. Wervend mag, maar met de ZEROHERO-toon: inhoud en verhaal eerst, geen marketingclichés ('unieke kans', 'mis het niet', 'niet te missen').
- Bij een spreker: wie is het, wat maakt het verhaal van deze persoon bijzonder voor keepers en voor iedereen die de keeper-mindset herkent, waar gaat de bijdrage over, en waarom past dat bij het congres. Feiten over de spreker alleen uit Martijns tekst, zijn bestanden of bevestigde bronnen.
- Bij het programma: een heldere opbouw van de dag (tijden en onderdelen als lijst of tabel), met per onderdeel één of twee zinnen.
- Gebruik de vaste congresgegevens hieronder letterlijk (datum, plaats, kaartverkoop). Verzin geen tijden, prijzen, namen of onderdelen die er niet staan; ontbreekt iets, zet het bij te_controleren.
- Eindig met een korte, uitnodigende alinea met waar en hoe je kaarten koopt. Geen vraag aan de lezer als slot.
- Lengte: liever compact (300 tot 600 woorden), tenzij Martijn iets anders vraagt.""",
    "zerohero": """

Soort tekst: nieuws over ZEROHERO zelf (een boek, een lezing, de podcast Keepersjournaal, een samenwerking of een mijlpaal). Dit gaat voor op de aanwijzingen over opbouw hierboven.
- Verwijzen naar de boeken, lezingen en het bedrijfsleven mag hier, als het past.
- Vertel het als een verhaal met een keeperhaak, niet als persbericht. Wervend mag, zonder marketingclichés.
- Feiten over ZEROHERO alleen uit Martijns tekst, zijn bestanden of de vaste gegevens hieronder. Ontbreekt iets, zet het bij te_controleren.
- Eindig met een concrete vervolgstap voor de lezer (lezing boeken, aflevering luisteren, boek bestellen), zonder vraag als slot.
- Lengte: liever compact (300 tot 700 woorden), tenzij Martijn iets anders vraagt.""",
    "aankondiging": """

Soort tekst: een aankondiging (bijvoorbeeld een lezing of optreden van Martijn, een nieuwe aflevering van Keepersjournaal, een boekpresentatie, een actie of een evenement). Dit gaat voor op de aanwijzingen over opbouw hierboven.
- Doel: mensen in actie krijgen (aanmelden, kaarten kopen, luisteren, bestellen), zonder schreeuwerig te worden. Inhoud en verhaal eerst, met een keeperhaak.
- Opbouw: een sterke openingszin die zegt wat er komt en waarom het de moeite waard is; dan de praktische kern (wat, wie, wanneer, waar, voor wie, prijs en aanmelden), bij voorkeur als korte lijst; dan één of twee alinea's die het verhaal erachter vertellen; tot slot een duidelijke oproep met waar en hoe.
- Geen marketingclichés ('unieke kans', 'mis het niet', 'niet te missen', 'exclusief', 'last minute'). Geen kunstmatige schaarste of urgentie die niet klopt.
- Feiten (datum, tijd, plaats, prijs, namen) alleen uit Martijns tekst, zijn bestanden of de vaste gegevens hieronder. Ontbreekt iets, schrijf dan [vul in: ...] in de tekst en zet het bij te_controleren.
- Eindig met de oproep, niet met een vraag.
- Lengte: 150 tot 400 woorden, tenzij Martijn iets anders vraagt.""",
    "persbericht": """

Soort tekst: een persbericht voor media en redacties. Dit gaat voor op de aanwijzingen over opbouw en toon hierboven: een persbericht is zakelijk en informatief, niet filosofisch of beschouwend.
- Schrijf in de derde persoon (ZEROHERO, Martijn Buurman), niet in de ik-vorm.
- De titel is een feitelijke nieuwskop van maximaal twaalf woorden: wat is er nieuw.
- De tekst begint met een plaats- en datumregel in de vorm **Plaats, datum** gevolgd door de lead: één alinea van maximaal vijftig woorden die wie, wat, wanneer, waar en waarom beantwoordt.
- Daarna de omgekeerde piramide: belangrijkste informatie eerst, achtergrond later. Korte alinea's, geen tussenkoppen tenzij het bericht lang is.
- Precies één citaat van Martijn, als een eigen alinea tussen aanhalingstekens met 'zegt Martijn Buurman, oprichter van ZEROHERO'. Gebruik een citaat dat Martijn zelf aanleverde. Leverde hij er geen, schrijf dan een voorstel en zet bij te_controleren dat het citaat een voorstel is dat hij moet goedkeuren of vervangen. Andere personen citeer je alleen als hun citaat in zijn tekst of bestanden staat.
- Geen superlatieven of wervende taal ('uniek', 'baanbrekend', 'de beste'). Feiten en cijfers alleen uit Martijns tekst, zijn bestanden of bevestigde bronnen.
- Sluit af met twee slotdelen, elk met een tussenkop:
  ## Over ZEROHERO
  de standaardalinea uit de persinformatie hieronder, letterlijk
  ## Noot voor de redactie (niet voor publicatie)
  de contactgegevens en het beeldmateriaal uit de persinformatie hieronder, letterlijk. Staat er '(vul in)', neem dan [vul in: ...] over en zet het bij te_controleren. Verzin nooit telefoonnummers, e-mailadressen of links.
- Lengte van het bericht zelf (zonder de twee slotdelen): 250 tot 450 woorden.""",
    "marketing": """

Soort tekst: een marketingtekst (bijvoorbeeld promotie van een boek of lezing, een tekst voor de nieuwsbrief of een landingspagina, of een aanbod aan partners, sponsors of standhouders). Dit gaat voor op de aanwijzingen over opbouw hierboven.
- Schrijf voor de doelgroep die Martijn noemt; noemt hij er geen, kies dan de meest logische en zeg welke je koos in wijzigingen.
- Opbouw: begin bij de lezer (zijn vraag, probleem of ambitie), laat zien wat ZEROHERO daarvoor biedt, maak het concreet met wat het oplevert, onderbouw met feiten, en eindig met één duidelijke vervolgstap.
- Behoud de ZEROHERO-toon: rustig, inhoudelijk, met een keeperhaak of paradox. Overtuigen door inhoud, niet door druk. Geen marketingclichés, geen overdreven beloften, geen kunstmatige schaarste.
- Verzin geen klantervaringen, recensies, citaten, aantallen deelnemers of resultaten. Gebruik alleen wat in Martijns tekst, zijn bestanden of de vaste gegevens staat; wil je sociale bewijskracht en ontbreekt die, zet dan [vul in: ...] en noem het bij te_controleren.
- Prijzen, data en voorwaarden alleen uit de vaste gegevens of Martijns tekst.
- Eindig met de oproep, niet met een vraag.
- Lengte: 200 tot 600 woorden, tenzij Martijn iets anders vraagt.""",
}
ONDERWERP_SOCIAAL = {
    "artikel": "",
    "congres": "\n\nDit gaat over het ZEROHERO Keeperscongres. LinkedIn en Instagram mogen wervend zijn: noem datum en plaats, en sluit LinkedIn af met een korte oproep om kaarten te kopen via de link in de eerste reactie (in plaats van een vraag). Instagram: de laatste alinea luidt 'Kaarten via de link in bio.' Gebruik naast #KeeperMindset #ZEROHERO ook #Keeperscongres.",
    "zerohero": "\n\nDit is ZEROHERO-nieuws. LinkedIn mag eindigen met een concrete oproep (link in de eerste reactie) in plaats van een vraag.",
    "aankondiging": "\n\nDit is een aankondiging. LinkedIn: begin met wat er komt en waarom het de moeite waard is, noem datum en plaats, en eindig met een korte oproep (aanmelden of kaarten via de link in de eerste reactie) in plaats van een vraag. Instagram: de laatste alinea luidt 'Meer info en aanmelden via de link in bio.' Geen marketingclichés.",
    "persbericht": "\n\nDit is een persbericht. Verander het persbericht niet. LinkedIn: een post in de ik-vorm van Martijn die het nieuws persoonlijk en kort vertelt (100 tot 180 woorden), zonder de noot voor de redactie, en eindigt met een verwijzing naar het hele bericht via de link in de eerste reactie in plaats van een vraag. Instagram: kort, het nieuws in twee zinnen, laatste alinea 'Het hele bericht via de link in bio.' kernzin: de nieuwskop of de sterkste zin uit het citaat. grafiek: null. verificatie: vooral data, namen en cijfers.",
    "marketing": "\n\nDit is een marketingtekst. LinkedIn: vertel het verhaal achter het aanbod in de ik-vorm, met een keeperhaak, en eindig met één concrete oproep (link in de eerste reactie) in plaats van een vraag. Instagram: kort, de kern en 'Meer via de link in bio.' Geen marketingclichés en geen beloften die niet in de tekst staan.",
}
VASTE_INFO = {
    "congres": ("Keeperscongres", """ZEROHERO Keeperscongres
Datum: woensdag 3 februari 2027
Tijden: (vul in)
Plaats: Apenheul, Apeldoorn
Thema: de keeper-mindset in alle sporten (voetbal, handbal, hockey, ijshockey, waterpolo, lacrosse)
Voor wie: keepers, keeperstrainers, coaches, ouders en iedereen die de keeper-mindset herkent
Kaarten: via Ticket Tailor, link: (vul hier de link in)
Prijzen: (vul in)
Sprekers: (vul in, per spreker één regel)
Programma: (vul in)"""),
    "pers": ("Persinformatie", """Over ZEROHERO (standaardalinea onder elk persbericht):
ZEROHERO (zerohero.one) is het Nederlandstalige platform voor keepers en goalies uit alle sporten: voetbal, handbal, hockey, ijshockey, waterpolo en lacrosse. Oprichter Martijn Buurman schreef de boeken ZEROHERO (2024) en De Keeper-mindset (2026), maakt met keepersspecialist Ruud Doevendans de podcast Keepersjournaal en organiseert het ZEROHERO Keeperscongres.

Contactpersoon voor de pers: Martijn Buurman
E-mail: (vul in)
Telefoon: (vul in)
Beeldmateriaal: (vul in, bijvoorbeeld een link naar de persmap met foto's en logo's)
Plaats in de datumregel: (vul in, bijvoorbeeld Helmond)"""),
    "zerohero": ("ZEROHERO algemeen", """ZEROHERO (zerohero.one): het platform voor keepers en goalies uit alle sporten. Want wie de nul houdt, is de stille held van elk team.
Oprichter: Martijn Buurman, auteur van ZEROHERO (2024) en De Keeper-mindset (2026).
De Keeper-mindset: over de Keeper (proactief, risicobewust, crises voorkomen) en de Scorer (zichtbaar, gericht op groei). Voorwoord Hans van Breukelen.
Lezingen: over De Keeper-mindset, voor MT-dagen, kick-offs, risk- en complianceteams, IT- en kwaliteitsafdelingen. Boeken via: (vul in)
Podcast en video: Keepersjournaal, met keepersspecialist Ruud Doevendans.
Kanalen: LinkedIn, Instagram, Facebook, YouTube (@ZEROHERO-ONE), podcastplatforms en de nieuwsbrief."""),
}


def vaste_info(sleutel):
    r = q1("SELECT waarde FROM instellingen WHERE sleutel = %s", (f"artikel_info_{sleutel}",))
    return r["waarde"] if r and r["waarde"] is not None else VASTE_INFO[sleutel][1]


def _vaste_gegevens_marketing(onderwerp):
    delen = ["ZEROHERO algemeen:\n" + vaste_info("zerohero"), "Keeperscongres:\n" + vaste_info("congres")]
    if onderwerp == "persbericht":
        delen.insert(0, "Persinformatie:\n" + vaste_info("pers"))
    return "\n\n".join(delen)


def onderwerp_regels(onderwerp):
    regels = ONDERWERP_REGELS.get(onderwerp, ONDERWERP_REGELS["artikel"])
    if onderwerp in MARKETING:
        return regels + ("\n\nVaste gegevens (door Martijn bijgehouden, gebruik ze letterlijk; '(vul in)' betekent: nog onbekend, niet verzinnen):\n"
                         + _vaste_gegevens_marketing(onderwerp))
    if onderwerp in ("congres", "zerohero"):
        regels += "\n\nVaste gegevens (door Martijn bijgehouden, gebruik ze letterlijk; '(vul in)' betekent: nog onbekend, niet verzinnen):\n" + vaste_info(onderwerp)
        if onderwerp == "congres":
            regels += "\n\nAchtergrond ZEROHERO:\n" + vaste_info("zerohero")
    return regels


VOORBEELD_SLUGS = ["hoe-vaak-is-de-beste-speler-een-keeper", "keepers-en-goalies-verdienen-meer-3", "lev-yashin-ballon-dor-keeper-gouden-bal"]

TOON = """Je werkt voor ZEROHERO (zerohero.one), het Nederlandstalige platform van Martijn Buurman voor keepers en goalies uit alle sporten: voetbal, handbal, hockey, ijshockey, waterpolo en lacrosse. Kernfilosofie: de keeper als stille held, de onzichtbare winnaar wiens beste werk is wat nooit gebeurt.

Harde regels:
- Nooit gedachtestreepjes (geen — en geen –). Gebruik een komma, punt, dubbele punt of haakjes. Koppeltekens in samenstellingen (WK-finale, shoot-out) mogen wel.
- Verzin geen feiten, cijfers, data of citaten. Citaten uit anderstalige bronnen breng je als parafrase of kort vertaald citaat.
- Gevoelige onderwerpen (overlijden, depressie, verslaving, ernstige ziekte): geen methode of omstandigheden, geen simpele oorzaak-gevolg, geen romantisering. Bij zelfdoding of depressie: vermeld 113 Zelfmoordpreventie (113 of 0800-0113, chat via 113.nl).
- Lev Yashin schrijf je altijd zo (niet Jasjin).
- Eindig nooit met een vraag aan de lezer. Het slot is een stellige, rustige slotzin.

Tone of voice: direct, filosofisch, paradox-gedreven Nederlands. Vooral lopende, goed opgebouwde zinnen in doorlopende alinea's; een korte, krachtige zin alleen af en toe als accent. Geen corporate vulling, geen AI-achtige verbindingszinnen ("laten we eerlijk zijn", "het is belangrijk om"). Ingehouden emotie, gedragen door feiten. Warm, nooit sentimenteel. Respect voor de keeper en het vak voorop. Nederlands perspectief waar mogelijk, cross-sport-verbanden, thema's als de asymmetrie van erkenning, de onzichtbare overwinning, de late bloeier, voorbereiding boven talent. Sanity metrics (reddingspercentage, tegengoals per 90 minuten) boven vanity metrics. Liever een nuchtere, prikkelende titel dan een hoogdravende.

Opmaak van de artikeltekst (Martijn redigeert die in een gewoon tekstvak):
- ## voor een tussenkop (kort en informatief, zoals "Europa: vijf competities, vijf prijzen")
- een lege regel tussen alinea's
- - voor een opsomming, 1. voor een genummerde lijst
- | kolom | kolom | voor een tabel, eerste regel is de kop
- **vet** en *cursief* spaarzaam
Geen HTML, geen titel bovenaan in de tekst (de titel is een apart veld)."""

ANTWOORD = """Antwoord met alleen één JSON-object, zonder uitleg ervoor of erna. Gebruik binnen teksten geen dubbele aanhalingstekens maar enkele, zodat de JSON geldig blijft:
{"titel": "...", "tekst": "het hele artikel in de opmaak hierboven", "wijzigingen": ["wat je veranderde en waarom, kort"], "suggesties": ["inhoudelijke suggestie voor Martijn"], "te_controleren": ["bewering die Martijn moet nalopen"], "bronnen": [{"titel": "...", "url": "https://..."}]}
Maximaal 8 wijzigingen, 6 suggesties en 8 te controleren beweringen, elk hooguit 25 woorden."""

VERBETER = TOON + """

Je bent de eindredacteur van Martijn. Je krijgt een artikel dat hij zelf schreef. Het blijft zijn artikel: behoud zijn stem, zijn inhoud, zijn voorbeelden, zijn standpunten en zoveel mogelijk zijn eigen formuleringen.
Niveau licht: verbeter de structuur (logische volgorde, korte informatieve tussenkoppen, alinea's die één gedachte uitwerken), spelling, grammatica en zinsbouw, en haal herhaling weg. Voeg geen inhoud toe.
Niveau stevig: daarnaast mag je zinnen herschrijven voor ritme en helderheid in de ZEROHERO-toon en een sterkere opening en slotzin maken. Ook dan geen nieuwe feiten.
Voeg nooit feiten, cijfers of citaten toe die niet in zijn tekst staan. Mist er volgens jou iets, of lijkt iets niet te kloppen, zet dat dan bij suggesties of te_controleren, niet in de tekst. Zoek niets op; bronnen blijft een lege lijst.

""" + ANTWOORD

SCHRIJF = TOON + """

Martijn geeft je een idee voor een artikel. Schrijf er een volledig artikel over dat hij daarna redigeert, in de stijl van zijn eigen artikelen op zerohero.one (zie de voorbeelden). Kenmerken van die artikelen: een beeldende opening die naar een prikkelende vraag of stelling leidt, korte informatieve tussenkoppen, veel concrete en bevestigde cijfers (jaartallen, aantallen, percentages), een tabel als die het verhaal helpt, vergelijkingen tussen sporten en landen, en een slotdeel ('Wat opvalt' of vergelijkbaar) met een rustige, stellige slotzin.
Zoek elk feit, cijfer en citaat op met de zoektool voordat je het gebruikt, bij voorkeur bij twee bronnen. Wat je niet kunt bevestigen, laat je weg of zet je bij te_controleren. Geef de gebruikte bronnen mee. Bij wijzigingen leg je in een paar regels uit welke opbouw en invalshoek je koos.

""" + ANTWOORD

HERZIE = TOON + """

Martijn heeft een artikel (dat hij misschien al heeft aangepast) en geeft een aanwijzing voor een volgende ronde. Voer die aanwijzing uit en laat de rest van zijn tekst zoveel mogelijk staan. Voeg geen feiten toe die je niet kunt bevestigen; heb je de zoektool, gebruik die alleen als de aanwijzing om nieuwe feiten vraagt.
Regels als [[blok 3: afbeelding, ...]] zijn vaste onderdelen van een bestaand bericht op de site (foto's, video's, knoppen). Neem ze letterlijk en elk op een eigen regel over, op dezelfde plek in het verhaal, tenzij de aanwijzing vraagt ze te verplaatsen of weg te laten.

""" + ANTWOORD

SOCIAAL = TOON + """

Martijn heeft het artikel hieronder geschreven en goedgekeurd. Verander het artikel niet. Maak er alleen de teksten en velden bij om het te plaatsen en te delen:
- linkedin: een LinkedIn-post van 150 tot 250 woorden. Vraag of paradox als opener, eventueel een paar regels met een pijl en een emoji voor de sterkste cijfers uit het artikel, een korte alinea met het inzicht, een afsluitende vraag, en de hashtags #KeeperMindset #ZEROHERO plus één of twee passende. Geen links in de tekst (die gaan in de comments).
- instagram: drie alinea's van elk één of twee zinnen. De eerste eindigt op de paradox, de laatste luidt: "Het hele verhaal lees je op LinkedIn en zerohero.one (link in bio)." Daarna hashtags. Eén emoji mag.
- sporten: gekozen uit voetbal, handbal, hockey, ijshockey, waterpolo, anders. Gaat het over meer dan twee sporten: ["alle"].
- seo_titel: maximaal 55 tekens, met het focus keyword vooraan.
- meta_beschrijving: 120 tot 155 tekens, wekt nieuwsgierigheid, bevat het focus keyword.
- focus_keyword: twee tot vier woorden waarop iemand zou zoeken.
- slug: korte url-tekst in kleine letters met koppeltekens, maximaal 6 woorden.
- samenvatting_kort: 1 of 2 zinnen voor het overzicht op de site.
- kernzin: de sterkste zin uit het artikel, letterlijk overgenomen, maximaal 110 tekens. Komt groot op het uitgelichte beeld.
- grafiek: alleen als het artikel echt om cijfers draait en die cijfers in de tekst staan: {"titel": "korte titel", "eenheid": "% of leeg", "reeksen": [{"label": "kort label", "waarde": 44, "accent": true}]}, maximaal 6 reeksen. Anders null.
- foto_zoek: twee of drie zoektermen om een passende foto te vinden.
- verificatie: maximaal 8 controleerbare beweringen uit het artikel in de vorm "bewering: waar na te gaan", elk hooguit 20 woorden. Zoek niets op.

Antwoord met alleen één JSON-object:
{"linkedin": "...", "instagram": "...", "sporten": ["..."], "seo_titel": "...", "meta_beschrijving": "...", "focus_keyword": "...", "slug": "...", "samenvatting_kort": "...", "kernzin": "...", "grafiek": null, "foto_zoek": ["..."], "verificatie": ["..."]}"""

CAT = {"voetbal": 32, "handbal": 34, "hockey": 31, "ijshockey": 30, "waterpolo": 33, "anders": 35, "alle": 36}
_voorbeelden = {"tijd": 0, "tekst": ""}


def _lijst(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [v]
    return v if isinstance(v, list) else []


def voorbeelden():
    """Twee eigen artikelen van zerohero.one als stijlvoorbeeld (een dag bewaard)."""
    if time.time() - _voorbeelden["tijd"] < 86400 and _voorbeelden["tekst"]:
        return _voorbeelden["tekst"]
    delen = []
    for slug in VOORBEELD_SLUGS[:2]:
        try:
            r = wp._wp("GET", f"/wp/v2/posts?slug={slug}&_fields=title,content")
            if r:
                titel = re.sub("<[^>]+>", "", r[0]["title"]["rendered"])
                delen.append(f"<voorbeeld titel=\"{titel}\">\n{opmaak.uit_html(r[0]['content']['rendered'])[:7000]}\n</voorbeeld>")
        except Exception:  # noqa: BLE001
            continue
    _voorbeelden.update(tijd=time.time(), tekst="\n\n".join(delen))
    return _voorbeelden["tekst"]


def _schoon_resultaat(o):
    o["titel"] = opmaak.zonder_streepjes(str(o.get("titel") or "")).strip()
    o["tekst"] = opmaak.zonder_streepjes(str(o.get("tekst") or "")).strip()
    for k in ("wijzigingen", "suggesties", "te_controleren"):
        o[k] = [opmaak.zonder_streepjes(str(x)) for x in _lijst(o.get(k))][:10]
    o["bronnen"] = [b for b in _lijst(o.get("bronnen")) if isinstance(b, dict)
                    and (str(b.get("url") or "").startswith("http") or str(b.get("titel") or "").startswith("Bestand"))][:15]
    return o


def _achtergrond(functie, *args):
    def run():
        try:
            functie(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=run, daemon=True).start()


def _werk_artikel(aid, soort, aanwijzing=""):
    """Laat Claude een voorstel maken (verbeteren, schrijven of een nieuwe ronde) en slaat het op."""
    a = q1("SELECT * FROM artikelen WHERE id = %s", (aid,))
    vandaag = dt.date.today().strftime("%d-%m-%Y")
    try:
        if soort == "verbeter":
            system = VERBETER
            user = (f"Niveau: {a['niveau']}.\n" + (f"Aanwijzing van Martijn: {a['aanwijzing']}\n" if a["aanwijzing"] else "")
                    + f"\nTitel (voorstel van Martijn, mag beter): {a['invoer_titel'] or '(geen)'}\n\nTEKST VAN MARTIJN:\n{a['invoer']}")
            zoeken, model = 0, None
        elif soort == "schrijf":
            vb = voorbeelden() if a["onderwerp"] == "artikel" else ""
            system = SCHRIJF + (("\n\nVoorbeelden van eigen artikelen van Martijn (alleen voor stijl en opbouw, neem geen inhoud over):\n" + vb) if vb else "")
            user = (f"Vandaag is het {vandaag}.\nGewenste lengte: {LENGTES.get(a['niveau'], LENGTES['middel'])}.\n"
                    + (f"Werktitel: {a['invoer_titel']}\n" if a["invoer_titel"] else "")
                    + f"\nIDEE VAN MARTIJN:\n{a['invoer']}\n" + (f"\nAANWIJZINGEN:\n{a['aanwijzing']}" if a["aanwijzing"] else ""))
            zoeken, model = 6, None
        else:
            system = HERZIE
            user = (f"Vandaag is het {vandaag}.\nAANWIJZING VOOR DEZE RONDE:\n{aanwijzing}\n\nHUIDIGE TITEL: {a['titel']}\n\nHUIDIGE TEKST:\n{a['tekst']}")
            zoeken, model = (3 if a["soort"] in ("idee", "bestaand") else 0), None
        system += onderwerp_regels(a["onderwerp"])
        bronnen = bron_blokken(aid)
        if bronnen:
            system += BRONREGEL
            if soort == "verbeter":
                system += " Bij het verbeteren van zijn eigen tekst gebruik je de bestanden alleen om te_controleren en suggesties te vullen."
        inhoud = bronnen + [{"type": "text", "text": user}] if bronnen else user
        tekst, usage = claude_api.vraag(system, inhoud, model=model, max_tokens=16000, zoeken=zoeken)
        o = _schoon_resultaat(claude_api.json_uit(tekst, "titel"))
        if not o["tekst"]:
            raise ValueError("Claude gaf geen artikeltekst terug.")
        if soort == "ronde" and a.get("bronnen"):
            oud = _lijst(a["bronnen"])
            gezien = {b.get("url") for b in o["bronnen"]}
            o["bronnen"] = o["bronnen"] + [b for b in oud if b.get("url") not in gezien]
        q("""UPDATE artikelen SET status = 'klaar', titel = %s, tekst = %s, wijzigingen = %s::jsonb, suggesties = %s::jsonb,
               te_controleren = %s::jsonb, bronnen = %s::jsonb, fout = NULL, ronde = ronde + 1, bijgewerkt = now(),
               tokens_in = tokens_in + %s, tokens_out = tokens_out + %s, zoekopdrachten = coalesce(zoekopdrachten, 0) + %s
               WHERE id = %s RETURNING 1""",
          (o["titel"] or a.get("titel") or a.get("invoer_titel"), o["tekst"], json.dumps(o["wijzigingen"]), json.dumps(o["suggesties"]),
           json.dumps(o["te_controleren"]), json.dumps(o["bronnen"]), usage["input_tokens"], usage["output_tokens"],
           usage.get("zoekopdrachten", 0), aid))
    except Exception as e:  # noqa: BLE001
        q("UPDATE artikelen SET status = 'fout', fout = %s, bijgewerkt = now() WHERE id = %s RETURNING 1", (str(e)[:500], aid))


def _categorieen(sporten, onderwerp="artikel"):
    """Basiscategorie: Blog voor keeperartikelen, een congrescategorie voor het congres (als die bestaat), anders Nieuws."""
    basis, gevonden = 2, {}
    try:
        for c in wp._wp("GET", "/wp/v2/categories?per_page=100&_fields=id,name,slug"):
            naam = f"{c.get('slug') or ''} {c.get('name') or ''}".lower()
            if "blog" in naam:
                gevonden.setdefault("artikel", c["id"])
            if "congres" in naam:
                gevonden.setdefault("congres", c["id"])
            if "zerohero" in naam:
                gevonden.setdefault("zerohero", c["id"])
            if re.search(r"\bpers(bericht|berichten)?\b", naam):
                gevonden.setdefault("persbericht", c["id"])
    except Exception:  # noqa: BLE001
        pass
    if onderwerp in ("aankondiging", "marketing"):
        basis = gevonden.get("zerohero", basis)
    elif onderwerp == "persbericht":
        basis = gevonden.get("persbericht", gevonden.get("zerohero", basis))
    else:
        basis = gevonden.get(onderwerp, basis)
    sport = [] if onderwerp == "congres" or onderwerp in MARKETING else [CAT[s] for s in sporten if s in CAT]
    return list(dict.fromkeys([basis, *sport]))


def _maak_concept(aid, cid):
    """Social-teksten en SEO laten maken, het concept in WordPress zetten met het huisstijlbeeld, en alles opslaan."""
    a = q1("SELECT * FROM artikelen WHERE id = %s", (aid,))
    try:
        titel, tekst = a["titel"], a["tekst"]
        onderwerp = a.get("onderwerp") or "artikel"
        sys_sociaal = SOCIAAL + ONDERWERP_SOCIAAL.get(onderwerp, "")
        if onderwerp in ("congres", "zerohero"):
            sys_sociaal += "\n\nVaste gegevens:\n" + vaste_info(onderwerp)
        elif onderwerp in MARKETING:
            sys_sociaal += "\n\nVaste gegevens:\n" + _vaste_gegevens_marketing(onderwerp)
        antwoord, usage = claude_api.vraag(sys_sociaal, f"TITEL: {titel}\n\nARTIKEL:\n{tekst}", max_tokens=6000)
        o = claude_api.json_uit(antwoord, "linkedin")
        g = lambda k: opmaak.zonder_streepjes(str(o.get(k) or "")).strip()  # noqa: E731
        sporten = [re.sub(r"[^a-z]", "", str(s).lower()) for s in _lijst(o.get("sporten"))]
        sport = "alle sporten" if ("alle" in sporten or len(sporten) > 2) else (sporten[0] if sporten else "")
        grafiek = o.get("grafiek") if isinstance(o.get("grafiek"), dict) and _lijst((o.get("grafiek") or {}).get("reeksen")) else None
        blog_html = opmaak.naar_html(tekst)
        # Eerst de teksten opslaan (status blijft 'bezig'), pas na WordPress en het beeld op 'klaar'.
        c = q1("""UPDATE concepten SET blog_titel = %s, blog_html = %s, linkedin = %s, instagram = %s,
                    verificatie = %s::jsonb, kernzin = %s, grafiek = %s::jsonb, foto_zoek = %s::jsonb, sport = %s, model = %s,
                    tokens_in = %s, tokens_out = %s, klaar_op = now(), fout = NULL WHERE id = %s RETURNING *""",
                  (titel, blog_html, g("linkedin"), g("instagram"), json.dumps([opmaak.zonder_streepjes(str(x)) for x in _lijst(o.get("verificatie"))][:8]),
                   g("kernzin")[:160] or titel, json.dumps(grafiek) if grafiek else None, json.dumps(_lijst(o.get("foto_zoek"))[:3]),
                   sport, claude_api.SCHRIJFMODEL, usage["input_tokens"], usage["output_tokens"], cid))
        if wp.wp_actief():
            slug = re.sub(r"[^a-z0-9]+", "-", g("slug").lower()).strip("-")[:70]
            post = {"title": titel, "content": opmaak.naar_wp(tekst), "status": "draft", "categories": _categorieen(sporten, onderwerp),
                    "excerpt": g("samenvatting_kort"), "comment_status": "open"}
            if slug:
                post["slug"] = slug
            try:
                p = wp._wp("POST", "/wp/v2/posts", data=post)
                try:
                    wp._wp("POST", "/rankmath/v1/updateMeta", data={"objectType": "post", "objectID": p["id"], "meta": {
                        "rank_math_title": (g("seo_titel") or titel) + " %sep% %sitename%",
                        "rank_math_description": g("meta_beschrijving") or g("samenvatting_kort"),
                        "rank_math_focus_keyword": g("focus_keyword")}})
                except Exception:  # noqa: BLE001
                    pass
                c = q1("UPDATE concepten SET wp_post_id = %s, wp_link = %s WHERE id = %s RETURNING *", (p["id"], p.get("link") or "", cid))
                naam = (slug or f"artikel-{aid}")[:60]
                try:
                    _zet_beeld(c, _huisstijl_png(c), "image/png", f"{naam}.png", c.get("kernzin") or titel, "", "huisstijl")
                except Exception as e:  # noqa: BLE001
                    q("UPDATE concepten SET fout = %s WHERE id = %s RETURNING 1", (f"Uitgelicht beeld niet gelukt: {str(e)[:200]}", cid))
            except Exception as e:  # noqa: BLE001
                q("UPDATE concepten SET fout = %s WHERE id = %s RETURNING 1", (f"WordPress: {str(e)[:250]}", cid))
        else:
            q("UPDATE concepten SET fout = 'De cockpit heeft geen WordPress-gegevens.' WHERE id = %s RETURNING 1", (cid,))
        q("UPDATE concepten SET status = 'klaar', klaar_op = now() WHERE id = %s RETURNING 1", (cid,))
    except Exception as e:  # noqa: BLE001
        q("UPDATE concepten SET status = 'fout', fout = %s, klaar_op = now() WHERE id = %s RETURNING 1", (str(e)[:500], cid))


# ---------- bronbestanden ----------

MAX_BESTAND = 10 * 1024 * 1024        # per bestand
MAX_TOTAAL = 18 * 1024 * 1024         # pdf's en afbeeldingen samen per artikel (de API neemt maximaal 32 MB per verzoek)
MAX_TEKST = 80_000                    # tekens tekst per bestand
SOORTEN_UITLEG = "PDF, Word (.docx), tekst (.txt, .md), CSV, Excel (.xlsx) en afbeeldingen (JPG, PNG, WebP)"


def _docx_tekst(data):
    import io
    import zipfile
    from xml.etree import ElementTree as ET
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    regels = []
    for p in root.iter(w + "p"):
        tekst = "".join(t.text or "" for t in p.iter(w + "t"))
        stijl = p.find(f"{w}pPr/{w}pStyle")
        kop = stijl is not None and re.match(r"(?i)(heading|kop)\s*[1-4]", stijl.get(w + "val") or "")
        if tekst.strip():
            regels.append(("## " + tekst.strip()) if kop else tekst.strip())
    return "\n\n".join(regels)


def _xlsx_tekst(data):
    import io
    import warnings

    import openpyxl
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        boek = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    delen = []
    for blad in boek.worksheets:
        rijen = []
        for i, r in enumerate(blad.iter_rows(values_only=True)):
            if i >= 300:
                rijen.append("| ... |"); break
            if any(c is not None and str(c).strip() for c in r):
                rijen.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
        if rijen:
            delen.append(f"Tabblad {blad.title}:\n" + "\n".join(rijen))
    return "\n\n".join(delen)


def lees_bestand(naam, data):
    """Geeft (mime, data_om_te_bewaren, tekst) terug of gooit ValueError met een begrijpelijke melding."""
    n = naam.lower()
    if len(data) > MAX_BESTAND:
        raise ValueError("groter dan 10 MB")
    if n.endswith(".pdf"):
        if not data.startswith(b"%PDF"):
            raise ValueError("geen geldige PDF")
        return "application/pdf", data, None
    if n.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        from . import ssi
        klein, mime = ssi.verklein(data)  # max 1800 px breed, JPEG
        return mime, klein, None
    if n.endswith(".docx"):
        return "text/plain", None, _docx_tekst(data)[:MAX_TEKST]
    if n.endswith(".xlsx"):
        return "text/plain", None, _xlsx_tekst(data)[:MAX_TEKST]
    if n.endswith((".txt", ".md", ".csv", ".html", ".htm")):
        tekst = data.decode("utf-8-sig", errors="replace")
        if n.endswith((".html", ".htm")):
            tekst = opmaak.uit_html(tekst)
        return "text/plain", None, tekst[:MAX_TEKST]
    raise ValueError("dit soort bestand kan de cockpit niet lezen")


async def bewaar_bestanden(aid, bestanden):
    """Bewaart geüploade bestanden bij een artikel. Geeft een lijst met meldingen terug."""
    meldingen = []
    totaal = q1("SELECT coalesce(sum(grootte), 0) AS t FROM artikel_bestanden WHERE artikel_id = %s AND data IS NOT NULL", (aid,))["t"]
    for b in bestanden[:10]:
        if not getattr(b, "filename", None):
            continue
        naam = b.filename.rsplit("/", 1)[-1][:200]
        data = await b.read(MAX_BESTAND + 1)
        try:
            mime, bewaar, tekst = lees_bestand(naam, data)
        except Exception as e:  # noqa: BLE001
            meldingen.append(f"{naam}: niet gebruikt ({e if isinstance(e, ValueError) else 'kon het bestand niet lezen'}).")
            continue
        if bewaar is not None and totaal + len(bewaar) > MAX_TOTAAL:
            meldingen.append(f"{naam}: niet gebruikt, de PDF's en afbeeldingen bij dit artikel worden samen te groot (max 18 MB).")
            continue
        if bewaar is None and not (tekst or "").strip():
            meldingen.append(f"{naam}: er stond geen leesbare tekst in.")
            continue
        totaal += len(bewaar or b"")
        q("INSERT INTO artikel_bestanden (artikel_id, naam, mime, grootte, data, tekst) VALUES (%s, %s, %s, %s, %s, %s) RETURNING 1",
          (aid, naam, mime, len(bewaar) if bewaar is not None else len((tekst or "").encode()), bewaar, tekst))
    return meldingen


def bestanden_van(aid):
    return q("SELECT id, naam, mime, grootte, (data IS NOT NULL) AS binair, left(tekst, 300) AS begin FROM artikel_bestanden "
             "WHERE artikel_id = %s ORDER BY id", (aid,))


def bron_blokken(aid):
    """De bronbestanden als inhoudsblokken voor Claude (PDF's en afbeeldingen zoals ze zijn, de rest als tekst)."""
    blokken = []
    for r in q("SELECT naam, mime, data, tekst FROM artikel_bestanden WHERE artikel_id = %s ORDER BY id", (aid,)):
        if r["mime"] == "application/pdf":
            blokken.append({"type": "document", "title": r["naam"],
                            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(bytes(r["data"])).decode()}})
        elif r["data"] is not None:
            blokken.append({"type": "text", "text": f"Bijgevoegde afbeelding: {r['naam']}"})
            blokken.append({"type": "image", "source": {"type": "base64", "media_type": r["mime"], "data": base64.b64encode(bytes(r["data"])).decode()}})
        elif r["tekst"]:
            blokken.append({"type": "document", "title": r["naam"], "source": {"type": "text", "media_type": "text/plain", "data": r["tekst"]}})
    return blokken


BRONREGEL = """

Bijgevoegde bestanden: Martijn heeft bestanden meegestuurd (PDF's, documenten, tabellen of afbeeldingen). Dat zijn zijn bronnen: gebruik ze als eerste en belangrijkste bron voor feiten en cijfers, en neem niets over dat er niet in staat zonder het te bevestigen. Noem ze bij bronnen als {"titel": "Bestand: naam van het bestand", "url": ""}."""


# ---------- bestaand bericht bijwerken ----------

def wp_berichten(zoek=""):
    import urllib.parse
    p = {"per_page": 20, "status": "publish,draft,future,pending", "orderby": "date", "order": "desc",
         "_fields": "id,title,date,modified,link,status"}
    if zoek:
        p["search"] = zoek
    return wp._wp("GET", "/wp/v2/posts?" + urllib.parse.urlencode(p))


def _wp_bericht(pid):
    return wp._wp("GET", f"/wp/v2/posts/{int(pid)}?context=edit&_fields=id,title,content,excerpt,status,link,modified_gmt,categories,featured_media")


def importeer(pid, opnieuw=False):
    """Haalt een bericht op. Staat er al een niet-opgeslagen bewerking van in de cockpit, dan die (tenzij opnieuw)."""
    if not opnieuw:
        eerder = q1("""SELECT id FROM artikelen WHERE soort = 'bestaand' AND wp_post_id = %s AND wp_opgeslagen IS NULL
                       AND concept_id IS NULL ORDER BY id DESC LIMIT 1""", (pid,))
        if eerder:
            return eerder
    p = _wp_bericht(pid)
    tekst, vaste = opmaak.wp_naar_opmaak((p.get("content") or {}).get("raw") or "")
    titel = re.sub("<[^>]+>", "", (p.get("title") or {}).get("raw") or "").strip()
    onderwerp = "artikel"
    try:
        namen = " ".join(f"{c.get('slug')} {c.get('name')}".lower() for c in wp._wp("GET", "/wp/v2/categories?per_page=100&_fields=id,name,slug")
                         if c["id"] in (p.get("categories") or []))
        onderwerp = "congres" if "congres" in namen else "artikel"
    except Exception:  # noqa: BLE001
        pass
    return q1("""INSERT INTO artikelen (soort, onderwerp, niveau, invoer_titel, invoer, titel, tekst, status, wp_post_id, wp_link,
                   wp_status, wp_gewijzigd, vaste_blokken)
                 VALUES ('bestaand', %s, 'licht', %s, %s, %s, %s, 'klaar', %s, %s, %s, %s, %s::jsonb) RETURNING id""",
              (onderwerp, titel, tekst, titel, tekst, int(pid), p.get("link"), p.get("status"), p.get("modified_gmt"), json.dumps(vaste)))


def naar_wordpress(aid, forceer=False):
    """Slaat titel en tekst op in het bestaande bericht. WordPress bewaart de vorige versie als revisie.
    Geeft (ok, melding) terug. Is het bericht intussen in WordPress gewijzigd, dan alleen met forceer."""
    a = q1("SELECT * FROM artikelen WHERE id = %s", (aid,))
    p = _wp_bericht(a["wp_post_id"])
    if p.get("status") == "trash":
        return False, "Dit bericht staat in de prullenbak van WordPress."
    if not forceer and a.get("wp_gewijzigd") and p.get("modified_gmt") != a["wp_gewijzigd"]:
        return False, "conflict"
    vaste = a.get("vaste_blokken") or {}
    if isinstance(vaste, str):
        vaste = json.loads(vaste)
    ontbreekt = [n for n in vaste if f"[[blok {n}" not in (a["tekst"] or "")]
    inhoud = opmaak.naar_wp(a["tekst"], vaste)
    r = wp._wp("POST", f"/wp/v2/posts/{int(a['wp_post_id'])}", data={"title": a["titel"], "content": inhoud})
    try:
        wp.beeld_in_artikel(a["wp_post_id"])
    except Exception:  # noqa: BLE001
        pass
    nieuw = _wp_bericht(a["wp_post_id"])
    q("UPDATE artikelen SET wp_gewijzigd = %s, wp_link = %s, wp_status = %s, wp_opgeslagen = now() WHERE id = %s RETURNING 1",
      (nieuw.get("modified_gmt"), r.get("link") or a["wp_link"], nieuw.get("status"), aid))
    melding = "Opgeslagen in WordPress. De vorige versie staat bij Revisies in WordPress."
    if ontbreekt:
        melding += f" Let op: {len(ontbreekt)} vast onderdeel (foto, video of knop) stond niet meer in de tekst en is dus weggehaald."
    return True, melding


def _sociaal_voor_bestaand(aid, cid, seo):
    """Maakt LinkedIn- en Instagram-teksten (en eventueel nieuwe SEO-velden) bij een bijgewerkt bestaand bericht."""
    a = q1("SELECT * FROM artikelen WHERE id = %s", (aid,))
    try:
        onderwerp = a.get("onderwerp") or "artikel"
        sys_sociaal = SOCIAAL + ONDERWERP_SOCIAAL.get(onderwerp, "") + (
            "\n\nDit artikel stond al op de site en is bijgewerkt. Deel het als bijgewerkt stuk: noem in LinkedIn kort wat er nieuw is als dat uit de tekst blijkt.")
        if onderwerp in ("congres", "zerohero"):
            sys_sociaal += "\n\nVaste gegevens:\n" + vaste_info(onderwerp)
        tekst = re.sub(r"^\[\[blok [^\]]*\]\]$", "", a["tekst"] or "", flags=re.M)
        antwoord, usage = claude_api.vraag(sys_sociaal, f"TITEL: {a['titel']}\n\nARTIKEL:\n{tekst}", max_tokens=6000)
        o = claude_api.json_uit(antwoord, "linkedin")
        g = lambda k: opmaak.zonder_streepjes(str(o.get(k) or "")).strip()  # noqa: E731
        sporten = [re.sub(r"[^a-z]", "", str(x).lower()) for x in _lijst(o.get("sporten"))]
        sport = "alle sporten" if ("alle" in sporten or len(sporten) > 2) else (sporten[0] if sporten else "")
        p = _wp_bericht(a["wp_post_id"])
        media = p.get("featured_media") or None
        q("""UPDATE concepten SET blog_titel = %s, blog_html = %s, linkedin = %s, instagram = %s, verificatie = %s::jsonb, kernzin = %s,
               foto_zoek = %s::jsonb, sport = %s, model = %s, tokens_in = %s, tokens_out = %s, beeld_media_id = %s,
               beeld_url = %s, beeld_bron = %s, fout = NULL WHERE id = %s RETURNING 1""",
          (a["titel"], opmaak.naar_html(tekst), g("linkedin"), g("instagram"),
           json.dumps([opmaak.zonder_streepjes(str(x)) for x in _lijst(o.get("verificatie"))][:8]), g("kernzin")[:160] or a["titel"],
           json.dumps(_lijst(o.get("foto_zoek"))[:3]), sport, claude_api.SCHRIJFMODEL, usage["input_tokens"], usage["output_tokens"],
           media, wp.media_url(media) if media else None, "bestaand" if media else None, cid))
        if seo:
            try:
                wp._wp("POST", "/rankmath/v1/updateMeta", data={"objectType": "post", "objectID": int(a["wp_post_id"]), "meta": {
                    "rank_math_title": (g("seo_titel") or a["titel"]) + " %sep% %sitename%",
                    "rank_math_description": g("meta_beschrijving") or g("samenvatting_kort"),
                    "rank_math_focus_keyword": g("focus_keyword")}})
            except Exception as e:  # noqa: BLE001
                q("UPDATE concepten SET fout = %s WHERE id = %s RETURNING 1", (f"SEO-velden niet bijgewerkt: {str(e)[:200]}", cid))
        q("UPDATE concepten SET status = 'klaar', klaar_op = now() WHERE id = %s RETURNING 1", (cid,))
    except Exception as e:  # noqa: BLE001
        q("UPDATE concepten SET status = 'fout', fout = %s, klaar_op = now() WHERE id = %s RETURNING 1", (str(e)[:500], cid))


@router.get("/artikelen/wp-lijst", response_class=HTMLResponse)
def wp_lijst(request: Request, zoek: str = ""):
    try:
        berichten = wp_berichten(zoek.strip()) if wp.wp_actief() else []
        fout = "" if wp.wp_actief() else "De cockpit heeft geen WordPress-gegevens."
    except Exception as e:  # noqa: BLE001
        berichten, fout = [], f"WordPress niet bereikbaar: {str(e)[:150]}"
    bezig = {r["wp_post_id"]: r["id"] for r in q("SELECT DISTINCT ON (wp_post_id) wp_post_id, id FROM artikelen WHERE wp_post_id IS NOT NULL ORDER BY wp_post_id, id DESC")}
    return templates.TemplateResponse("_wp_lijst.html", ctx(request, berichten=berichten, fout=fout, zoek=zoek, bezig=bezig))


@router.post("/artikelen/wp/{pid}")
def wp_openen(request: Request, pid: int, opnieuw: str = Form("")):
    controleer_herkomst(request)
    try:
        a = importeer(pid, bool(opnieuw))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Het bericht kon niet uit WordPress worden gehaald: {str(e)[:150]}") from None
    return RedirectResponse(f"/artikelen/{a['id']}", status_code=303)


@router.post("/artikelen/{aid}/naar-wp")
def wp_opslaan_route(request: Request, aid: int, forceer: str = Form(""), sociaal: str = Form(""), seo: str = Form("")):
    controleer_herkomst(request)
    return _naar_wp_en_verder(aid, bool(forceer), bool(sociaal), bool(seo))


def _naar_wp_en_verder(aid, forceer, sociaal, seo):
    from urllib.parse import quote
    try:
        ok, melding = naar_wordpress(aid, forceer)
    except Exception as e:  # noqa: BLE001
        ok, melding = False, f"WordPress gaf een fout: {str(e)[:200]}"
    if not ok:
        extra = "&sociaal=1" if sociaal else ""
        extra += "&seo=1" if seo else ""
        return RedirectResponse(f"/artikelen/{aid}?" + ("conflict=1" + extra if melding == "conflict" else "wpfout=" + quote(melding)), status_code=303)
    if sociaal and claude_api.actief():
        a = q1("SELECT * FROM artikelen WHERE id = %s", (aid,))
        c = q1("""INSERT INTO concepten (item_ids, status, artikel_id, blog_titel, wp_post_id, wp_link, bestaand_bericht, gepubliceerd_op)
                  VALUES ('{}', 'bezig', %s, %s, %s, %s, true, %s) RETURNING id""",
               (aid, a["titel"], a["wp_post_id"], a["wp_link"], dt.datetime.now(dt.timezone.utc) if a["wp_status"] == "publish" else None))
        q("UPDATE artikelen SET concept_id = %s WHERE id = %s RETURNING 1", (c["id"], aid))
        _achtergrond(_sociaal_voor_bestaand, aid, c["id"], seo)
        return RedirectResponse(f"/concepten/{c['id']}", status_code=303)
    return RedirectResponse(f"/artikelen/{aid}?wpok=" + quote(melding), status_code=303)


# ---------- schermen ----------

@router.get("/artikelen", response_class=HTMLResponse)
def artikelen(request: Request):
    rijen = q("""SELECT a.*, c.status AS concept_status, c.gepubliceerd_op FROM artikelen a
                 LEFT JOIN concepten c ON c.id = a.concept_id ORDER BY a.bijgewerkt DESC LIMIT 60""")
    return templates.TemplateResponse("artikelen.html", ctx(request, pagina="artikelen", rijen=rijen, niveaus=NIVEAUS,
                                                            lengtes=LENGTES, actief=claude_api.actief(),
                                                            bestandsoorten=SOORTEN_UITLEG, onderwerpen=ONDERWERPEN, groepen=ONDERWERP_GROEPEN,
                                                            info={k: (VASTE_INFO[k][0], vaste_info(k)) for k in VASTE_INFO}))


@router.post("/artikelen/vaste-info")
def vaste_info_opslaan(request: Request, sleutel: str = Form(...), waarde: str = Form("")):
    controleer_herkomst(request)
    if sleutel not in VASTE_INFO:
        raise HTTPException(400)
    q("""INSERT INTO instellingen (sleutel, waarde, bijgewerkt) VALUES (%s, %s, now())
         ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now() RETURNING 1""",
      (f"artikel_info_{sleutel}", waarde.strip()))
    return RedirectResponse("/artikelen?info=1#info", status_code=303)


@router.post("/artikelen")
async def artikel_nieuw(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    soort = str(form.get("soort") or "eigen")
    invoer = str(form.get("invoer") or "").strip()
    invoer_titel = str(form.get("invoer_titel") or "").strip()
    niveau = str(form.get("niveau") or "")
    aanwijzing = str(form.get("aanwijzing") or "").strip()
    onderwerp = str(form.get("onderwerp") or "artikel")
    onderwerp = onderwerp if onderwerp in ONDERWERPEN else "artikel"
    bestanden = [b for b in form.getlist("bestanden") if getattr(b, "filename", None)]
    if soort == "eigen" and not invoer:
        # Geen tekst geplakt? Dan is het eerste Word- of tekstbestand het artikel zelf.
        for b in bestanden:
            if b.filename.lower().endswith((".docx", ".txt", ".md")):
                try:
                    _m, _d, invoer = lees_bestand(b.filename, await b.read(MAX_BESTAND + 1))
                    invoer = (invoer or "").strip()
                    invoer_titel = invoer_titel or re.sub(r"\.(docx|txt|md)$", "", b.filename.rsplit("/", 1)[-1], flags=re.I)
                except Exception:  # noqa: BLE001
                    invoer = ""
                bestanden.remove(b)
                break
    if not invoer:
        return RedirectResponse("/artikelen?leeg=1" + ("#idee" if soort == "idee" else ""), status_code=303)
    if soort == "eigen":
        niveau = niveau if niveau in NIVEAUS else "licht"
        klaar = niveau == "geen" or not claude_api.actief()
        a = q1("""INSERT INTO artikelen (soort, onderwerp, niveau, invoer_titel, invoer, aanwijzing, titel, tekst, status)
                  VALUES ('eigen', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
               (onderwerp, niveau, invoer_titel or None, invoer, aanwijzing or None, (invoer_titel or "Zonder titel") if klaar else None,
                opmaak.zonder_streepjes(invoer) if klaar else None, "klaar" if klaar else "bezig"))
        meldingen = await bewaar_bestanden(a["id"], bestanden)
        if not klaar:
            _achtergrond(_werk_artikel, a["id"], "verbeter")
    else:
        if not claude_api.actief():
            raise HTTPException(400, "Er is geen Claude-sleutel ingesteld (deploy/claude.sh).")
        niveau = niveau if niveau in LENGTES else "middel"
        a = q1("""INSERT INTO artikelen (soort, onderwerp, niveau, invoer_titel, invoer, aanwijzing)
                  VALUES ('idee', %s, %s, %s, %s, %s) RETURNING id""", (onderwerp, niveau, invoer_titel or None, invoer, aanwijzing or None))
        meldingen = await bewaar_bestanden(a["id"], bestanden)
        _achtergrond(_werk_artikel, a["id"], "schrijf")
    from urllib.parse import quote
    return RedirectResponse(f"/artikelen/{a['id']}" + (("?bestandmelding=" + quote(" ".join(meldingen))) if meldingen else ""), status_code=303)


@router.post("/artikelen/{aid}/bestanden")
async def artikel_bestanden_erbij(request: Request, aid: int):
    controleer_herkomst(request)
    form = await request.form()
    meldingen = await bewaar_bestanden(aid, [b for b in form.getlist("bestanden") if getattr(b, "filename", None)])
    from urllib.parse import quote
    return RedirectResponse(f"/artikelen/{aid}" + (("?bestandmelding=" + quote(" ".join(meldingen))) if meldingen else "?bestanden=1"), status_code=303)


@router.post("/artikelen/{aid}/bestanden/{bid}/weg")
def artikel_bestand_weg(request: Request, aid: int, bid: int):
    controleer_herkomst(request)
    q("DELETE FROM artikel_bestanden WHERE id = %s AND artikel_id = %s RETURNING 1", (bid, aid))
    return RedirectResponse(f"/artikelen/{aid}", status_code=303)


def _artikel(aid):
    a = q1("SELECT * FROM artikelen WHERE id = %s", (aid,))
    if not a:
        raise HTTPException(404)
    a = dict(a)
    for k in ("wijzigingen", "suggesties", "te_controleren", "bronnen"):
        a[k] = _lijst(a.get(k))
    a["woorden"] = len(re.findall(r"\w+", a.get("tekst") or ""))
    a["bestanden"] = bestanden_van(aid)
    if a["concept_id"]:
        a["concept"] = q1("SELECT id, status, wp_link, gepubliceerd_op, fout FROM concepten WHERE id = %s", (a["concept_id"],))
    return a


@router.get("/artikelen/{aid}", response_class=HTMLResponse)
def artikel(request: Request, aid: int):
    a = _artikel(aid)
    if a["status"] == "bezig" and (dt.datetime.now(dt.timezone.utc) - a["bijgewerkt"]).total_seconds() > 900:
        q("UPDATE artikelen SET status = 'fout', fout = 'Geen antwoord binnen 15 minuten.' WHERE id = %s RETURNING 1", (aid,))
        a = _artikel(aid)
    return templates.TemplateResponse("artikel.html", ctx(request, pagina="artikelen", a=a, onderwerpen=ONDERWERPEN, html=opmaak.naar_html(a.get("tekst") or ""),
                                                          niveaus=NIVEAUS, lengtes=LENGTES, actief=claude_api.actief()))


@router.get("/artikelen/{aid}/status", response_class=HTMLResponse)
def artikel_status(request: Request, aid: int):
    a = _artikel(aid)
    if a["status"] != "bezig":
        return HTMLResponse("", headers={"HX-Redirect": f"/artikelen/{aid}"})
    return HTMLResponse(f'<span class="noot">Bezig sinds {a["bijgewerkt"].astimezone().strftime("%H:%M")}…</span>')


@router.post("/artikelen/{aid}/opslaan")
async def artikel_opslaan(request: Request, aid: int, titel: str = Form(""), tekst: str = Form(""), actie: str = Form("opslaan"),
                    aanwijzing: str = Form("")):
    controleer_herkomst(request)
    a = _artikel(aid)
    if a["status"] == "bezig":
        return RedirectResponse(f"/artikelen/{aid}", status_code=303)
    q("UPDATE artikelen SET titel = %s, tekst = %s, bijgewerkt = now() WHERE id = %s RETURNING 1",
      (opmaak.zonder_streepjes(titel.strip()) or a["titel"], tekst.strip(), aid))
    if actie == "ronde" and aanwijzing.strip() and claude_api.actief():
        q("UPDATE artikelen SET status = 'bezig', bijgewerkt = now() WHERE id = %s RETURNING 1", (aid,))
        _achtergrond(_werk_artikel, aid, "ronde", aanwijzing.strip()[:2000])
    elif actie in ("wp", "wp_sociaal") and a["soort"] == "bestaand":
        form = await request.form()
        return _naar_wp_en_verder(aid, False, actie == "wp_sociaal", bool(form.get("seo")))
    elif actie == "concept":
        if not claude_api.actief():
            raise HTTPException(400, "Er is geen Claude-sleutel ingesteld (deploy/claude.sh).")
        c = q1("INSERT INTO concepten (item_ids, status, artikel_id, blog_titel) VALUES ('{}', 'bezig', %s, %s) RETURNING id",
               (aid, titel.strip() or a["titel"]))
        q("UPDATE artikelen SET concept_id = %s WHERE id = %s RETURNING 1", (c["id"], aid))
        _achtergrond(_maak_concept, aid, c["id"])
        return RedirectResponse(f"/concepten/{c['id']}", status_code=303)
    return RedirectResponse(f"/artikelen/{aid}?opgeslagen=1", status_code=303)


@router.post("/artikelen/{aid}/opnieuw")
def artikel_opnieuw(request: Request, aid: int):
    controleer_herkomst(request)
    a = _artikel(aid)
    if a["status"] == "fout" and claude_api.actief():
        q("UPDATE artikelen SET status = 'bezig', fout = NULL, bijgewerkt = now() WHERE id = %s RETURNING 1", (aid,))
        _achtergrond(_werk_artikel, aid, "verbeter" if a["soort"] == "eigen" else ("schrijf" if not a["tekst"] else "ronde"),
                     "Maak de vorige aanpassing opnieuw.")
    return RedirectResponse(f"/artikelen/{aid}", status_code=303)


@router.get("/artikelen/{aid}/word")
def artikel_word(aid: int):
    from urllib.parse import quote
    from fastapi.responses import Response
    from . import woord
    a = _artikel(aid)
    label = {"persbericht": "Persbericht", "aankondiging": "Aankondiging"}.get(a.get("onderwerp"))
    data = woord.maak(a.get("titel") or a.get("invoer_titel"), opmaak.zonder_streepjes(a.get("tekst") or ""), label)
    naam = woord.bestandsnaam(a.get("titel") or a.get("invoer_titel"))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(naam)}"})


@router.post("/artikelen/voorbeeld", response_class=HTMLResponse)
def artikel_voorbeeld(tekst: str = Form("")):
    return HTMLResponse(opmaak.naar_html(tekst) or '<p class="noot">Nog geen tekst.</p>')


@router.post("/artikelen/{aid}/weg")
def artikel_weg(request: Request, aid: int):
    controleer_herkomst(request)
    q("DELETE FROM artikel_bestanden WHERE artikel_id = %s RETURNING 1", (aid,))
    q("DELETE FROM artikelen WHERE id = %s RETURNING 1", (aid,))
    return RedirectResponse("/artikelen", status_code=303)
