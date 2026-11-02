"""Aankondigingen: een melding kort voordat een vergadering, afspraak of taak begint, en 's ochtends voor deadlines.

Elke minuut kijkt de planner wat er binnenkort begint. Een aankondiging is een melding van niveau 'belangrijk', dus
hij verschijnt als banner en (als je dat toestaat) als melding op je bureaublad. Elke afspraak wordt één keer
aangekondigd. Een paar uur later ruimt Buurmanitor de aankondiging zelf op (naar het archief), zodat je meldingen
niet vollopen met wat al voorbij is.

Wat wordt aangekondigd (in te stellen op /meldingen/aankondigingen):
- vergaderingen en afspraken met een tijd in je eigen agenda's (niet die je hebt afgewezen of die vrij staan, en niet
  die van Sanne of Brigit, tenzij je meegaat); reistijd van Clara als 'tijd om te vertrekken';
- taken met een begintijd (ook als ze niet in de agenda staan);
- 's ochtends: taken waarvan de deadline vandaag is.
"""
import datetime as dt
import hashlib
import json

from .kern import config, meldingen, planner

STANDAARD = {"vergaderingen": True, "taken": True, "deadlines": True, "minuten": 15, "ochtend": "08:00"}
KEUZE_MINUTEN = (5, 10, 15, 30, 60)
REIS_ICONEN = ("🚗", "🚆", "🚲", "🚶")  # reistijd en brengen of ophalen van Clara
SLEUTEL = "aankondigen"
OPRUIMEN_NA = dt.timedelta(hours=3)


def instellingen(c):
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = %s", (SLEUTEL,)).fetchone()
    try:
        eigen = json.loads(r["waarde"]) if r and r["waarde"] else {}
    except ValueError:
        eigen = {}
    return STANDAARD | {k: v for k, v in eigen.items() if k in STANDAARD}


def opslaan(c, waarden):
    c.execute("""INSERT INTO instellingen (sleutel, waarde) VALUES (%s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()""", (SLEUTEL, json.dumps(waarden)))


def _sleutel(*delen):
    return "aankondiging-" + hashlib.sha1("|".join(str(d) for d in delen).encode()).hexdigest()[:24]


def _tijd(t, tz):
    return t.astimezone(tz).strftime("%H.%M")


def te_melden_afspraken(lijst, nu, minuten, tz=config.TZ):
    """Welke afspraken nu aangekondigd moeten worden: met een tijd, binnen 'minuten' beginnend, en voor Martijn zelf.
    Geeft (sleutel, titel, tekst, link). Pure functie."""
    grens = nu + dt.timedelta(minutes=minuten)
    uit, gezien = [], set()
    for a in lijst:
        if a.get("heledag") or a.get("niet_voor_mij") or a.get("antwoord") == "afgewezen":
            continue
        if not (nu < a["start"] <= grens):
            continue
        titel = (a.get("titel") or "").strip() or "Afspraak"
        reis = titel.startswith(REIS_ICONEN)
        if a.get("vrij") and not reis:
            continue
        sleutel = _sleutel("afspraak", titel, a["start"].isoformat())
        if sleutel in gezien:  # dezelfde afspraak in twee agenda's: één keer melden
            continue
        gezien.add(sleutel)
        over = max(1, round((a["start"] - nu).total_seconds() / 60))
        if reis:
            wat = titel.lstrip("".join(REIS_ICONEN) + " ").strip()
            wat = wat[len("Reistijd "):] if wat.startswith("Reistijd ") else wat
            kop = f"Over {over} min vertrekken: {wat}"
            tekst = f"Om {_tijd(a['start'], tz)} vertrekken" + (f" ({a['locatie']})" if a.get("locatie") else "") + "."
        else:
            kop = f"Over {over} min: {titel}"
            tekst = f"{_tijd(a['start'], tz)}-{_tijd(a['eind'], tz)}" + (f" · {a['locatie']}" if a.get("locatie") else "") \
                + (" · online" if a.get("online") and not a.get("locatie") else "") + (f" · {a.get('agenda_naam') or a.get('koppeling')}" if a.get("koppeling") else "")
        uit.append((sleutel, kop[:200], tekst[:500], f"/clara#a{a['id']}" if a.get("id") else "/clara"))
    return uit


def te_melden_taken(taken, nu, minuten, ochtend, tz=config.TZ, met_taken=True, met_deadlines=True):
    """Welke taken nu aangekondigd moeten worden: met een begintijd binnen 'minuten', en vanaf 'ochtend' de deadlines
    van vandaag. Taken die al als afspraak in de agenda staan, kondigt de agenda aan. Pure functie."""
    nu_l = nu.astimezone(tz)
    uit = []
    for t in taken:
        if t["status"] == "afgerond":
            continue
        if met_taken and t.get("starttijd") and t.get("startdatum") and not t.get("agenda_extern"):
            start = dt.datetime.combine(t["startdatum"], t["starttijd"], tzinfo=tz)
            if nu < start <= nu + dt.timedelta(minutes=minuten):
                over = max(1, round((start - nu).total_seconds() / 60))
                uit.append((_sleutel("taak", t["id"], start.isoformat()), f"Over {over} min: {t['titel']}"[:200],
                            f"Taak om {start:%H.%M}" + (f"-{t['eindtijd']:%H.%M}" if t.get("eindtijd") else "") + ".", f"/taken?bewerk={t['id']}#formulier"))
        if met_deadlines and t.get("einddatum") == nu_l.date() and not t.get("eindtijd") and not t.get("starttijd") and nu_l.time() >= ochtend:
            uit.append((_sleutel("deadline", t["id"], t["einddatum"].isoformat()), f"Deadline vandaag: {t['titel']}"[:200],
                        "Deze taak moet vandaag af." + (" Prioriteit hoog." if t.get("prio") == "hoog" else ""), f"/taken?bewerk={t['id']}#formulier"))
    return uit


@planner.taak("aankondigen", elke=dt.timedelta(minutes=1))
def taak_aankondigen(c, nu=None):
    nu = nu or dt.datetime.now(config.TZ)
    inst = instellingen(c)
    minuten = int(inst["minuten"]) if int(inst["minuten"]) in KEUZE_MINUTEN else 15
    try:
        ochtend = dt.time.fromisoformat(str(inst["ochtend"])[:5])
    except ValueError:
        ochtend = dt.time(8, 0)
    te_melden = []
    if inst["vergaderingen"]:
        from .clara import sync  # pylint: disable=import-outside-toplevel
        if sync.koppelingen(c, alleen_actief=True):
            te_melden += te_melden_afspraken(sync.afspraken(c, nu, nu + dt.timedelta(minutes=minuten + 1)), nu, minuten)
    if inst["taken"] or inst["deadlines"]:
        taken = c.execute("""SELECT * FROM takenbord WHERE status <> 'afgerond'
                             AND (startdatum = %s OR einddatum = %s)""", (nu.date(), nu.date())).fetchall()
        te_melden += te_melden_taken(taken, nu, minuten, ochtend, met_taken=inst["taken"], met_deadlines=inst["deadlines"])
    gemeld = sum(1 for sleutel, titel, tekst, link in te_melden
                 if meldingen.melden(c, "belangrijk", "aankondiging", titel, tekst, link, sleutel=sleutel))
    # Opruimen: aankondigingen van een paar uur geleden naar het archief (de sleutel blijft, zodat hij niet terugkomt).
    c.execute("""UPDATE meldingen SET status = 'archief', gelezen = true, afgehandeld = now()
                 WHERE bron = 'aankondiging' AND status = 'open' AND tijd < %s""", (nu - OPRUIMEN_NA,))
    return f"{gemeld} aankondiging{'en' if gemeld != 1 else ''}" if gemeld else "niets aan te kondigen"
