"""De bouwstenen van een dagelijks overzicht.

Elk onderdeel haalt zijn eigen gegevens op en geeft een sectie terug: {"items": [...], "regel": "", "link": (tekst, url)}.
Een item heeft titel en verder (allemaal optioneel): url (openbaar adres), intern (adres in Buurmanitor, alleen voor
Martijn zelf), label (vet ervoor), wanneer, bron (grijs erachter), tekst (toelichting) en let_op (rood).

- alleen_eigen: alleen in het exemplaar voor Martijn zelf (agenda, taken, concepten, omzet). Fractieleden en andere
  ontvangers krijgen het niet.
- per_ontvanger: per ontvanger apart opgehaald (de eigen deadlines en agendapunten van een fractielid).
Links naar Buurmanitor (intern) gaan nooit naar andere ontvangers: die kunnen daar niet inloggen.
"""
import datetime as dt
import os
from dataclasses import dataclass, field

from ..kern import config

PORTAAL = f"https://{config.PORTAL_HOST}"
POLITIEK = os.environ.get("POLITIEK_BASIS_URL", f"{PORTAAL}/politiek").rstrip("/")
ZEROHERO = os.environ.get("ZEROHERO_BASIS_URL", f"{PORTAAL}/zerohero").rstrip("/")
DAGEN_KORT = ["ma", "di", "wo", "do", "vr", "za", "zo"]
SOORTEN = {"raadsvoorstel": "Raadsvoorstel", "rib": "RIB", "vraag": "Raadsvraag", "motie": "Motie", "toezegging": "Toezegging",
           "ingekomen": "Ingekomen", "besloten": "Alleen raadsleden"}


@dataclass
class Onderdeel:
    code: str
    werkruimte: str  # politiek, zerohero of beide
    naam: str
    uitleg: str
    titel: str
    functie: object = None
    max: int = 8
    alleen_eigen: bool = False
    per_ontvanger: bool = False
    opties: tuple = field(default_factory=tuple)  # (sleutel, label, soort 'getal' of 'vink', standaard)


ONDERDELEN = {}


def onderdeel(code, werkruimte, naam, uitleg, titel, **kw):
    def registreer(functie):
        ONDERDELEN[code] = Onderdeel(code, werkruimte, naam, uitleg, titel, functie, **kw)
        return functie
    return registreer


def voor_werkruimte(werkruimte):
    return [o for o in ONDERDELEN.values() if o.werkruimte in (werkruimte, "beide")]


def optie(instelling, o, sleutel):
    """De waarde van een optie, of de standaard. Pure functie."""
    standaard = next((d for s, _l, _t, d in o.opties if s == sleutel), None)
    waarde = (instelling.get("opties") or {}).get(sleutel, standaard)
    if isinstance(standaard, bool):
        return bool(waarde)
    if isinstance(standaard, int):
        try:
            return int(waarde)
        except (TypeError, ValueError):
            return standaard
    return waarde


class Omgeving:
    """Wat een onderdeel nodig heeft: de database van Buurmanitor, het moment, en een kleine cache (zodat de agenda van
    Promonitor bij tien ontvangers maar één keer wordt opgebouwd)."""

    def __init__(self, c, overzicht, nu=None):
        self.c = c
        self.overzicht = overzicht
        self.nu = nu or dt.datetime.now(config.TZ)
        self.vandaag = self.nu.date()
        self.cache = {}

    def bewaard(self, sleutel, maak):
        """Eén keer ophalen; ook een fout onthouden, zodat een onbereikbare Notubiz niet tien ontvangers lang wordt geprobeerd."""
        if sleutel not in self.cache:
            try:
                self.cache[sleutel] = (True, maak())
            except Exception as e:  # noqa: BLE001
                self.cache[sleutel] = (False, e)
        gelukt, waarde = self.cache[sleutel]
        if not gelukt:
            raise waarde
        return waarde


class NietBeschikbaar(Exception):
    pass


def _politiek_pool():
    if not config.POLITIEK_ACTIEF:
        raise NietBeschikbaar("Promonitor draait niet in deze Buurmanitor")
    from ..politiek import main as pm  # pylint: disable=import-outside-toplevel
    return pm.pool


def _zerohero():
    if not config.ZEROHERO_ACTIEF:
        raise NietBeschikbaar("ZEROHERO draait niet in deze Buurmanitor")
    from ..zerohero import main as zh  # pylint: disable=import-outside-toplevel
    return zh


def politiek_instellingen(omg):
    def maak():
        with _politiek_pool().connection() as pc:
            return {r["sleutel"]: r["waarde"] or "" for r in pc.execute("SELECT sleutel, waarde FROM instellingen").fetchall()}
    return omg.bewaard("pinst", maak)


def _agenda(omg, dagen):
    """Vergaderingen met agendapunten en deadlines uit Promonitor, voor de komende dagen (gedeeld door alle ontvangers)."""
    def maak():
        from ..politiek import agenda  # pylint: disable=import-outside-toplevel
        with _politiek_pool().connection() as pc:
            personen = pc.execute("""SELECT naam, whaller, rol, portefeuilles FROM personen
                                     WHERE actief AND coalesce(whaller, '') <> '' ORDER BY volgorde, naam""").fetchall()
            verg, _fouten = agenda.overzicht(pc, omg.vandaag, omg.vandaag + dt.timedelta(days=21), politiek_instellingen(omg), personen)
        return verg
    return omg.bewaard("pagenda", maak)


def _ik(omg, ontvanger):
    """De Whaller-naam van de ontvanger (bij Martijn zelf: die uit de instellingen van Promonitor)."""
    if not ontvanger:
        return ""
    if ontvanger.get("persoon"):
        return ontvanger["persoon"]
    return politiek_instellingen(omg).get("agenda_ik", "") if ontvanger.get("rol") == "eigen" else ""


def wanneer_dag(d, vandaag):
    """'vandaag', 'morgen', 'verlopen' of 'wo 18-11'. Pure functie."""
    if d < vandaag:
        return "verlopen"
    if d == vandaag:
        return "vandaag"
    if d == vandaag + dt.timedelta(days=1):
        return "morgen"
    return f"{DAGEN_KORT[d.weekday()]} {d:%d-%m}"


# ---------------------------------------------------------------- Politiek

@onderdeel("deadlines", "politiek", "Jouw deadlines",
           "Open stappen van je eigen agendapunten (stukken lezen, analyse, bijdrage) met een deadline. Per ontvanger: elk "
           "fractielid ziet zijn eigen deadlines, als je bij de ontvanger de persoon uit Promonitor kiest.",
           "Jouw deadlines", per_ontvanger=True, opties=(("dagen", "Tot hoeveel dagen vooruit", "getal", 1),))
def _deadlines(omg, inst, ontvanger=None):
    from ..politiek import agenda  # pylint: disable=import-outside-toplevel
    ik = _ik(omg, ontvanger)
    if not ik:
        return {"items": []}
    lijst = agenda.mijn_deadlines(_agenda(omg, 21), ik, omg.vandaag, dagen=optie(inst, ONDERDELEN["deadlines"], "dagen"))
    return {"items": [{"label": wanneer_dag(d["datum"], omg.vandaag), "titel": f"{d['naam']} bij {d['punt']['onderwerp']}",
                       "bron": f"{d['punt']['vergadering']} {d['punt']['datum']:%d-%m}", "let_op": "verlopen" if d["verlopen"] else ""}
                      for d in lijst], "link": ("Naar de Agenda", f"{POLITIEK}/agenda")}


@onderdeel("woordvoering", "politiek", "Jouw agendapunten",
           "De agendapunten waarvoor de ontvanger woordvoerder is of erbij zit, in de komende dagen. Per ontvanger.",
           "Jouw agendapunten", per_ontvanger=True, opties=(("dagen", "Hoeveel dagen vooruit", "getal", 14),))
def _woordvoering(omg, inst, ontvanger=None):
    from ..politiek import agenda  # pylint: disable=import-outside-toplevel
    ik = _ik(omg, ontvanger)
    if not ik:
        return {"items": []}
    tot = omg.vandaag + dt.timedelta(days=optie(inst, ONDERDELEN["woordvoering"], "dagen"))
    items = []
    for v in _agenda(omg, 21):
        for p in v["punten"]:
            if agenda.van_mij(p, ik) and omg.vandaag <= p["datum"] <= tot:
                open_ = ", ".join(agenda.STAP_NAAM[s].lower() for s in p["open"]) if p.get("open") else ""
                items.append({"wanneer": wanneer_dag(p["datum"], omg.vandaag), "titel": p["onderwerp"],
                              "bron": v["naam"] + (" (woordvoerder)" if p.get("woordvoerder") == ik else " (zit erbij)"),
                              "tekst": f"Nog te doen: {open_}" if open_ else "Klaar"})
    return {"items": items, "link": ("Naar de Agenda", f"{POLITIEK}/agenda")}


@onderdeel("nieuws", "politiek", "Nieuws", "Het belangrijkste nieuws (gewogen door Claude) van de afgelopen periode.", "Nieuws",
           opties=(("drempel", "Vanaf score", "getal", 60), ("uren", "Van de afgelopen uren", "getal", 24),
                   ("toelichting", "Met de reden erbij", "vink", True)))
def _nieuws(omg, inst, ontvanger=None):
    o = ONDERDELEN["nieuws"]
    with _politiek_pool().connection() as pc:
        rijen = pc.execute("""SELECT * FROM stukken WHERE bron = 'nieuws' AND score >= %s AND status <> 'genegeerd'
                                AND toegevoegd > now() - make_interval(hours => %s) ORDER BY score DESC, id DESC LIMIT 30""",
                           (optie(inst, o, "drempel"), optie(inst, o, "uren"))).fetchall()
    return {"items": [{"label": str(s["score"]), "titel": s["titel"], "url": s["document_url"], "bron": s["bronnaam"] or "",
                       "tekst": (s["reden"] or "") if optie(inst, o, "toelichting") else ""} for s in rijen],
            "link": ("Naar de Inbox", f"{POLITIEK}/inbox")}


@onderdeel("antwoorden", "politiek", "Antwoord op onze vragen", "Antwoorden op raadsvragen van de fractie die het afgelopen etmaal binnenkwamen.",
           "Antwoord op onze vragen")
def _antwoorden(omg, inst, ontvanger=None):
    with _politiek_pool().connection() as pc:
        rijen = pc.execute("""SELECT * FROM stukken WHERE bron = 'notubiz' AND soort = 'vraag' AND pro
                                AND bijgewerkt > now() - interval '24 hours'
                                AND (coalesce((extra->>'beantwoord')::boolean, false) OR context LIKE 'beantwoord%%')
                              ORDER BY nummer""").fetchall()
    return {"items": [{"titel": s["titel"], "url": (s["extra"] or {}).get("antwoord_url") or s["document_url"]} for s in rijen],
            "link": ("Alle vragen", f"{POLITIEK}/vragen?toon=beantwoord")}


@onderdeel("raadsinformatie", "politiek", "Raadsinformatie",
           "Nieuwe raadsvoorstellen, raadsinformatiebrieven, raadsvragen, moties en ingekomen stukken (eerst wat PRO raakt).",
           "Raadsinformatie", max=10, opties=(("toelichting", "Met een stukje inhoud", "vink", True),))
def _raadsinformatie(omg, inst, ontvanger=None):
    with _politiek_pool().connection() as pc:
        rijen = pc.execute("""SELECT * FROM stukken WHERE bron = 'notubiz' AND soort NOT IN ('bijlage', 'besloten') AND status <> 'genegeerd'
                                AND (NOT gezien OR pro) AND coalesce(bijgewerkt, toegevoegd) > now() - interval '24 hours'
                                AND NOT (soort = 'vraag' AND pro)
                              ORDER BY pro DESC, datum DESC NULLS LAST LIMIT 30""").fetchall()
    toelichting = optie(inst, ONDERDELEN["raadsinformatie"], "toelichting")
    return {"items": [{"label": ("PRO · " if s["pro"] else "") + SOORTEN.get(s["soort"], s["soort"]), "titel": s["titel"],
                       "url": s["document_url"], "tekst": (s["context"] or "")[:220] if toelichting else ""} for s in rijen],
            "link": ("Naar de Inbox", f"{POLITIEK}/inbox")}


@onderdeel("vergaderingen", "politiek", "Vergaderingen", "De vergaderingen van de gemeenteraad in de komende dagen (Notubiz).",
           "Vandaag en morgen", opties=(("dagen", "Hoeveel dagen (1 = alleen vandaag)", "getal", 2),))
def _vergaderingen(omg, inst, ontvanger=None):
    _politiek_pool()  # werkt Promonitor hier niet, dan een duidelijke melding
    from ..politiek import notubiz  # pylint: disable=import-outside-toplevel
    dagen = max(1, optie(inst, ONDERDELEN["vergaderingen"], "dagen"))
    with _politiek_pool().connection() as pc:
        besloten = {r["extern_id"] for r in pc.execute("SELECT extern_id FROM stukken WHERE soort = 'besloten'").fetchall()}
    lijst = omg.bewaard(("ev", dagen), lambda: notubiz.evenementen(omg.vandaag, omg.vandaag + dt.timedelta(days=dagen - 1)))
    return {"items": [{"wanneer": f"{DAGEN_KORT[v['start'].weekday()]} {v['start']:%H.%M}", "titel": v["naam"], "url": v.get("url") or "",
                       "let_op": "alleen met inloggen" if v.get("alleen_leden") or f"leden-{v['id']}" in besloten else ""}
                      for v in lijst]}


@onderdeel("whaller", "politiek", "Whaller-berichten",
           "Op de plaatsdag (instelling in Promonitor) een seintje dat de Whaller-berichten voor volgende week klaarstaan.",
           "Whaller", alleen_eigen=True)
def _whaller(omg, inst, ontvanger=None):
    _politiek_pool()
    from ..politiek import whaller  # pylint: disable=import-outside-toplevel
    plaatsdag = (politiek_instellingen(omg).get("whaller_plaatsdag") or "woensdag").lower()
    if whaller.DAGEN[omg.vandaag.weekday()] != plaatsdag:
        return {"items": []}
    return {"items": [], "regel": "De berichten voor de vergaderingen van volgende week staan klaar.",
            "link": ("Open Whaller in Promonitor", f"{POLITIEK}/whaller")}


# ---------------------------------------------------------------- ZEROHERO

@onderdeel("keepersnieuws", "zerohero", "Keepersnieuws", "Het hoogst gewogen keepersnieuws uit de Doellijn.", "Keepersnieuws",
           opties=(("drempel", "Vanaf score", "getal", 60), ("uren", "Van de afgelopen uren", "getal", 24),
                   ("toelichting", "Met samenvatting", "vink", True)))
def _keepersnieuws(omg, inst, ontvanger=None):
    o = ONDERDELEN["keepersnieuws"]
    zh = _zerohero()
    with zh.pool.connection() as zc:
        rijen = zc.execute("""SELECT * FROM items WHERE binnen > now() - make_interval(hours => %s) AND score >= %s
                                AND status = ANY(%s) AND coalesce(url, '') ~ '^https?://'
                              ORDER BY score DESC, binnen DESC LIMIT 30""",
                           (optie(inst, o, "uren"), optie(inst, o, "drempel"), list(zh.STATUS_OPEN))).fetchall()
    return {"items": [{"label": str(r["score"]), "titel": r["titel"], "url": r["url"],
                       "bron": " · ".join(x for x in (r.get("bron"), r.get("sport")) if x),
                       "tekst": (r.get("samenvatting") or r.get("invalshoek") or "")[:260] if optie(inst, o, "toelichting") else ""}
                      for r in rijen], "link": ("Naar Vandaag in ZEROHERO", f"{ZEROHERO}/")}


@onderdeel("concepten", "zerohero", "Concepten klaar", "Concepten (blog, LinkedIn, Instagram) die klaarstaan om te bekijken en te publiceren.",
           "Klaar om te bekijken", alleen_eigen=True)
