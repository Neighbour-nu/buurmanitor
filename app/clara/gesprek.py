"""Praten met Clara. Ze krijgt de komende twee weken mee (alle agenda's samen), met dubbele boekingen, krappe
overgangen, open uitnodigingen en vrije blokken. Stelt ze een tijdstip voor, dan zet ze dat als voorstel klaar;
in de agenda komt het pas als Martijn op de knop drukt."""
import datetime as dt
import json
import re

from .. import personas
from ..kern import claude, config
from ..kern.web import DAGEN, datum_lang
from . import analyse, bronnen, reis, sync

VOORSTEL = re.compile(r"<voorstel>\s*(\{.*?\})\s*</voorstel>", re.S)
VERVOER_TAG = re.compile(r"<vervoer>\s*(\{.*?\})\s*</vervoer>", re.S)
REEKS_TAG = re.compile(r"<reeks>\s*(\{.*?\})\s*</reeks>", re.S)
UITLEG = """# Voorstellen doen
Stel je een concreet tijdstip voor om iets in te plannen (een schrijfblok, voorbereiding, reistijd), zet dan per
voorstel aan het eind van je antwoord precies één regel:
<voorstel>{"titel": "...", "start": "JJJJ-MM-DDTUU:MM", "eind": "JJJJ-MM-DDTUU:MM", "domein": "politiek|prive|zerohero|fbim|upperspire", "agenda": "naam van de agenda"}</voorstel>
"agenda" is de naam van een agenda uit de lijst hieronder waarin je kunt inplannen, bijvoorbeeld "Sanne" voor een
afspraak voor Sanne (de dochter van Martijn). Laat "agenda" weg als het voor Martijn zelf is.
Tijden in Nederlandse tijd. Martijn ziet het als knop en beslist zelf.

# Herhalende afspraken
Voor iets dat elke week terugkomt, zet je in het voorstel ook "herhaal": {"dagen": ["wo", "za"], "tot": "JJJJ-MM-DD"}.
Dagen: ma, di, wo, do, vr, za, zo. "tot" is de laatste dag; laat het weg als er geen einddatum is. "start" en "eind"
zijn de eerste keer. Alle dagen in één voorstel krijgen dezelfde tijden: maak dus een apart voorstel per groep dagen
met andere tijden.
Sanne wegbrengen en ophalen (bijvoorbeeld bij paardrijles) zet je als blokken in Martijns eigen agenda (laat "agenda"
weg), zonder adres, met een titel als "🚗 Sanne naar paardrijles brengen" of "🚗 Sanne ophalen bij paardrijles": van
vertrek tot hij weer thuis is (heen en terug). Wegbrengen eindigt dus een reistijd na het begin van de les, ophalen
begint een reistijd voor het einde. Gebruik de reistijden vanaf huis die hieronder staan; staat er geen, zeg dan dat
het een schatting is. Blijft Martijn de hele les, dan is het geen wegbrengen maar meegaan: leg dan vervoer vast.
Een bestaande reeks van Clara (lijst hieronder, met [reeks N]) wijzig je met een nieuw voorstel met "vervangt": N; de
oude reeks stopt dan vanaf de eerste keer van de nieuwe. Stoppen, één keer overslaan of helemaal verwijderen:
<reeks>{"id": N, "actie": "stop|overslaan|verwijder", "datum": "JJJJ-MM-DD"}</reeks>
Bij stop is datum de eerste dag zonder de afspraak; bij overslaan de dag die vervalt; verwijder heeft geen datum
nodig. Ook dit ziet Martijn als knop.

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
Zeg niet dat je geen toegang hebt tot routes of het thuisadres: dat regelt het systeem.
Mist er bij een afspraak een adres, of wil Martijn reistijd voor elke keer van een reeks, dan doet hij dat met de knop
"Reistijd" bij die afspraak in de week; daar vult hij het adres in en kiest hij "elke keer". Voor afspraken in de
agenda van Sanne is er de knop "Brengen en ophalen": die zet de ritten (eenmalig of voor de hele reeks) in zijn eigen
agenda. Wijs hem daarop als hij vraagt om wegbrengen en ophalen op basis van haar agenda. Doe dit alleen als hij het zelf zegt, niet uit jezelf. Je accepteert, verplaatst of verwijdert zelf
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
    if a.get("reisadres") and a.get("reisadres") != a.get("locatie"):
        extra.append(f"reisadres {a['reisadres']}")
    if a.get("reis_auto"):
        extra.append("reistijd wordt elke keer automatisch ingepland")
    return f"- {tijd} {a['titel']} [{sync.DOMEINEN.get(a.get('domein'), a.get('domein'))}, {a.get('agenda_naam') or a.get('koppeling')}]" + (
        f" ({'; '.join(extra)})" if extra else "")


def _reistijd_anderen(c, lijst, inst):
    """Reistijd vanaf huis naar plekken in agenda's die niet bezet zijn voor Martijn (voor wegbrengen en ophalen)."""
    if not reis.actief() or not inst.get("vertrekpunt"):
        return ""
    plekken = []
    for a in lijst:
        if a.get("niet_voor_mij") and a.get("locatie") and not a.get("online") and a["locatie"] not in plekken:
            plekken.append(a["locatie"])
    regels = []
    try:
        thuis = reis.adres(c, inst["vertrekpunt"], alleen_cache=True)
        for plek in plekken[:6] if thuis else []:
            doel = reis.adres(c, plek, focus=thuis["punt"])
            if not doel:
                continue
            tijden = {s: reis.route(c, thuis["punt"], doel["punt"], s) for s in ("auto", "fiets")}
            delen = [f"{m} min {'met de auto' if s == 'auto' else 'op de fiets'}" for s, m in tijden.items() if m]
            if delen:
                regels.append(f"- {plek}: " + ", ".join(delen) + " (enkele reis)")
    except bronnen.BronFout:
        pass
    return ("# Reistijd vanaf huis naar plekken in andere agenda's (berekend)\n" + "\n".join(regels)) if regels else ""


def context(c, nu=None):
    tz = config.TZ
    nu = nu or dt.datetime.now(tz)
    inst = sync.instellingen(c)
    tot = nu + dt.timedelta(days=14)
    lijst = sync.afspraken(c, dt.datetime.combine(nu.date(), dt.time(0), tzinfo=tz), tot)
    kop = [f"Het is {datum_lang(nu.date())} {nu.year}, {nu:%H.%M} uur."]
    koppelingen = sync.koppelingen(c, alleen_actief=True)
    if not koppelingen:
        return "\n".join(kop + ["Er zijn nog geen agenda's gekoppeld. Zeg dat, en verwijs naar Clara > Agenda-instellingen."])
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
        delen.append("# Potentiële overlap (twee bezette afspraken tegelijk)\n" + "\n".join(
            f"- {x['van'].astimezone(tz):%d-%m %H.%M}: {x['a']['titel']} en {x['b']['titel']}" for x in conf))
    over = analyse.overgangen(lijst, inst["reistijd"])
    if over:
        delen.append("# Krappe overgangen\n" + "\n".join(
            f"- {x['van']['eind'].astimezone(tz):%d-%m %H.%M}: {x['minuten']} min van {x['van']['locatie']} naar {x['naar']['locatie']}"
            for x in over))
    reeksen = c.execute("""SELECT r.*, k.naam AS koppeling FROM agenda_reeksen r JOIN agenda_koppelingen k ON k.id = r.koppeling_id
                           WHERE r.status = 'actief' ORDER BY r.titel""").fetchall()
    if reeksen:
        regels_r = []
        for r in reeksen:
            keren = c.execute("""SELECT start FROM agenda_items WHERE koppeling_id = %s AND reeks = %s AND start > %s
                                 ORDER BY start LIMIT 6""", (r["koppeling_id"], r["extern_id"], nu)).fetchall()
            regels_r.append(f"- [reeks {r['id']}] {r['titel']}, {bronnen.herhaal_tekst(r['herhaal'])}, "
                            f"{r['start'].astimezone(tz):%H.%M}-{r['eind'].astimezone(tz):%H.%M}, in agenda {r['voor'] or r['koppeling']}"
                            + ("; komende keren: " + ", ".join(f"{x['start'].astimezone(tz):%d-%m}" for x in keren) if keren else ""))
        delen.append("# Herhalende afspraken die Clara maakte\n" + "\n".join(regels_r))
    delen.append(_reistijd_anderen(c, lijst, inst))
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
            try:
                vervangt = int(v["vervangt"]) if v.get("vervangt") not in (None, "") else None
            except (TypeError, ValueError):
                vervangt = None
            uit.append({"titel": str(v.get("titel") or "Blok")[:200], "start": start, "eind": eind,
                        "domein": v.get("domein") if v.get("domein") in sync.DOMEINEN else "",
                        "agenda": str(v.get("agenda") or "").strip()[:80],
                        "herhaal": bronnen.herhaal_normaal(v.get("herhaal")), "vervangt": vervangt})
    return VOORSTEL.sub("", tekst).strip(), uit


def reeksacties_uit(c, tekst, tz=config.TZ):
    """Stop, overslaan of verwijderen voor een reeks van Clara: alleen als voorstel (Martijn klikt). Geeft (tekst, lijst)."""
    uit = []
    for m in REEKS_TAG.finditer(tekst):
        try:
            v = json.loads(m.group(1))
            r = c.execute("SELECT * FROM agenda_reeksen WHERE id = %s AND status = 'actief'", (int(v["id"]),)).fetchone()
            actie = str(v.get("actie", ""))
            datum = dt.date.fromisoformat(str(v["datum"])[:10]) if v.get("datum") else None
        except (ValueError, KeyError, TypeError):
            continue
        if not r or actie not in ("stop", "overslaan", "verwijder") or (actie != "verwijder" and not datum):
            continue
        moment = dt.datetime.combine(datum, dt.time(0), tzinfo=tz) if datum else r["start"]
        uit.append({"soort": actie, "reeks_id": r["id"], "datum": datum, "titel": r["titel"], "start": moment, "eind": moment})
    return REEKS_TAG.sub("", tekst).strip(), uit


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
    tekst, reeksacties = reeksacties_uit(c, tekst)
    tekst, vervoer = vervoer_uit(c, tekst)
    if vervoer:
        tekst = (tekst + "\n\n" + "\n".join(f"(Vastgelegd: {v})" for v in vervoer)).strip()
    if not tekst:
        tekst = "Hier is mijn voorstel." if voorstellen or reeksacties else "(Geen antwoord ontvangen. Probeer het nog eens.)"
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    for v in voorstellen:
        vervangt = v.get("vervangt")
        if vervangt and not c.execute("SELECT 1 FROM agenda_reeksen WHERE id = %s AND status = 'actief'", (vervangt,)).fetchone():
            vervangt = None
        c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, domein, agenda, herhaal, reeks_id)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                  (gesprek_id, v["titel"], v["start"], v["eind"], v["domein"], v.get("agenda", ""),
                   json.dumps(v["herhaal"]) if v.get("herhaal") else None, vervangt))
    for v in reeksacties:
        c.execute("""INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, soort, reeks_id, datum)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                  (gesprek_id, v["titel"], v["start"], v["eind"], v["soort"], v["reeks_id"], v["datum"]))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst
