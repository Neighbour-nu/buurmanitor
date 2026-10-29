"""Praten met Clara. Ze krijgt de komende twee weken mee (alle agenda's samen), met dubbele boekingen, krappe
overgangen, open uitnodigingen en vrije blokken. Stelt ze een tijdstip voor, dan zet ze dat als voorstel klaar;
in de agenda komt het pas als Martijn op de knop drukt."""
import datetime as dt
import json
import re

from .. import personas
from ..kern import claude, config
from ..kern.web import DAGEN, datum_lang
from . import analyse, sync

VOORSTEL = re.compile(r"<voorstel>\s*(\{.*?\})\s*</voorstel>", re.S)
UITLEG = """# Voorstellen doen
Stel je een concreet tijdstip voor om iets in te plannen (een schrijfblok, voorbereiding, reistijd), zet dan per
voorstel aan het eind van je antwoord precies één regel:
<voorstel>{"titel": "...", "start": "JJJJ-MM-DDTUU:MM", "eind": "JJJJ-MM-DDTUU:MM", "domein": "politiek|prive|zerohero|fbim|upperspire"}</voorstel>
Tijden in Nederlandse tijd. Martijn ziet het als knop en beslist zelf. Je accepteert, verplaatst of verwijdert zelf
nooit iets en zegt ook niet dat je dat gedaan hebt. Gebruik alleen tijden die in de vrije blokken passen, tenzij
Martijn iets anders vraagt."""


def _regel(a, tz):
    s, e = a["start"].astimezone(tz), a["eind"].astimezone(tz)
    tijd = "hele dag" if a.get("heledag") else f"{s:%H.%M}-{e:%H.%M}" + (" (einde geschat)" if a.get("geschat") else "")
    extra = [x for x in (a.get("locatie"), "online" if a.get("online") else "",
                         {"geen": "uitnodiging, nog niet beantwoord", "voorlopig": "voorlopig", "afgewezen": "afgewezen"}.get(a.get("antwoord"), ""),
                         "vrij" if a.get("vrij") else "") if x]
    return f"- {tijd} {a['titel']} [{sync.DOMEINEN.get(a.get('domein'), a.get('domein'))}, {a.get('koppeling')}]" + (
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
    vrij = analyse.vrije_blokken(lijst, nu, 14, 60, inst, tz, max_aantal=20)
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
                        "domein": v.get("domein") if v.get("domein") in sync.DOMEINEN else ""})
    return VOORSTEL.sub("", tekst).strip(), uit


def antwoord(c, gesprek_id):
    berichten = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id",
                          (gesprek_id,)).fetchall()
    systeem = personas.systeemtekst(personas.get("clara")) + "\n\n" + UITLEG + "\n\n# Wat je nu weet\n" + context(c)
    tekst = claude.gesprek(c, "clara", systeem, [{"role": b["rol"], "content": b["tekst"]} for b in berichten if (b["tekst"] or "").strip()][-30:],
                           max_tokens=1500)
    tekst = tekst.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    tekst, voorstellen = voorstellen_uit(tekst)
    if not tekst:
        tekst = "Hier is mijn voorstel." if voorstellen else "(Geen antwoord ontvangen. Probeer het nog eens.)"
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    for v in voorstellen:
        c.execute("INSERT INTO agenda_voorstellen (gesprek_id, titel, start, eind, domein) VALUES (%s, %s, %s, %s, %s)",
                  (gesprek_id, v["titel"], v["start"], v["eind"], v["domein"]))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst
