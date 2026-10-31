"""Praten met Clara. Ze krijgt de komende twee weken mee (alle agenda's samen), met dubbele boekingen, krappe
overgangen, open uitnodigingen en vrije blokken. Stelt ze een tijdstip voor, dan zet ze dat als voorstel klaar;
in de agenda komt het pas als Martijn op de knop drukt."""
import datetime as dt
import json
import re

from .. import personas
from ..kern import claude, config
from ..kern.web import DAGEN, datum_lang
from . import analyse, reis, sync

VOORSTEL = re.compile(r"<voorstel>\s*(\{.*?\})\s*</voorstel>", re.S)
VERVOER_TAG = re.compile(r"<vervoer>\s*(\{.*?\})\s*</vervoer>", re.S)
UITLEG = """# Voorstellen doen
Stel je een concreet tijdstip voor om iets in te plannen (een schrijfblok, voorbereiding, reistijd), zet dan per
voorstel aan het eind van je antwoord precies één regel:
<voorstel>{"titel": "...", "start": "JJJJ-MM-DDTUU:MM", "eind": "JJJJ-MM-DDTUU:MM", "domein": "politiek|prive|zerohero|fbim|upperspire", "agenda": "naam van de agenda"}</voorstel>
"agenda" is de naam van een agenda uit de lijst hieronder waarin je kunt inplannen, bijvoorbeeld "Sanne" voor een
afspraak voor Sanne (de dochter van Martijn). Laat "agenda" weg als het voor Martijn zelf is.
Tijden in Nederlandse tijd. Martijn ziet het als knop en beslist zelf.

# Vervoer per afspraak
Zegt Martijn uitdrukkelijk hoe hij naar een afspraak gaat (bijvoorbeeld "naar Utrecht ga ik met de trein"), leg dat
dan vast met één regel per afspraak, met het id uit de reistijdlijst:
<vervoer>{"id": 123, "vervoer": "fiets|auto|ov|lopen|geen|automatisch"}</vervoer>
"geen" betekent geen reistijd (hij wordt opgehaald of is er al); "automatisch" zet de vaste regels terug. Bij een
terugkerende afspraak geldt de keuze voor de hele reeks.
Afspraken uit een agenda die niet bezet is voor Martijn (zoals die van Sanne) krijgen geen reistijd, tenzij hij
meegaat. Zegt hij dat hij meegaat of haar brengt (bijvoorbeeld "naar de manege rijd ik met de auto"), leg dan het
vervoer vast met het id dat bij die afspraak staat; vanaf dan telt de afspraak als de zijne. "automatisch" of "geen"
betekent dat hij niet (meer) meegaat.
De reistijden zelf rekent het systeem uit met de routedienst (Google Maps of OpenRouteService); jij hoeft en kunt dat
niet zelf. Leg je een vervoerskeuze vast, zeg dan dat de nieuwe reistijd binnen een paar minuten in de week staat.
Zeg niet dat je geen toegang hebt tot routes of het thuisadres: dat regelt het systeem. Doe dit alleen als hij het zelf zegt, niet uit jezelf. Je accepteert, verplaatst of verwijdert zelf
nooit iets en zegt ook niet dat je dat gedaan hebt. Gebruik alleen tijden die in de vrije blokken passen, tenzij
Martijn iets anders vraagt."""


def _regel(a, tz):
    s, e = a["start"].astimezone(tz), a["eind"].astimezone(tz)
    tijd = "hele dag" if a.get("heledag") else f"{s:%H.%M}-{e:%H.%M}" + (" (einde geschat)" if a.get("geschat") else "")
    extra = [x for x in (a.get("locatie"), "online" if a.get("online") else "",
                         {"geen": "uitnodiging, nog niet beantwoord", "voorlopig": "voorlopig", "afgewezen": "afgewezen"}.get(a.get("antwoord"), ""),
                         "vrij" if a.get("vrij") else "") if x]
    if a.get("niet_voor_mij"):
        extra = [x for x in extra if x != "vrij"] + ["niet bezet voor Martijn, hij gaat niet mee"]
        if a.get("locatie") and not a.get("online") and not a.get("heledag") and a.get("id"):
            extra.append(f"id {a['id']} voor vervoer als hij meegaat")
    elif a.get("mee"):
        extra.append("Martijn gaat mee")
    return f"- {tijd} {a['titel']} [{sync.DOMEINEN.get(a.get('domein'), a.get('domein'))}, {a.get('agenda_naam') or a.get('koppeling')}]" + (
        f" ({'; '.join(extra)})" if extra else "")