def _concepten(omg, inst, ontvanger=None):
    with _zerohero().pool.connection() as zc:
        rijen = zc.execute("""SELECT id, blog_titel, klaar_op FROM concepten WHERE status = 'klaar' AND gepubliceerd_op IS NULL
                              ORDER BY klaar_op DESC NULLS LAST LIMIT 20""").fetchall()
    return {"items": [{"titel": r["blog_titel"] or f"Concept {r['id']}", "intern": f"{ZEROHERO}/concepten/{r['id']}"} for r in rijen],
            "link": ("Alle concepten", f"{ZEROHERO}/concepten")}


@onderdeel("kaartverkoop", "zerohero", "Kaartverkoop congres", "Verkochte kaarten (Ticket Tailor) in het afgelopen etmaal en in totaal.",
           "Kaartverkoop", alleen_eigen=True)
def _kaartverkoop(omg, inst, ontvanger=None):
    totaal, dag = _zerohero().kaartverkoop()
    if not totaal or not totaal["kaarten"]:
        return {"items": []}
    euro = lambda cent: f"€ {cent / 100:,.0f}".replace(",", ".")  # noqa: E731
    return {"items": [], "regel": f"Afgelopen 24 uur: {dag['kaarten']} kaarten ({euro(dag['omzet'])}). "
                                  f"Totaal: {totaal['kaarten']} kaarten ({euro(totaal['omzet'])}).",
            "link": ("Naar het congres", f"{ZEROHERO}/congres")}


@onderdeel("zh_kansen", "zerohero", "Nieuwe kansen", "Nieuwe kansen in het kansenlogboek (lezingen, media, podcasts, samenwerkingen).",
           "Nieuwe kansen", alleen_eigen=True, opties=(("dagen", "Van de afgelopen dagen", "getal", 7),))
def _zh_kansen(omg, inst, ontvanger=None):
    with _zerohero().pool.connection() as zc:
        rijen = zc.execute("""SELECT * FROM kansen WHERE status = 'binnen' AND aangemaakt > now() - make_interval(days => %s)
                              ORDER BY aangemaakt DESC LIMIT 20""", (optie(inst, ONDERDELEN["zh_kansen"], "dagen"),)).fetchall()
    return {"items": [{"label": r["soort"], "titel": r["organisatie"], "tekst": r["omschrijving"] or ""} for r in rijen],
            "link": ("Naar het dashboard", f"{ZEROHERO}/dashboard")}


# ---------------------------------------------------------------- beide (uit Buurmanitor zelf, alleen voor Martijn)

@onderdeel("agenda", "beide", "Jouw afspraken vandaag", "Je afspraken van vandaag uit Clara (alle gekoppelde agenda's).", "Jouw afspraken vandaag",
           max=12, alleen_eigen=True, opties=(("alleen_domein", "Alleen afspraken van dit domein", "vink", False),))
def _agenda_vandaag(omg, inst, ontvanger=None):
    from ..clara import sync  # pylint: disable=import-outside-toplevel
    begin = dt.datetime.combine(omg.vandaag, dt.time(0, 0), tzinfo=config.TZ)
    alleen = optie(inst, ONDERDELEN["agenda"], "alleen_domein")
    items = []
    for a in sync.afspraken(omg.c, begin, begin + dt.timedelta(days=1)):
        if a.get("status") == "geannuleerd" or a.get("antwoord") == "afgewezen" or (a.get("niet_voor_mij") and not a.get("mee")):
            continue
        if alleen and a.get("domein") != omg.overzicht["werkruimte"]:
            continue
        start = a["start"].astimezone(config.TZ)
        items.append({"wanneer": "hele dag" if a.get("heledag") else f"{start:%H.%M}", "titel": a["titel"] or "(geen titel)",
                      "bron": a.get("locatie") or ""})
    return {"items": items, "link": ("Naar de agenda", f"{PORTAAL}/clara")}


@onderdeel("taken", "beide", "Taken voor vandaag", "Taken uit Buurmanitor met een deadline vandaag, of die al te laat zijn.", "Taken voor vandaag",
           max=10, alleen_eigen=True, opties=(("alleen_domein", "Alleen taken bij een kans of project van dit domein", "vink", False),))
def _taken(omg, inst, ontvanger=None):
    alleen = optie(inst, ONDERDELEN["taken"], "alleen_domein")
    rijen = omg.c.execute("""SELECT t.id, t.titel, t.einddatum, t.prio FROM takenbord t
                             LEFT JOIN crm_projecten p ON p.id = t.project_id LEFT JOIN crm_kansen k ON k.id = t.kans_id
                             WHERE t.status <> 'afgerond' AND t.gearchiveerd IS NULL AND t.einddatum <= %s
                               AND (NOT %s OR coalesce(p.activiteit, k.activiteit) = %s)
                             ORDER BY t.einddatum, t.prio = 'hoog' DESC, t.id""",
                          (omg.vandaag, alleen, omg.overzicht["werkruimte"])).fetchall()
    return {"items": [{"label": wanneer_dag(r["einddatum"], omg.vandaag), "titel": r["titel"], "intern": f"{PORTAAL}/taken?bewerk={r['id']}#formulier",
                       "let_op": "te laat" if r["einddatum"] < omg.vandaag else ""} for r in rijen],
            "link": ("Naar Taken", f"{PORTAAL}/taken")}


@onderdeel("crm", "beide", "Kansen om op te volgen", "Kansen in het CRM van dit domein waarvan de volgende stap vandaag of eerder gepland staat.",
           "Kansen om op te volgen", alleen_eigen=True)
def _crm(omg, inst, ontvanger=None):
    rijen = omg.c.execute("""SELECT id, titel, volgende_stap, volgende_datum FROM crm_kansen
                             WHERE gesloten IS NULL AND activiteit = %s AND volgende_datum <= %s ORDER BY volgende_datum, id""",
                          (omg.overzicht["werkruimte"], omg.vandaag)).fetchall()
    return {"items": [{"label": wanneer_dag(r["volgende_datum"], omg.vandaag), "titel": r["titel"], "tekst": r["volgende_stap"],
                       "intern": f"{PORTAAL}/crm/kans/{r['id']}"} for r in rijen], "link": ("Naar het CRM", f"{PORTAAL}/crm")}


@onderdeel("samenvatting", "beide", "Samenvatting door Claude",
           "Twee tot vier zinnen bovenaan over wat vandaag het belangrijkst is, geschreven door Claude op basis van de andere "
           "onderdelen. Alleen openbare onderdelen gaan naar Claude, nooit je agenda, taken of interne stukken.",
           "Vandaag belangrijk", max=1)
def _samenvatting(omg, inst, ontvanger=None):
    return {"items": []}  # wordt bij het samenstellen gevuld, als laatste (zie overzichten.verzenden)


# Standaard-opbouw van een nieuw overzicht (de huidige ochtendbriefing van Promonitor, en een begin voor ZEROHERO).
STANDAARD = {
    "politiek": ["samenvatting", "deadlines", "woordvoering", "nieuws", "antwoorden", "raadsinformatie", "vergaderingen", "whaller",
                 "agenda", "taken", "crm"],
    "zerohero": ["samenvatting", "keepersnieuws", "concepten", "kaartverkoop", "zh_kansen", "agenda", "taken", "crm"],
}
STANDAARD_AAN = {"deadlines", "nieuws", "antwoorden", "raadsinformatie", "vergaderingen", "whaller", "keepersnieuws", "concepten",
                 "kaartverkoop", "zh_kansen"}


def standaard_onderdelen(werkruimte):
    return [{"code": code, "aan": code in STANDAARD_AAN, "titel": ONDERDELEN[code].titel, "max": ONDERDELEN[code].max, "opties": {}}
            for code in STANDAARD[werkruimte]]


def aangevuld(werkruimte, onderdelen):
    """De opgeslagen onderdelen, aangevuld met nieuwe onderdelen die er sinds het opslaan bij kwamen (uit). Pure functie."""
    bekend = {o.get("code") for o in onderdelen}
    uit = [o for o in onderdelen if o.get("code") in ONDERDELEN and ONDERDELEN[o["code"]].werkruimte in (werkruimte, "beide")]
    for o in voor_werkruimte(werkruimte):
        if o.code not in bekend:
            uit.append({"code": o.code, "aan": False, "titel": o.titel, "max": o.max, "opties": {}})
    return uit
