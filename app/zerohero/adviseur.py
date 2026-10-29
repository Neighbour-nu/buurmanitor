"""Adviseur: een sparringpartner voor de koers van ZEROHERO.

Twee dingen:
- Analyse: de cockpit verzamelt alle cijfers die het dashboard kent (kanalen, website, winkel, congres, kansen, content)
  en de nieuwssignalen uit de inbox in één dossier. Claude leest dat, zoekt zelf op het web naar marktsignalen
  (sportkalender, actualiteit, evenementen, media, partners) en geeft een advies met concrete acties per terrein:
  website, content, social, contacten, evenementen en strategie.
- Gesprek: daarna praat Martijn met de adviseur door over keuzes, met hetzelfde dossier als achtergrond.

Acties komen op een actielijst (open, oppakken, gedaan, niet nu). De volgende analyse weet wat er met eerdere acties
is gebeurd, zodat hij niet herhaalt wat Martijn al afwees en voortbouwt op wat er liep.
"""
from . import omgeving
import datetime as dt
import json
import re
import threading
import time
import traceback
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import claude_api, gdata, linkedin, meta, opmaak, podcast, profielbezoek, ssi, woo, youtube
from .main import CONGRES_DATUM, controleer_herkomst, ctx, q, q1, templates

router = APIRouter()

MODEL = None  # None = claude_api.SCHRIJFMODEL
SNEL = "claude-haiku-4-5"

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS advies_analyses (
        id serial PRIMARY KEY, aangemaakt timestamptz NOT NULL DEFAULT now(), bijgewerkt timestamptz NOT NULL DEFAULT now(),
        focus text, horizon text, automatisch boolean NOT NULL DEFAULT false, dossier text, resultaat jsonb, bronnen jsonb,
        status text NOT NULL DEFAULT 'bezig', fout text, tokens_in int NOT NULL DEFAULT 0, tokens_out int NOT NULL DEFAULT 0,
        zoekopdrachten int NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS advies_acties (
        id serial PRIMARY KEY, analyse_id int, gesprek_id int, aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now(), categorie text NOT NULL DEFAULT 'strategie', titel text NOT NULL,
        waarom text, hoe text, prioriteit text, inspanning text, wanneer text, meten text, artikel jsonb,
        status text NOT NULL DEFAULT 'open', notitie text)""",
    """CREATE TABLE IF NOT EXISTS advies_gesprekken (
        id serial PRIMARY KEY, aangemaakt timestamptz NOT NULL DEFAULT now(), bijgewerkt timestamptz NOT NULL DEFAULT now(),
        titel text, analyse_id int, actie_id int, dossier text, status text NOT NULL DEFAULT 'klaar', fout text,
        tokens_in int NOT NULL DEFAULT 0, tokens_out int NOT NULL DEFAULT 0, zoekopdrachten int NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS advies_berichten (
        id serial PRIMARY KEY, gesprek_id int NOT NULL, rol text NOT NULL, tekst text NOT NULL, bronnen jsonb,
        acties_gemaakt int, aangemaakt timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS advies_berichten_gesprek ON advies_berichten (gesprek_id, id)",
    "CREATE TABLE IF NOT EXISTS instellingen (sleutel text PRIMARY KEY, waarde text, bijgewerkt timestamptz NOT NULL DEFAULT now())",
]

CATEGORIEEN = {
    "website": "Website",
    "content": "Content",
    "social": "Social",
    "contact": "Contacten en kanalen",
    "evenement": "Evenementen",
    "strategie": "Strategie",
}
STATUSSEN = {"open": "Open", "oppakken": "Oppakken", "gedaan": "Gedaan", "niet": "Niet nu"}
PRIORITEITEN = {"hoog": 0, "middel": 1, "laag": 2}
HORIZONNEN = {"week": "de komende week", "maand": "de komende vier weken", "kwartaal": "het komende kwartaal", "jaar": "de komende twaalf maanden"}

DOELEN = """Doelstelling: ZEROHERO wil hét platform zijn voor keepers en goalies, en een autoriteit in het keepersvak vanuit een sportoverstijgende positie (voetbal, handbal, hockey, ijshockey, waterpolo, lacrosse). Niet de zoveelste voetbalkeeperssite, maar de plek waar de keeper als type, als rol en als mindset centraal staat, over alle sporten heen.

Wat telt (sanity metrics, het doel):
- uitnodigingen voor lezingen en presentaties (bedrijfsleven: MT-dagen, kick-offs, risk en compliance, IT en kwaliteit)
- kaarten voor het ZEROHERO Keeperscongres
- verkochte boeken (ZEROHERO, 2024; De Keeper-mindset, 2026)
- abonnees op de nieuwsbrief
- media-aandacht en verzoeken om expertise
- samenwerkingen met bonden, clubs, keepersscholen, merken en media

Wat context is (vanity metrics): volgers, weergaven, impressies. Nuttig om te zien wat werkt, geen doel op zich.

Lopend:
- ZEROHERO Keeperscongres, woensdag 3 februari 2027, Apenheul in Apeldoorn
- Keepersjournaal: podcast en YouTube met keepersspecialist Ruud Doevendans
- zerohero.one: keepersverhalen, analyses en nieuws, plus de webshop met de boeken
- LinkedIn (persoonlijk profiel van Martijn en de bedrijfspagina), Instagram, Facebook, nieuwsbrief via MailerLite

Beschikbare tijd en middelen: (vul in, bijvoorbeeld het aantal uren per week en het budget)
Wat ik niet wil of niet doe: (vul in)
Wat ik dit jaar wil bereiken: (vul in, liefst meetbaar)"""

ROL = """Je bent de strategisch adviseur van ZEROHERO (zerohero.one), het Nederlandstalige platform van Martijn Buurman voor keepers en goalies uit alle sporten. Martijn is auteur, spreker en oprichter; hij werkt in zijn eentje met een kleine kring mensen om zich heen. Je denkt mee als een ervaren, eerlijke sparringpartner met verstand van contentstrategie, community, sportmedia, evenementen en het verkopen van lezingen en boeken.

Hoe je werkt:
- Je redeneert vanuit het doel: een platform en een autoriteit in het keepersvak, sportoverstijgend. Vraag je bij elk advies af of het daaraan bijdraagt.
- Sanity boven vanity: uitnodigingen, kaarten, boeken, abonnees, media en samenwerkingen wegen zwaarder dan volgers en weergaven. Gebruik bereikcijfers om te zien wat aanslaat, niet als doel.
- Je baseert je op de cijfers in het dossier en noemt ze concreet. Verzin geen cijfers. Zijn gegevens dun, oud of afwezig, zeg dat en houd daar rekening mee. Maak onderscheid tussen wat de data laat zien en wat jouw inschatting is.
- Je bent eerlijk, ook als het tegenvalt. Liever drie scherpe adviezen dan tien vrijblijvende. Durf te zeggen wat Martijn beter kan laten.
- Je houdt rekening met beperkte tijd: één persoon, dus kies, focus en hergebruik (één verhaal, meerdere kanalen).
- Sportoverstijgend is het onderscheidend vermogen. Zoek verbanden tussen sporten, en haken bij momenten in andere sporten dan voetbal.
- Contacten: noem organisaties, media, podcasts, evenementen, bonden, clubs, keepersscholen, merken of publieke personen in hun professionele rol, met de reden waarom en een concrete eerste stap. Geen privégegevens.
- Respecteer de gevoeligheid van onderwerpen (overlijden, ziekte, mentale gezondheid): nooit aanhaken op een manier die kan voelen als profiteren.

Schrijfregels: helder Nederlands, direct en nuchter, zonder managementtaal. Nooit gedachtestreepjes (geen — en geen –); gebruik een komma, dubbele punt, haakjes of een nieuwe zin. Lev Yashin schrijf je zo."""

ANALYSE = ROL + """

Je opdracht nu: een analyse met advies. Werk zo:
1. Lees het dossier: wat zeggen de cijfers over wat werkt en wat niet, per kanaal en over de kanalen heen? Waar zit groei, waar stilstand, waar lekt aandacht weg zonder dat het iets oplevert?
2. Zoek op het web naar marktsignalen waar ZEROHERO op kan aanhaken in de gekozen periode, bijvoorbeeld: grote toernooien en beslissende fases in de keeperssporten (voetbal, handbal, hockey, ijshockey, waterpolo, lacrosse), prijzen en verkiezingen (Yashin Trophy, Vezina, keeper van het jaar), jubilea en gedenkdagen van bekende keepers, discussies en trends in het keepersvak (regels, techniek, data, mentale kant, vrouwensport, jeugd), congressen en evenementen om te spreken of te bezoeken (sport, leiderschap, risicomanagement), media en podcasts die passen, en mogelijke partners. Gebruik de nieuwssignalen uit de inbox in het dossier ook. Noem bij elk signaal de datum en de bron.
3. Kijk naar de actielijst in het dossier: herhaal geen acties die Martijn afwees (status Niet nu) tenzij er iets wezenlijk veranderde, en bouw voort op wat gedaan of opgepakt is.
4. Geef advies: concrete acties per terrein (website, content, social, contact, evenement, strategie), met prioriteit, inspanning en hoe je het meet. Liever 8 tot 14 goede acties dan meer. Geef bij contentacties een artikelidee mee als dat logisch is. Gebruik voor website- en contentacties ook de SEO-kansen: noem de zoekterm letterlijk, en kies bij soort titel of bijna een aanpassing van de bestaande pagina, bij pagina2 of nieuw een artikel.
5. Benoem de twee of drie strategische keuzes die Martijn moet maken, met de opties en jouw advies.

Houd het compact, dan blijft het overzichtelijk en past het in één antwoord:
- samenvatting maximaal 120 woorden; waarom, wat, haak, beeld en advies elk maximaal 50 woorden; hoe maximaal 80 woorden
- maximaal 8 regels bij stand, 8 signalen, 14 acties, 3 keuzes, 6 punten bij ontbreekt en 15 bronnen
- schrijf tijdens het zoeken geen tussentekst of uitleg: je enige uitvoer is het JSON-object

Antwoord met alleen één JSON-object, zonder tekst ervoor of erna. Gebruik binnen teksten geen dubbele aanhalingstekens maar enkele, zodat de JSON geldig blijft:
{"samenvatting": "vier tot zes zinnen: waar ZEROHERO staat en wat nu het belangrijkst is",
 "stand": [{"onderwerp": "bijvoorbeeld LinkedIn of Congres", "beeld": "wat de cijfers zeggen, met getallen", "oordeel": "goed|aandacht|zorg"}],
 "signalen": [{"titel": "...", "wat": "wat er speelt", "haak": "hoe ZEROHERO kan aanhaken", "wanneer": "datum of periode", "url": "https://..."}],
 "acties": [{"categorie": "website|content|social|contact|evenement|strategie", "titel": "korte, actieve titel", "waarom": "met verwijzing naar cijfer of signaal", "hoe": "concrete eerste stappen", "prioriteit": "hoog|middel|laag", "inspanning": "klein|middel|groot", "wanneer": "deze week|deze maand|dit kwartaal|of een datum", "meten": "waaraan je ziet dat het werkt", "artikel": {"titel": "...", "idee": "drie tot vijf zinnen"} of null}],
 "keuzes": [{"vraag": "...", "opties": ["...", "..."], "advies": "..."}],
 "ontbreekt": ["gegevens die ontbreken of verouderd zijn en die het advies beter zouden maken"],
 "bronnen": [{"titel": "...", "url": "https://..."}]}"""

GESPREK = ROL + """

Je bent nu in gesprek met Martijn. Hij wil met je van gedachten wisselen over de volgende stappen, keuzes in content, activiteiten, evenementen en strategie.
- Antwoord als gesprekspartner, niet als rapport: to the point, met een duidelijk standpunt en de redenen erachter. Een korte lijst mag, tussenkoppen alleen bij een langer antwoord.
- Vraag door als de vraag onduidelijk is of als een keuze afhangt van iets wat je niet weet (tijd, budget, voorkeur). Eén gerichte vraag, niet vijf.
- Daag uit waar dat helpt: noem het risico, het alternatief, of wat hij zou moeten laten.
- Gebruik de cijfers uit het dossier en zoek op het web als je actuele informatie nodig hebt (data, evenementen, contactroutes van organisaties). Noem je bronnen.
- Opmaak: gewone alinea's met een lege regel ertussen, - voor een opsomming, **vet** spaarzaam, ## alleen voor een tussenkop. Geen tabellen tenzij om een vergelijking gevraagd wordt.

Hieronder de doelen van ZEROHERO en het dossier met de stand van zaken."""

WEEKPLAN = """

Dit is een weekplan voor {van} tot en met {tot}. Pas je werkwijze daarop aan:
- Kies voor deze week één of twee speerpunten en zeg waarom juist deze week (actualiteit, deadline, kans die verloopt).
- Geef 4 tot 8 acties die echt in deze week passen, met bij wanneer een dag (bijvoorbeeld 'dinsdag 13 oktober'). Grotere zaken knip je op: wat is de stap die deze week kan?
- Zoek gericht naar wat er deze week en vlak daarna gebeurt in de keeperssporten en daarbuiten, waar ZEROHERO op kan aanhaken.
- Houd rekening met de tijd die Martijn heeft (zie doelen). Staat die er niet, ga uit van ongeveer zes uur voor ZEROHERO deze week en zeg dat.
- Voeg aan het JSON-object een weekplan toe, één regel per dag waarop iets te doen is (dagen zonder taak laat je weg):
  "weekplan": [{{"dag": "maandag 12 oktober", "focus": "waar deze dag om draait", "taken": ["concrete taak", "..."], "minuten": 60}}]
  De taken in het weekplan zijn dezelfde als de acties, maar dan als concrete stappen per dag."""


def _week():
    """De komende week: vanaf vandaag, of vanaf maandag als het weekend is."""
    vandaag = dt.date.today()
    van = vandaag + dt.timedelta(days=7 - vandaag.weekday()) if vandaag.weekday() >= 5 else vandaag
    return van, van + dt.timedelta(days=6 - van.weekday() if vandaag.weekday() < 5 else 6)


DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]


def _datum_nl(d):
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


ACTIES_UIT = """Je haalt concrete acties uit een advies van de ZEROHERO-adviseur. Alleen acties die er echt in staan, maximaal zes. Geen gedachtestreepjes.
Antwoord met alleen JSON: {"acties": [{"categorie": "website|content|social|contact|evenement|strategie", "titel": "korte, actieve titel", "waarom": "...", "hoe": "...", "prioriteit": "hoog|middel|laag", "inspanning": "klein|middel|groot", "wanneer": "...", "meten": "..."}]}"""


# ---------- instellingen ----------

def instelling(sleutel, standaard=""):
    r = q1("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel,))
    return r["waarde"] if r and r["waarde"] is not None else standaard


def zet_instelling(sleutel, waarde):
    q("""INSERT INTO instellingen (sleutel, waarde, bijgewerkt) VALUES (%s, %s, now())
         ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now() RETURNING 1""", (sleutel, waarde))


def doelen():
    return instelling("advies_doelen", DOELEN)


# ---------- het dossier ----------

WEG = {"status", "actief", "sorteer", "screenshot", "screenshot_id", "beeld", "thumbnail", "data_url", "abonneereeks", "reeks",
       "dagreeks", "laatste_upload", "html", "blog_html", "items", "keeper"}


def _kort(o, diepte=0, max_lijst=14):
    """Maakt de uitkomst van de dashboardfuncties compact en leesbaar voor Claude: datums als tekst, lange lijsten ingekort."""
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else round(float(o), 2)
    if isinstance(o, float):
        return round(o, 2)
    if isinstance(o, (dt.datetime,)):
        return o.astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(o, dt.date):
        return o.isoformat()
    if isinstance(o, (bytes, memoryview)):
        return None
    if isinstance(o, str):
        return o if len(o) <= 240 else o[:240].rsplit(" ", 1)[0] + "…"
    if isinstance(o, dict):
        uit = {}
        for k, v in o.items():
            if k in WEG or v is None or v == [] or v == {}:
                continue
            w = _kort(v, diepte + 1, max_lijst)
            if w is not None and w != [] and w != {}:
                uit[k] = w
        return uit
    if isinstance(o, (list, tuple)):
        lijst = [_kort(v, diepte + 1, max_lijst) for v in list(o)[:max_lijst]]
        if len(o) > max_lijst:
            lijst.append(f"(en nog {len(o) - max_lijst})")
        return lijst
    return o


def _blok(titel, functie):
    try:
        inhoud = functie()
    except Exception as e:  # noqa: BLE001
        return f"## {titel}\n(niet beschikbaar: {str(e)[:120]})"
    if inhoud in (None, "", [], {}):
        return f"## {titel}\n(geen gegevens)"
    if not isinstance(inhoud, str):
        inhoud = json.dumps(_kort(inhoud), ensure_ascii=False, separators=(",", ":"))
    return f"## {titel}\n{inhoud}"


def _kanalen():
    from .dashboard import kanaalcijfers
    uit = []
    for periode in (30, 90):
        for k in kanaalcijfers(periode):
            regels = [f"{r['label']}: {r['waarde']}" + (f" ({'+' if (r['delta'] or 0) >= 0 else ''}{_kort(r['delta'])} t.o.v. vorige periode)" if r["delta"] is not None else "")
                      + (f", peildatum {r['peil']}" if r["peil"] else "") for r in k["regels"] if r["waarde"] is not None]
            if regels:
                uit.append(f"- {k['naam']}, laatste {periode} dagen: " + "; ".join(str(x) for x in regels))
    return "\n".join(uit)


def _kern():
    from .dashboard import kerncijfers
    o = {}
    for periode in (30, 90):
        k = kerncijfers(periode)
        o[f"laatste_{periode}_dagen"] = {"kansen": k["kansen"], "congreskaarten": k["kaarten"], "media_over_zerohero": k["media"],
                                         "werk_in_de_cockpit": k["werk"]}
    return o


def _zonder_contact(t):
    """Geen e-mailadressen, telefoonnummers of namen van contactpersonen naar Claude: alleen wat er gevraagd werd."""
    t = t or ""
    bericht = re.search(r"Bericht:\s*(.*)", t, re.S)
    t = bericht.group(1) if bericht else re.sub(r"(?im)^(e-?mail|telefoon|naam|achternaam)\s*:.*$", "", t)
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "", t)
    t = re.sub(r"\+?\d[\d\s-]{7,}\d", "", t)
    return t.strip()[:300] or None


def _kansen():
    open_ = q("""SELECT datum, soort, organisatie, omschrijving, bron, status, waarde_cent, notitie FROM kansen
                 WHERE status IN ('binnen', 'gesprek') ORDER BY datum DESC LIMIT 25""")
    for r in open_:
        r["omschrijving"] = re.sub(r"\s*\([^)]*\)\s*$", "", r["omschrijving"] or "") or None
        r["vraag"] = _zonder_contact(r.pop("notitie"))
    return {"open": open_,
            "afgehandeld_laatste_jaar": q("""SELECT datum, soort, organisatie, bron, status, waarde_cent FROM kansen
                                             WHERE status NOT IN ('binnen', 'gesprek') AND datum > current_date - 365
                                             ORDER BY datum DESC LIMIT 25"""),
            "per_soort_en_bron": q("""SELECT soort, bron, count(*) AS aantal, count(*) FILTER (WHERE status IN ('bevestigd', 'afgerond')) AS bevestigd
                                      FROM kansen GROUP BY soort, bron ORDER BY aantal DESC""")}


def _congres():
    from .main import congres_event
    ev = congres_event()
    filt, p = (" AND event_id = %s", (ev,)) if ev else ("", ())
    kaarten = q(f"""SELECT date_trunc('week', besteld)::date AS week, sum(aantal) AS kaarten FROM tt_orders WHERE status = 'completed'{filt}
                   GROUP BY 1 ORDER BY 1 DESC LIMIT 12""", p)
    eerdere = q("""SELECT max(evenement) AS evenement, coalesce(sum(aantal) FILTER (WHERE status = 'completed'), 0) AS kaarten,
                          min(besteld)::date AS eerste, max(besteld)::date AS laatste
                   FROM tt_orders WHERE event_id IS DISTINCT FROM %s GROUP BY event_id ORDER BY laatste DESC""", (ev or None,))
    return {"datum": CONGRES_DATUM, "dagen_tot_congres": (CONGRES_DATUM - dt.date.today()).days, "kaarten_per_week": kaarten,
            "totaal": q1(f"SELECT coalesce(sum(aantal), 0) AS kaarten, coalesce(sum(totaal_cent), 0) AS omzet_cent FROM tt_orders WHERE status = 'completed'{filt}", p),
            "telt_alleen_evenement": ev or "(alle evenementen, nog geen congres gekozen)",
            "eerdere_edities_ter_vergelijking": eerdere if ev else None}


def _website():
    o = gdata.overzicht(q, 90)
    o30 = gdata.overzicht(q, 30)
    return {"laatste_90_dagen": {k: o[k] for k in ("ga_nu", "ga_toen", "gsc_nu", "gsc_toen", "positie", "paginas", "bronnen", "kanalen", "termen") if k in o},
            "laatste_30_dagen": {k: o30[k] for k in ("ga_nu", "ga_toen", "gsc_nu", "gsc_toen", "positie", "paginas", "termen") if k in o30},
            "laatste_gsc_datum": o.get("laatste_gsc")}


def _seo_kansen():
    kansen = gdata.seo_kansen(q, 90, limiet=20)
    if not kansen:
        return None
    return {"soorten": {"titel": "pagina 1, weinig klikken: SEO-titel en metabeschrijving herschrijven",
                        "bijna": "positie 4 tot 10: tussenkop, eerste alinea, interne links",
                        "pagina2": "positie 11 tot 20: pagina uitbreiden of eigen artikel",
                        "nieuw": "positie boven 20: nieuw artikel"},
            "extra": "geschatte extra klikken per maand bij plek 3 (vuistregel); merktermen weggelaten",
            "kansen": [{k: x.get(k) for k in ("term", "soort", "vertoningen", "klikken", "positie", "ctr", "extra", "pagina")} for x in kansen]}


def _winkel():
    return {"laatste_90_dagen": woo.overzicht(q, 90), "laatste_30_dagen": woo.overzicht(q, 30)}


def _nieuwsbrief():
    from .dashboard import mailerlite_overzicht
    o = mailerlite_overzicht(90)
    return {"formulieren": o["formulieren"], "laatste_campagnes": o["campagnes"], "aanmeldingen_90_dagen": o["aanmeldingen"]}


def _youtube():
    o = youtube.detail(q, 90, "periode")
    return {"inzichten": o.get("inzichten"), "kanaal": o.get("videos"), "erbij_90_dagen": o.get("erbij_periode"),
            "videos_meeste_weergaven_90_dagen": [{k: v.get(k) for k in ("titel", "gepubliceerd", "weergaven", "in_periode", "per_dag", "interactie")}
                                                 for v in (o.get("alle") or [])[:10]]}


def _podcast():
    o = podcast.detail(q, 90, "eerste7")
    return {"inzichten": o.get("inzichten"), "som_90_dagen": o.get("som"), "volgers": o.get("volgers"), "jaren": o.get("jaren"),
            "maanden": (o.get("maanden") or [])[-13:],
            "afleveringen_beste_start": [{k: a.get(k) for k in ("titel", "publicatie", "eerste7", "afspelen", "voltooiing") if k in a}
                                         for a in (o.get("afleveringen") or [])[:10]]}


def _linkedin_profiel():
    o = linkedin.detail_profiel(q, 90)
    if not o.get("heeft_data"):
        return None
    return {"tot": o.get("tot"), "volgers": o.get("volgers"), "som_90_dagen": o.get("som"), "vorige_90_dagen": o.get("toen"),
            "maanden": (o.get("maanden") or [])[-13:], "inzichten": o.get("inzichten"), "ontdekking": o.get("ontdekking"),
            "posts_meeste_impressies": [{k: p.get(k) for k in ("titel", "datum", "impressies", "interacties", "ratio", "url") if k in p}
                                        for p in (o.get("posts") or [])[:10]],
            "publiek": {k: [{"titel": d["titel"], "rijen": d["rijen"][:5]} for d in v] for k, v in (o.get("demografie") or {}).items()}}


def _linkedin_pagina():
    o = linkedin.detail_pagina(q, 90)
    if not o.get("heeft_data"):
        return None
    posts = sorted(o.get("posts") or [], key=lambda p: -(p.get("weergaven") or p.get("impressies") or 0))
    return {"tot": o.get("tot"), "som_90_dagen": o.get("som"), "vorige_90_dagen": o.get("toen"), "maanden": (o.get("maanden") or [])[-13:],
            "inzichten": o.get("inzichten"), "posts_meeste_weergaven": posts[:8]}


def _linkedin_ssi():
    return {"ssi": [{k: r.get(k) for k in r if k not in ("screenshot_id",)} for r in ssi.historie(q)[-4:]],
            "profielbezoekers": profielbezoek.overzicht(q).get("inzichten")}


def _meta_kanaal(kanaal):
    def f():
        rijen = meta.posts(q, kanaal, 90, "interacties")
        return [{k: r.get(k) for k in ("gepubliceerd", "soort", "tekst", "weergaven", "bereik", "interacties", "ratio", "bewaard", "gedeeld")}
                for r in rijen[:10]]
    return f


def _content():
    return {"concepten_90_dagen": q("""SELECT aangemaakt::date AS datum, blog_titel, sport, status, gepubliceerd_op::date AS gepubliceerd
                                       FROM concepten WHERE aangemaakt > now() - interval '90 days' ORDER BY aangemaakt DESC LIMIT 30"""),
            "eigen_artikelen_90_dagen": q("""SELECT bijgewerkt::date AS datum, onderwerp, soort, coalesce(titel, invoer_titel) AS titel, status
                                             FROM artikelen WHERE bijgewerkt > now() - interval '90 days' ORDER BY bijgewerkt DESC LIMIT 20""")}


def _markt():
    top = q("""SELECT binnen::date AS datum, sport, competitie, keepers, titel, score, thema, nl_haak, congreshaak, invalshoek, status
               FROM items WHERE binnen > now() - interval '21 days' AND coalesce(score, 0) >= 60
                 AND coalesce(status, '') NOT IN ('onleesbaar', 'geen_inhoud', 'zoeken_mislukt', 'geleerd_weg')
               ORDER BY score DESC, binnen DESC LIMIT 35""")
    per = q("""SELECT sport, count(*) AS berichten, round(avg(score)) AS gem_score,
                      count(*) FILTER (WHERE status IN ('geselecteerd')) AS geselecteerd
               FROM items WHERE binnen > now() - interval '60 days' GROUP BY sport ORDER BY berichten DESC""")
    themas = q("""SELECT thema, count(*) AS n FROM items WHERE binnen > now() - interval '60 days' AND thema IS NOT NULL
                  GROUP BY thema ORDER BY n DESC LIMIT 12""")
    return {"sterkste_berichten_21_dagen": top, "per_sport_60_dagen": per, "themas_60_dagen": themas}


def _acties():
    rijen = q("""SELECT aangemaakt::date AS voorgesteld, categorie, titel, status, notitie, bijgewerkt::date AS bijgewerkt FROM advies_acties
                 WHERE aangemaakt > now() - interval '180 days' ORDER BY (status = 'open'), bijgewerkt DESC LIMIT 60""")
    if not rijen:
        return "(nog geen acties: dit is de eerste analyse)"
    return "\n".join(f"- [{STATUSSEN.get(r['status'], r['status'])}] {CATEGORIEEN.get(r['categorie'], r['categorie'])}: {r['titel']}"
                     f" (voorgesteld {r['voorgesteld']}{', bijgewerkt ' + str(r['bijgewerkt']) if r['status'] != 'open' else ''})"
                     + (f" Notitie van Martijn: {r['notitie']}" if r["notitie"] else "") for r in rijen)


def _vaste_info():
    from .artikel import VASTE_INFO, vaste_info
    return "\n\n".join(f"{VASTE_INFO[k][0]}:\n{vaste_info(k)}" for k in VASTE_INFO)


def dossier():
    """Alle gegevens uit de cockpit in één tekst. Elk onderdeel apart, zodat een ontbrekende koppeling de rest niet raakt."""
    vandaag = dt.date.today()
    delen = [
        f"# Dossier ZEROHERO, stand {vandaag.strftime('%d-%m-%Y')} ({['maandag', 'dinsdag', 'woensdag', 'donderdag', 'vrijdag', 'zaterdag', 'zondag'][vandaag.weekday()]})",
        "Toelichting: 'nu' is de huidige periode, 'toen' de periode daarvoor. Cijfers uit exports (LinkedIn, Spotify) lopen tot de laatste upload.",
        _blok("Kanalen in het kort (dashboard, tabblad Bereik)", _kanalen),
        _blok("Waar het om draait: kansen, congreskaarten, media en werk in de cockpit", _kern),
        _blok("Kansenlogboek (lezingen, media, podcasts, samenwerkingen)", _kansen),
        _blok("Keeperscongres en kaartverkoop", _congres),
        _blok("Website zerohero.one (Google Analytics en Search Console)", _website),
        _blok("SEO-kansen: zoektermen waarop zerohero.one al verschijnt, met ruimte voor meer klikken (Search Console)", _seo_kansen),
        _blok("Webshop (boeken)", _winkel),
        _blok("Nieuwsbrief (MailerLite)", _nieuwsbrief),
        _blok("YouTube (Keepersjournaal)", _youtube),
        _blok("Podcast (Spotify)", _podcast),
        _blok("LinkedIn, persoonlijk profiel van Martijn", _linkedin_profiel),
        _blok("LinkedIn, bedrijfspagina ZEROHERO", _linkedin_pagina),
        _blok("LinkedIn SSI en profielbezoekers", _linkedin_ssi),
        _blok("Instagram, posts met de meeste interacties (90 dagen)", _meta_kanaal("instagram")),
        _blok("Facebook, posts met de meeste interacties (90 dagen)", _meta_kanaal("facebook")),
        _blok("Threads, posts met de meeste interacties (90 dagen)", _meta_kanaal("threads")),
        _blok("Content gemaakt in de cockpit", _content),
        _blok("Nieuwssignalen uit de inbox (Miniflux, gewogen door de cockpit)", _markt),
        _blok("Actielijst van de adviseur (wat Martijn deed met eerdere adviezen)", _acties),
        _blok("Vaste informatie (congres en ZEROHERO)", _vaste_info),
    ]
    return "\n\n".join(delen)


# ---------- analyse ----------

def _achtergrond(functie, *args):
    def run():
        try:
            functie(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=run, daemon=True).start()


def _lijst(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def _schoon(t, n=1200):
    return opmaak.zonder_streepjes(str(t or "")).strip()[:n]


def _json_of_reparatie(tekst, sleutel):
    try:
        return claude_api.json_uit(tekst, sleutel)
    except ValueError:
        hersteld, _u = claude_api.vraag("Zet de tekst om naar één geldig JSON-object met dezelfde inhoud en sleutels. Is de tekst halverwege "
                                         "afgebroken, laat dan het onaffe laatste onderdeel weg en sluit alle lijsten en objecten netjes af. "
                                         "Alleen JSON, niets anders.", tekst[:60000], model=SNEL, max_tokens=16000)
        return claude_api.json_uit(hersteld, sleutel)


def _actie_rij(a, analyse_id=None, gesprek_id=None):
    cat = str(a.get("categorie") or "strategie").lower()
    cat = cat if cat in CATEGORIEEN else "strategie"
    prio = str(a.get("prioriteit") or "middel").lower()
    art = a.get("artikel") if isinstance(a.get("artikel"), dict) and a["artikel"].get("idee") else None
    if art:
        art = {"titel": _schoon(art.get("titel"), 200), "idee": _schoon(art.get("idee"), 2000)}
    titel = _schoon(a.get("titel"), 200)
    if not titel:
        return
    q("""INSERT INTO advies_acties (analyse_id, gesprek_id, categorie, titel, waarom, hoe, prioriteit, inspanning, wanneer, meten, artikel)
         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING 1""",
      (analyse_id, gesprek_id, cat, titel, _schoon(a.get("waarom")), _schoon(a.get("hoe"), 2000),
       prio if prio in PRIORITEITEN else "middel", _schoon(a.get("inspanning"), 20).lower() or None, _schoon(a.get("wanneer"), 80) or None,
       _schoon(a.get("meten"), 400) or None, json.dumps(art) if art else None))


def _werk_analyse(aid):
    a = q1("SELECT * FROM advies_analyses WHERE id = %s", (aid,))
    try:
        d = dossier()
        q("UPDATE advies_analyses SET dossier = %s, bijgewerkt = now() WHERE id = %s RETURNING 1", (d, aid))
        horizon = HORIZONNEN.get(a["horizon"] or "kwartaal", HORIZONNEN["kwartaal"])
        user = (f"Vandaag is het {dt.date.today().strftime('%d-%m-%Y')}. Periode voor het advies: {horizon}.\n"
                + (f"Waar Martijn deze keer vooral naar wil kijken: {a['focus']}\n" if a["focus"] else "")
                + f"\n<doelen>\n{doelen()}\n</doelen>\n\n<dossier>\n{d}\n</dossier>")
        system = ANALYSE
        if a["horizon"] == "week":
            van, tot = _week()
            system += WEEKPLAN.format(van=_datum_nl(van), tot=_datum_nl(tot))
        tekst, usage, citaten = claude_api.gesprek(system, [{"role": "user", "content": user}], model=MODEL, max_tokens=32000,
                                                   zoeken=6, timeout=900, doorgaan=2, inspanning="medium")
        o = _json_of_reparatie(tekst, "samenvatting")
        o["samenvatting"] = _schoon(o.get("samenvatting"), 3000)
        for veld in ("stand", "signalen", "keuzes"):
            o[veld] = [x for x in _lijst(o.get(veld)) if isinstance(x, dict)][:16]
            for x in o[veld]:
                for k, v in list(x.items()):
                    x[k] = [_schoon(i, 400) for i in v] if isinstance(v, list) else (_schoon(v, 1500) if isinstance(v, str) else v)
        o["ontbreekt"] = [_schoon(x, 400) for x in _lijst(o.get("ontbreekt"))][:10]
        o["weekplan"] = [{"dag": _schoon(x.get("dag"), 60), "focus": _schoon(x.get("focus"), 300),
                          "taken": [_schoon(t, 400) for t in _lijst(x.get("taken"))][:8],
                          "minuten": x.get("minuten") if isinstance(x.get("minuten"), (int, float)) else None}
                         for x in _lijst(o.get("weekplan")) if isinstance(x, dict) and x.get("dag")][:7]
        acties = [x for x in _lijst(o.pop("acties", [])) if isinstance(x, dict)][:20]
        bronnen = [b for b in _lijst(o.pop("bronnen", [])) if isinstance(b, dict) and str(b.get("url") or "").startswith("http")]
        gezien = {b["url"] for b in bronnen}
        bronnen += [b for b in citaten if b["url"] not in gezien]
        for x in acties:
            _actie_rij(x, analyse_id=aid)
        q("""UPDATE advies_analyses SET status = 'klaar', resultaat = %s::jsonb, bronnen = %s::jsonb, fout = NULL, bijgewerkt = now(),
               tokens_in = tokens_in + %s, tokens_out = tokens_out + %s, zoekopdrachten = zoekopdrachten + %s WHERE id = %s RETURNING 1""",
          (json.dumps(o, ensure_ascii=False, default=str), json.dumps(bronnen[:40], ensure_ascii=False), usage["input_tokens"],
           usage["output_tokens"], usage.get("zoekopdrachten", 0), aid))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        q("UPDATE advies_analyses SET status = 'fout', fout = %s, bijgewerkt = now() WHERE id = %s RETURNING 1", (str(e)[:500], aid))


def start_analyse(focus="", horizon="kwartaal", automatisch=False):
    bezig = q1("SELECT id FROM advies_analyses WHERE status = 'bezig' AND bijgewerkt > now() - interval '15 minutes' ORDER BY id DESC LIMIT 1")
    if bezig:
        return bezig["id"]
    a = q1("INSERT INTO advies_analyses (focus, horizon, automatisch) VALUES (%s, %s, %s) RETURNING id",
           (focus.strip()[:1000] or None, horizon if horizon in HORIZONNEN else "kwartaal", automatisch))
    _achtergrond(_werk_analyse, a["id"])
    return a["id"]


def _analyse(aid):
    a = q1("SELECT * FROM advies_analyses WHERE id = %s", (aid,))
    if not a:
        return None
    a = dict(a)
    if a["status"] == "bezig" and (dt.datetime.now(dt.timezone.utc) - a["bijgewerkt"]).total_seconds() > 1200:
        q("UPDATE advies_analyses SET status = 'fout', fout = 'Geen antwoord binnen 20 minuten.' WHERE id = %s RETURNING 1", (aid,))
        a.update(status="fout", fout="Geen antwoord binnen 20 minuten.")
    r = a.get("resultaat") or {}
    a["r"] = json.loads(r) if isinstance(r, str) else r
    a["bronnen"] = _lijst(a.get("bronnen"))
    a["kosten"] = kosten(a)
    return a


def kosten(r):
    """Ruwe schatting in dollars: tarief van het schrijfmodel plus de webzoekopdrachten."""
    from .kosten import dollar
    return round(dollar(r.get("tokens_in"), r.get("tokens_out"), "sonnet", r.get("zoekopdrachten")), 2)


# ---------- gesprek ----------

def _context_voor(g):
    """De vaste systeemtekst van een gesprek: rol, doelen, dossier en (als het gesprek daarover gaat) de analyse of actie."""
    delen = [GESPREK, f"<doelen>\n{doelen()}\n</doelen>", f"<dossier>\n{g['dossier'] or ''}\n</dossier>"]
    an = _analyse(g["analyse_id"]) if g.get("analyse_id") else None
    if not an:
        laatste = q1("SELECT id FROM advies_analyses WHERE status = 'klaar' ORDER BY id DESC LIMIT 1")
        an = _analyse(laatste["id"]) if laatste else None
    if an and an.get("r"):
        r = an["r"]
        delen.append(f"<laatste_analyse datum=\"{an['aangemaakt'].astimezone().strftime('%d-%m-%Y')}\">\n"
                     + json.dumps({k: r.get(k) for k in ("samenvatting", "weekplan", "stand", "signalen", "keuzes", "ontbreekt") if r.get(k)}, ensure_ascii=False)
                     + "\n</laatste_analyse>")
    open_ = q("""SELECT id, categorie, titel, waarom, hoe, status, notitie FROM advies_acties WHERE status IN ('open', 'oppakken')
                 ORDER BY (status = 'oppakken') DESC, id DESC LIMIT 25""")
    if open_:
        delen.append("<openstaande_acties>\n" + "\n".join(f"- [{STATUSSEN[a['status']]}] {a['titel']}: {a['waarom'] or ''}"
                                                          + (f" (notitie: {a['notitie']})" if a["notitie"] else "") for a in open_)
                     + "\n</openstaande_acties>")
    if g.get("actie_id"):
        a = q1("SELECT * FROM advies_acties WHERE id = %s", (g["actie_id"],))
        if a:
            delen.append(f"<besproken_actie>\n{CATEGORIEEN.get(a['categorie'])}: {a['titel']}\nWaarom: {a['waarom']}\nHoe: {a['hoe']}\n"
                         f"Wanneer: {a['wanneer']}\nMeten: {a['meten']}\nStatus: {STATUSSEN.get(a['status'])}\n</besproken_actie>")
    return "\n\n".join(delen)


def _werk_antwoord(gid):
    g = q1("SELECT * FROM advies_gesprekken WHERE id = %s", (gid,))
    try:
        berichten = q("SELECT rol, tekst FROM advies_berichten WHERE gesprek_id = %s ORDER BY id", (gid,))
        geschiedenis = [{"role": "user" if b["rol"] == "martijn" else "assistant", "content": b["tekst"]} for b in berichten][-30:]
        while geschiedenis and geschiedenis[0]["role"] != "user":
            geschiedenis.pop(0)
        vandaag = dt.date.today().strftime("%d-%m-%Y")
        system = _context_voor(g) + f"\n\nVandaag is het {vandaag}."
        tekst, usage, bronnen = claude_api.gesprek(system, geschiedenis, model=MODEL, max_tokens=12000, zoeken=3, timeout=600, cache=True, doorgaan=2,
                                                   inspanning="medium")
        tekst = opmaak.zonder_streepjes(tekst).strip() or "(geen antwoord)"
        if usage.get("afgebroken"):
            tekst += "\n\n*(Dit antwoord was erg lang en is ingekort. Vraag gerust door op het laatste punt.)*"
        q("INSERT INTO advies_berichten (gesprek_id, rol, tekst, bronnen) VALUES (%s, 'adviseur', %s, %s::jsonb) RETURNING 1",
          (gid, tekst, json.dumps(bronnen[:15], ensure_ascii=False)))
        q("""UPDATE advies_gesprekken SET status = 'klaar', fout = NULL, bijgewerkt = now(), tokens_in = tokens_in + %s,
               tokens_out = tokens_out + %s, zoekopdrachten = zoekopdrachten + %s WHERE id = %s RETURNING 1""",
          (usage["input_tokens"], usage["output_tokens"], usage.get("zoekopdrachten", 0), gid))
        if not g["titel"]:
            _geef_titel(gid)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        q("UPDATE advies_gesprekken SET status = 'fout', fout = %s, bijgewerkt = now() WHERE id = %s RETURNING 1", (str(e)[:500], gid))


def _geef_titel(gid):
    eerste = q1("SELECT tekst FROM advies_berichten WHERE gesprek_id = %s AND rol = 'martijn' ORDER BY id LIMIT 1", (gid,))
    if not eerste:
        return
    try:
        titel, _u = claude_api.vraag("Geef een korte Nederlandse titel (maximaal zes woorden, geen aanhalingstekens, geen punt, geen gedachtestreepje) voor een gesprek dat begint met deze vraag.",
                                     eerste["tekst"][:2000], model=SNEL, max_tokens=50)
        titel = opmaak.zonder_streepjes(titel).strip().strip('".')[:80]
    except Exception:  # noqa: BLE001
        titel = eerste["tekst"][:60]
    q("UPDATE advies_gesprekken SET titel = coalesce(titel, %s) WHERE id = %s RETURNING 1", (titel, gid))


def nieuw_gesprek(eerste_vraag, analyse_id=None, actie_id=None, titel=None):
    g = q1("""INSERT INTO advies_gesprekken (titel, analyse_id, actie_id, dossier, status) VALUES (%s, %s, %s, %s, 'bezig') RETURNING id""",
           (titel, analyse_id, actie_id, dossier()))
    q("INSERT INTO advies_berichten (gesprek_id, rol, tekst) VALUES (%s, 'martijn', %s) RETURNING 1", (g["id"], eerste_vraag))
    _achtergrond(_werk_antwoord, g["id"])
    return g["id"]


# ---------- wekelijkse analyse ----------

_gestart = False


def start_achtergrond():
    """Als Martijn het aanzet: elke maandagochtend vanzelf een weekplan, zodat er bij het begin van de week een advies klaarstaat."""
    global _gestart
    if _gestart or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(120)
        while True:
            try:
                nu = dt.datetime.now().astimezone()
                from . import rekening
                if (claude_api.actief() and instelling("advies_wekelijks") == "1" and nu.weekday() == 0 and nu.hour >= 6
                        and rekening.automatisch_toegestaan(q)
                        and not q1("SELECT 1 AS x FROM advies_analyses WHERE aangemaakt > now() - interval '6 days'")):
                    start_analyse("", "week", automatisch=True)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(1800)

    threading.Thread(target=lus, name="adviseur", daemon=True).start()


# ---------- schermen ----------

def _acties_lijst(status="actief"):
    filt = {"actief": "status IN ('open', 'oppakken')", "gedaan": "status = 'gedaan'", "niet": "status = 'niet'"}.get(status, "true")
    rijen = q(f"""SELECT * FROM advies_acties WHERE {filt}
                  ORDER BY (status = 'oppakken') DESC, CASE prioriteit WHEN 'hoog' THEN 0 WHEN 'middel' THEN 1 ELSE 2 END, id DESC LIMIT 200""")
    for r in rijen:
        if isinstance(r.get("artikel"), str):
            r["artikel"] = json.loads(r["artikel"])
    return rijen


def _analyse_acties(aid):
    rijen = q("SELECT * FROM advies_acties WHERE analyse_id = %s ORDER BY CASE prioriteit WHEN 'hoog' THEN 0 WHEN 'middel' THEN 1 ELSE 2 END, id", (aid,))
    for r in rijen:
        if isinstance(r.get("artikel"), str):
            r["artikel"] = json.loads(r["artikel"])
    return rijen


def _artikel_link(a):
    art = a.get("artikel") or {}
    return "/artikelen?" + urlencode({"idee_titel": art.get("titel") or a["titel"], "idee": art.get("idee") or f"{a['titel']}\n\n{a.get('hoe') or ''}",
                                      "idee_aanwijzing": f"Uit het advies van de adviseur. Waarom: {a.get('waarom') or ''}"}) + "#idee"


@router.get("/adviseur", response_class=HTMLResponse)
def adviseur(request: Request, acties: str = "actief"):
    start_achtergrond()
    laatste = q1("SELECT id FROM advies_analyses ORDER BY id DESC LIMIT 1")
    an = _analyse(laatste["id"]) if laatste else None
    if an and an["status"] != "klaar":
        vorige = q1("SELECT id FROM advies_analyses WHERE status = 'klaar' ORDER BY id DESC LIMIT 1")
        an_klaar = _analyse(vorige["id"]) if vorige else None
    else:
        an_klaar = an
    an_acties = _analyse_acties(an_klaar["id"]) if an_klaar else []
    acties_rijen = _acties_lijst(acties)
    tellers = q1("""SELECT count(*) FILTER (WHERE status IN ('open', 'oppakken')) AS actief, count(*) FILTER (WHERE status = 'gedaan') AS gedaan,
                           count(*) FILTER (WHERE status = 'niet') AS niet FROM advies_acties""")
    gesprekken = q("""SELECT g.*, (SELECT count(*) FROM advies_berichten b WHERE b.gesprek_id = g.id) AS berichten
                      FROM advies_gesprekken g ORDER BY bijgewerkt DESC LIMIT 40""")
    archief = q("SELECT id, aangemaakt, focus, horizon, status, automatisch, tokens_in, tokens_out, zoekopdrachten FROM advies_analyses ORDER BY id DESC LIMIT 40")
    for r in archief:
        r["kosten"] = kosten(r)
    return templates.TemplateResponse("adviseur.html", ctx(
        request, pagina="adviseur", an=an, an_klaar=an_klaar, an_acties=an_acties, acties=acties_rijen, acties_filter=acties, tellers=tellers,
        gesprekken=gesprekken, archief=archief, categorieen=CATEGORIEEN, statussen=STATUSSEN, horizonnen=HORIZONNEN,
        doelen=doelen(), doelen_standaard=doelen() == DOELEN, wekelijks=instelling("advies_wekelijks") == "1",
        actief=claude_api.actief(), artikel_link=_artikel_link))


@router.post("/adviseur/analyse")
def analyse_start(request: Request, focus: str = Form(""), horizon: str = Form("kwartaal")):
    controleer_herkomst(request)
    if not claude_api.actief():
        raise HTTPException(400, "Er is geen Claude-sleutel ingesteld (deploy/claude.sh).")
    start_analyse(focus, horizon)
    return RedirectResponse("/adviseur#advies", status_code=303)


@router.get("/adviseur/analyse/{aid}", response_class=HTMLResponse)
def analyse_bekijk(request: Request, aid: int):
    an = _analyse(aid)
    if not an:
        raise HTTPException(404)
    acties = _analyse_acties(aid)
    return templates.TemplateResponse("adviseur_analyse.html", ctx(request, pagina="adviseur", an=an, acties=acties, categorieen=CATEGORIEEN,
                                                                   statussen=STATUSSEN, horizonnen=HORIZONNEN, artikel_link=_artikel_link))


@router.get("/adviseur/analyse/{aid}/status", response_class=HTMLResponse)
def analyse_status(aid: int):
    an = _analyse(aid)
    if not an or an["status"] != "bezig":
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    sec = int((dt.datetime.now(dt.timezone.utc) - an["aangemaakt"]).total_seconds())
    stap = "verzamelt de cijfers" if not an.get("dossier") else "leest het dossier en zoekt naar marktsignalen"
    return HTMLResponse(f'<span class="noot">De adviseur {stap}… ({sec // 60}:{sec % 60:02d})</span>')


@router.get("/adviseur/analyse/{aid}/dossier", response_class=HTMLResponse)
def analyse_dossier(aid: int):
    an = q1("SELECT dossier FROM advies_analyses WHERE id = %s", (aid,))
    if not an or not an["dossier"]:
        raise HTTPException(404)
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>Dossier</title><pre style='white-space:pre-wrap;font:13px/1.5 monospace;padding:16px'>"
                        f"{an['dossier'].replace('&', '&amp;').replace('<', '&lt;')}</pre>")


@router.post("/adviseur/analyse/{aid}/weg")
def analyse_weg(request: Request, aid: int):
    controleer_herkomst(request)
    q("DELETE FROM advies_acties WHERE analyse_id = %s AND status = 'open' RETURNING 1", (aid,))
    q("DELETE FROM advies_analyses WHERE id = %s RETURNING 1", (aid,))
    return RedirectResponse("/adviseur#archief", status_code=303)


@router.post("/adviseur/actie/{xid}/status", response_class=HTMLResponse)
def actie_status(request: Request, xid: int, status: str = Form(...), notitie: str = Form(None)):
    controleer_herkomst(request)
    if status not in STATUSSEN:
        raise HTTPException(400)
    if notitie is None:
        q("UPDATE advies_acties SET status = %s, bijgewerkt = now() WHERE id = %s RETURNING 1", (status, xid))
    else:
        q("UPDATE advies_acties SET status = %s, notitie = %s, bijgewerkt = now() WHERE id = %s RETURNING 1",
          (status, notitie.strip()[:1000] or None, xid))
    a = q1("SELECT * FROM advies_acties WHERE id = %s", (xid,))
    if not a:
        raise HTTPException(404)
    if isinstance(a.get("artikel"), str):
        a["artikel"] = json.loads(a["artikel"])
    return templates.TemplateResponse("_advies_actie.html", ctx(request, a=a, categorieen=CATEGORIEEN, statussen=STATUSSEN, artikel_link=_artikel_link))


@router.post("/adviseur/actie")
def actie_nieuw(request: Request, titel: str = Form(...), categorie: str = Form("strategie"), hoe: str = Form(""), wanneer: str = Form("")):
    controleer_herkomst(request)
    _actie_rij({"titel": titel, "categorie": categorie, "hoe": hoe, "wanneer": wanneer, "prioriteit": "middel", "waarom": "Zelf toegevoegd."})
    return RedirectResponse("/adviseur#acties", status_code=303)


@router.post("/adviseur/actie/{xid}/weg", response_class=HTMLResponse)
def actie_weg(request: Request, xid: int):
    controleer_herkomst(request)
    q("DELETE FROM advies_acties WHERE id = %s RETURNING 1", (xid,))
    return HTMLResponse("")


@router.post("/adviseur/actie/{xid}/bespreek")
def actie_bespreek(request: Request, xid: int):
    controleer_herkomst(request)
    a = q1("SELECT * FROM advies_acties WHERE id = %s", (xid,))
    if not a:
        raise HTTPException(404)
    gid = nieuw_gesprek(f"Ik wil deze actie met je bespreken: {a['titel']}. Hoe pak ik dit het slimst aan, wat is de eerste stap, "
                        "en wat moet ik weten voordat ik begin?", analyse_id=a["analyse_id"], actie_id=xid, titel=a["titel"][:80])
    return RedirectResponse(f"/adviseur/gesprek/{gid}", status_code=303)


@router.post("/adviseur/gesprek")
def gesprek_nieuw(request: Request, vraag: str = Form(...), analyse_id: str = Form("")):
    controleer_herkomst(request)
    if not claude_api.actief():
        raise HTTPException(400, "Er is geen Claude-sleutel ingesteld (deploy/claude.sh).")
    if not vraag.strip():
        return RedirectResponse("/adviseur#gesprek", status_code=303)
    gid = nieuw_gesprek(vraag.strip()[:8000], analyse_id=int(analyse_id) if analyse_id.isdigit() else None)
    return RedirectResponse(f"/adviseur/gesprek/{gid}", status_code=303)


def _gesprek(gid):
    g = q1("SELECT * FROM advies_gesprekken WHERE id = %s", (gid,))
    if not g:
        raise HTTPException(404)
    g = dict(g)
    if g["status"] == "bezig" and (dt.datetime.now(dt.timezone.utc) - g["bijgewerkt"]).total_seconds() > 900:
        q("UPDATE advies_gesprekken SET status = 'fout', fout = 'Geen antwoord binnen 15 minuten.' WHERE id = %s RETURNING 1", (gid,))
        g.update(status="fout", fout="Geen antwoord binnen 15 minuten.")
    g["kosten"] = kosten(g)
    return g


@router.get("/adviseur/gesprek/{gid}", response_class=HTMLResponse)
def gesprek(request: Request, gid: int):
    g = _gesprek(gid)
    berichten = q("SELECT * FROM advies_berichten WHERE gesprek_id = %s ORDER BY id", (gid,))
    for b in berichten:
        b["html"] = opmaak.naar_html(b["tekst"]) if b["rol"] == "adviseur" else None
        b["bronnen"] = _lijst(b.get("bronnen"))
    actie = q1("SELECT id, titel FROM advies_acties WHERE id = %s", (g["actie_id"],)) if g.get("actie_id") else None
    andere = q("SELECT id, titel, bijgewerkt FROM advies_gesprekken WHERE id <> %s ORDER BY bijgewerkt DESC LIMIT 12", (gid,))
    return templates.TemplateResponse("adviseur_gesprek.html", ctx(request, pagina="adviseur", g=g, berichten=berichten, actie=actie,
                                                                   andere=andere, actief=claude_api.actief()))


@router.get("/adviseur/gesprek/{gid}/status", response_class=HTMLResponse)
def gesprek_status(gid: int):
    g = _gesprek(gid)
    if g["status"] != "bezig":
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return HTMLResponse('<span class="noot">De adviseur denkt na…</span>')


@router.post("/adviseur/gesprek/{gid}")
def gesprek_verder(request: Request, gid: int, vraag: str = Form("")):
    controleer_herkomst(request)
    g = _gesprek(gid)
    if g["status"] == "bezig" or not vraag.strip():
        return RedirectResponse(f"/adviseur/gesprek/{gid}", status_code=303)
    q("INSERT INTO advies_berichten (gesprek_id, rol, tekst) VALUES (%s, 'martijn', %s) RETURNING 1", (gid, vraag.strip()[:8000]))
    q("UPDATE advies_gesprekken SET status = 'bezig', fout = NULL, bijgewerkt = now() WHERE id = %s RETURNING 1", (gid,))
    _achtergrond(_werk_antwoord, gid)
    return RedirectResponse(f"/adviseur/gesprek/{gid}#laatste", status_code=303)


@router.post("/adviseur/gesprek/{gid}/opnieuw")
def gesprek_opnieuw(request: Request, gid: int):
    controleer_herkomst(request)
    g = _gesprek(gid)
    if g["status"] == "fout":
        q("UPDATE advies_gesprekken SET status = 'bezig', fout = NULL, bijgewerkt = now() WHERE id = %s RETURNING 1", (gid,))
        _achtergrond(_werk_antwoord, gid)
    return RedirectResponse(f"/adviseur/gesprek/{gid}", status_code=303)


@router.post("/adviseur/gesprek/{gid}/ververs")
def gesprek_ververs(request: Request, gid: int):
    """Het dossier van het gesprek bijwerken met de cijfers van nu (bijvoorbeeld na een nieuwe export)."""
    controleer_herkomst(request)
    q("UPDATE advies_gesprekken SET dossier = %s WHERE id = %s RETURNING 1", (dossier(), gid))
    return RedirectResponse(f"/adviseur/gesprek/{gid}?ververst=1", status_code=303)


@router.post("/adviseur/gesprek/{gid}/weg")
def gesprek_weg(request: Request, gid: int):
    controleer_herkomst(request)
    q("DELETE FROM advies_berichten WHERE gesprek_id = %s RETURNING 1", (gid,))
    q("DELETE FROM advies_gesprekken WHERE id = %s RETURNING 1", (gid,))
    return RedirectResponse("/adviseur#gesprek", status_code=303)


@router.post("/adviseur/bericht/{bid}/acties", response_class=HTMLResponse)
def bericht_acties(request: Request, bid: int):
    """Haalt de acties uit een antwoord van de adviseur en zet ze op de actielijst."""
    controleer_herkomst(request)
    b = q1("SELECT * FROM advies_berichten WHERE id = %s AND rol = 'adviseur'", (bid,))
    if not b:
        raise HTTPException(404)
    if b["acties_gemaakt"] is not None:
        return HTMLResponse(f'<span class="noot">✓ {b["acties_gemaakt"]} op de <a href="/adviseur#acties">actielijst</a> gezet.</span>')
    try:
        tekst, _u = claude_api.vraag(ACTIES_UIT, b["tekst"], model=SNEL, max_tokens=3000)
        acties = [x for x in _lijst(claude_api.json_uit(tekst, "acties").get("acties")) if isinstance(x, dict)][:6]
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f'<span class="rood-tekst">Lukte niet: {str(e)[:120]}</span>')
    for x in acties:
        _actie_rij(x, gesprek_id=b["gesprek_id"])
    q("UPDATE advies_berichten SET acties_gemaakt = %s WHERE id = %s RETURNING 1", (len(acties), bid))
    if not acties:
        return HTMLResponse('<span class="noot">Geen concrete acties gevonden in dit antwoord.</span>')
    return HTMLResponse(f'<span class="noot">✓ {len(acties)} {"actie" if len(acties) == 1 else "acties"} op de <a href="/adviseur#acties">actielijst</a> gezet.</span>')


@router.post("/adviseur/doelen")
def doelen_opslaan(request: Request, waarde: str = Form(""), wekelijks: str = Form(""), standaard: str = Form("")):
    controleer_herkomst(request)
    if standaard:
        q("DELETE FROM instellingen WHERE sleutel = 'advies_doelen' RETURNING 1")
    else:
        zet_instelling("advies_doelen", opmaak.zonder_streepjes(waarde.strip()) or DOELEN)
    zet_instelling("advies_wekelijks", "1" if wekelijks else "0")
    return RedirectResponse("/adviseur?doelen=1#doelen", status_code=303)