def context(c, nu=None):
    tz = config.TZ
    nu = nu or dt.datetime.now(tz)
    inst = sync.instellingen(c)
    tot = nu + dt.timedelta(days=14)
    lijst = sync.afspraken(c, dt.datetime.combine(nu.date(), dt.time(0), tzinfo=tz), tot)
    kop = [f"Het is {datum_lang(nu.date())} {nu.year}, {nu:%H.%M} uur."]
    koppelingen = sync.koppelingen(c, alleen_actief=True)
    if not koppelingen:
        return "\n".join(kop + ["Er zijn nog geen agenda's gekoppeld. Zeg dat, en verwijs naar Clara > Koppelingen."])
    kop.append("Gekoppelde agenda's: " + "; ".join(
        f"{k['naam']} ({k['soort']}{', ' + 'lezen en schrijven' if k['schrijven'] else ', alleen lezen'})"
        + (f" FOUT: {k['fout']}" if k["fout"] else "") for k in koppelingen))
    schrijfbaar = sorted({a["naam"] for k in koppelingen if k["schrijven"] and k["soort"] in ("google", "microsoft")
                          for a in (k["agendas"] or []) if a.get("schrijven") and (a.get("aan") or a["id"] == k["schrijfagenda"])})
    if schrijfbaar:
        kop.append("Agenda's waarin Martijn (na zijn klik) kan inplannen: " + ", ".join(schrijfbaar) + ".")
    regels = sync.verbergregels(c)
    if regels:
        kop.append("Martijn laat deze afspraken bewust weg (ze staan hieronder niet): " + "; ".join(
            " en ".join(x for x in (f"organisator bevat '{r['organisator']}'" if r.get("organisator") else "",
                                    f"tekst bevat '{r['tekst']}'" if r.get("tekst") else "") if x) for r in regels) + ".")
    kop.append(f"Werktijden: {inst['begin']} tot {inst['eind']} op werkdagen" + (", en 19.00 tot 22.00 's avonds" if inst["avond"] else "")
               + f". Reistijd tussen plekken: {inst['reistijd']} minuten.")
    delen = ["\n".join(kop), "# Agenda komende twee weken"]
    for dag, items in analyse.per_dag(lijst, tz).items():
        delen.append(f"{DAGEN[dag.weekday()].capitalize()} {dag:%d-%m}:\n" + "\n".join(_regel(a, tz) for a in items))
    conf = analyse.conflicten(lijst)
    if conf:
        delen.append("# Dubbele boekingen\n" + "\n".join(
            f"- {x['van'].astimezone(tz):%d-%m %H.%M}: {x['a']['titel']} en {x['b']['titel']}" for x in conf))
    over = analyse.overgangen(lijst, inst["reistijd"])
    if over:
        delen.append("# Krappe overgangen\n" + "\n".join(
            f"- {x['van']['eind'].astimezone(tz):%d-%m %H.%M}: {x['minuten']} min van {x['van']['locatie']} naar {x['naar']['locatie']}"
            for x in over))
    try:
        plan = sync.reisplan(c, nu, tot)
    except Exception:  # noqa: BLE001  (reistijd is een extra; het gesprek gaat altijd door)
        plan = {"regels": []}
    if plan.get("regels"):
        delen.append(f"# Reistijd (berekend met {reis.dienst_naam()}"
                     + ("; OV is een schatting" if reis.dienst() == "ors" else "; OV volgens de dienstregeling") + ")\n" + "\n".join(
            f"- [id {r['afspraak']['id']}] {r['afspraak']['titel']} ({r['afspraak']['start'].astimezone(tz):%d-%m %H.%M}): {r['richting']} "
            f"{r['minuten']} min {r['vervoer']} {'vanaf ' + r['van'] if r['richting'] == 'heen' else 'naar huis'}"
            f"{' (zelf gekozen)' if r.get('gekozen') else ''}{', ingepland' if r['ingepland'] else ''}"
            f"{'' if r['past'] else ', PAST NIET (' + r['botst'] + ' loopt nog)'}"
            for r in plan["regels"] if r.get("minuten")) + "".join(
            f"\n- [id {r['afspraak']['id']}] {r['afspraak']['titel']}: geen reistijd (zelf gekozen)"
            for r in plan["regels"] if r.get("vervoer") == "geen"))
    extra = [{"start": r["start"], "eind": r["eind"], "heledag": False, "status": "bevestigd", "vrij": False, "antwoord": "eigen"}
             for r in plan.get("regels", []) if r.get("minuten") and not r["ingepland"]]
    vrij = analyse.vrije_blokken(lijst + extra, nu, 14, 60, inst, tz, max_aantal=20)
    if vrij:
        delen.append("# Vrije blokken (minstens een uur, binnen werktijden)\n" + "\n".join(
            f"- {DAGEN[b['start'].weekday()]} {b['start']:%d-%m %H.%M} (ruimte {b['ruimte']} min)" for b in vrij))
    return "\n\n".join(delen)


def voorstellen_uit(tekst, tz=config.TZ):
    """Haal de voorstellen uit het antwoord. Geeft (tekst zonder voorstel-regels, lijst voorstellen)."""
    uit = []
    for m in VOORSTEL.finditer(tekst):
        try:
            v = json.loads(m.group(1))
            start = dt.datetime.fromisoformat(v["start"]).replace(tzinfo=tz)
            eind = dt.datetime.fromisoformat(v["eind"]).replace(tzinfo=tz)
        except (ValueError, KeyError, TypeError):
            continue
        if eind > start and (eind - start) <= dt.timedelta(hours=12):
            uit.append({"titel": str(v.get("titel") or "Blok")[:200], "start": start, "eind": eind,
                        "domein": v.get("domein") if v.get("domein") in sync.DOMEINEN else "",
                        "agenda": str(v.get("agenda") or "").strip()[:80]})
    return VOORSTEL.sub("", tekst).strip(), uit


def vervoer_uit(c, tekst):
    """Vervoerskeuzes uit het antwoord halen en vastleggen. Geeft (tekst zonder die regels, lijst met bevestigingen)."""
    from . import reis  # pylint: disable=import-outside-toplevel
    klaar = []
    for m in VERVOER_TAG.finditer(tekst):
        try:
            v = json.loads(m.group(1))
            a = c.execute("SELECT * FROM agenda_items WHERE id = %s", (int(v["id"]),)).fetchone()
        except (ValueError, KeyError, TypeError):
            continue
        if a:
            gekozen = reis.kies(c, a, str(v.get("vervoer", "")))
            klaar.append(f"{a['titel']}: {reis.VERVOER[gekozen] if gekozen else 'automatisch'}")
    return VERVOER_TAG.sub("", tekst).strip(), klaar


def antwoord(c, gesprek_id):
    berichten = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id",
                          (gesprek_id,)).fetchall()
    systeem = personas.systeemtekst(personas.get("clara")) + "\n\n" + UITLEG + "\n\n# Wat je nu weet\n" + context(c)
    tekst = claude.gesprek(c, "clara", systeem, [{"role": b["rol"], "content": b["tekst"]} for b in berichten if (b["tekst"] or "").strip()][-30:],
                           max_tokens=1500)
    tekst = tekst.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    tekst, voorstellen = voorstellen_uit(tekst)
    tekst, vervoer = vervoer_uit(c, tekst)
    if vervoer:
        tekst = (tekst + "\n\n" + "\n".join(f"(Vastgelegd: {v})" for v in vervoer)).strip()
    if not tekst:
        tekst = "Hier is mijn voorstel." if voorstellen else "(Geen antwoord ontvangen. Probeer het nog eens.)"
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    for v in voorstellen:
        c.execute("INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, domein, agenda) VALUES (%s, %s, %s, %s, %s, %s)",
                  (gesprek_id, v["titel"], v["start"], v["eind"], v["domein"], v.get("agenda", "")))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst
