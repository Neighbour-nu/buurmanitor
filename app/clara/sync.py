"""Clara haalt de agenda's op: elke vijf minuten Google en Outlook, elk kwartier agendalinks, elk uur de raadsagenda.

Het venster loopt van een week terug tot acht weken vooruit. Wat in dat venster niet meer bestaat, verdwijnt ook hier.
Na het ophalen kijkt Clara naar dubbele boekingen en nieuwe uitnodigingen en maakt daar meldingen van.
"""
import datetime as dt
import json
import threading
import time

from ..kern import config, meldingen, planner
from . import analyse, bronnen, kluis, reis, spiegel

INTERVAL = {"google": dt.timedelta(minutes=5), "microsoft": dt.timedelta(minutes=5), "ics": dt.timedelta(minutes=15),
            "raad": dt.timedelta(hours=1)}
DOMEINEN = dict(config.DOMEINEN)
_toegang = {}  # koppeling-id -> (token, verloopt) ; alleen in het geheugen van dit proces
_slot = threading.Lock()


def venster(nu=None):
    nu = nu or dt.datetime.now(config.TZ)
    van = dt.datetime.combine(nu.date() - dt.timedelta(days=7), dt.time(0), tzinfo=config.TZ)
    return van, van + dt.timedelta(days=7 + 56)


def instellingen(c):
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = 'clara'").fetchone()
    try:
        return {**analyse.STANDAARD, **reis.STANDAARD, **spiegel.STANDAARD, **(json.loads(r["waarde"]) if r and r["waarde"] else {})}
    except ValueError:
        return {**analyse.STANDAARD, **reis.STANDAARD, **spiegel.STANDAARD}


def instellingen_opslaan(c, inst):
    c.execute("""INSERT INTO instellingen (sleutel, waarde) VALUES ('clara', %s)
                 ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()""", (json.dumps(inst),))


def koppelingen(c, alleen_actief=False):
    return c.execute("SELECT * FROM agenda_koppelingen" + (" WHERE actief" if alleen_actief else "") + " ORDER BY id").fetchall()


def toegang(c, k):
    """Een geldig toegangstoken voor Google of Microsoft (vernieuwd met het refresh-token uit de kluis)."""
    with _slot:
        token, verloopt = _toegang.get(k["id"], (None, 0))
        if token and verloopt > time.monotonic():
            return token
        refresh = kluis.open_(k["geheim"])
        if k["soort"] == "google":
            token, duur, nieuw = bronnen.google_toegang(refresh)
        else:
            token, duur, nieuw = bronnen.ms_toegang(refresh, k["tenant"], not k["schrijven"])
        if nieuw and nieuw != refresh:  # Microsoft wisselt het refresh-token regelmatig
            c.execute("UPDATE agenda_koppelingen SET geheim = %s WHERE id = %s", (kluis.sluit(nieuw), k["id"]))
        _toegang[k["id"]] = (token, time.monotonic() + max(60, int(duur) - 120))
        return token


def vergeet_toegang(kid):
    with _slot:
        _toegang.pop(kid, None)


def agendas_bijwerken(c, k):
    """De lijst agenda's van een account ophalen; eerdere keuzes (aan/uit, domein) blijven staan."""
    tok = toegang(c, k)
    nieuw = bronnen.google_agendas(tok) if k["soort"] == "google" else bronnen.ms_agendas(tok)
    oud = {a["id"]: a for a in (k["agendas"] or [])}
    lijst = []
    for a in nieuw:
        eerder = oud.get(a["id"], {})
        lijst.append({**a, "aan": eerder.get("aan", a["primair"]), "domein": eerder.get("domein", k["domein"]),
                      "bezet": eerder.get("bezet", True)})
    schrijf = k["schrijfagenda"] or next((a["id"] for a in lijst if a["primair"] and a["schrijven"]), "")
    c.execute("UPDATE agenda_koppelingen SET agendas = %s, schrijfagenda = %s WHERE id = %s",
              (json.dumps(lijst), schrijf, k["id"]))
    return lijst


def ophalen(c, k, van, tot):
    """Alle afspraken van één koppeling in het venster, met het domein erbij."""
    if k["soort"] in ("google", "microsoft"):
        tok = toegang(c, k)
        agendas = k["agendas"]
        if not agendas:
            agendas = agendas_bijwerken(c, k)
        elif any("kleur" not in a for a in agendas):  # van voor de kleuren: één keer verversen, lukt dat niet dan de oude lijst
            try:
                agendas = agendas_bijwerken(c, k)
            except bronnen.BronFout:
                pass
        uit = []
        for a in agendas:
            if not a.get("aan"):
                continue
            lijst = (bronnen.google_items if k["soort"] == "google" else bronnen.ms_items)(tok, a["id"], van, tot)
            uit += [dict(i, domein=a.get("domein") or k["domein"]) for i in lijst]
        return uit
    if k["soort"] == "ics":
        return [dict(i, domein=k["domein"]) for i in bronnen.ics_items(bronnen.ics_tekst(kluis.open_(k["geheim"])), van, tot)]
    return [dict(i, domein=k["domein"]) for i in bronnen.raad_items(van, tot)]


def opslaan(c, kid, items, van, tot):
    for i in items:
        c.execute("""INSERT INTO agenda_items (koppeling_id, agenda_id, extern_id, domein, ical_uid, titel, locatie, start,
                        eind, heledag, status, vrij, antwoord, organisator, online, link, geschat, bijgewerkt, omschrijving, kleur, reeks)
                     VALUES (%(k)s, %(agenda_id)s, %(extern_id)s, %(domein)s, %(ical_uid)s, %(titel)s, %(locatie)s, %(start)s,
                        %(eind)s, %(heledag)s, %(status)s, %(vrij)s, %(antwoord)s, %(organisator)s, %(online)s, %(link)s,
                        %(geschat)s, now(), %(omschrijving)s, %(kleur)s, %(reeks)s)
                     ON CONFLICT (koppeling_id, agenda_id, extern_id) DO UPDATE SET domein = EXCLUDED.domein,
                        ical_uid = EXCLUDED.ical_uid, titel = EXCLUDED.titel, locatie = EXCLUDED.locatie,
                        start = EXCLUDED.start, eind = EXCLUDED.eind, heledag = EXCLUDED.heledag, status = EXCLUDED.status,
                        vrij = EXCLUDED.vrij, antwoord = EXCLUDED.antwoord, organisator = EXCLUDED.organisator,
                        online = EXCLUDED.online, link = EXCLUDED.link, geschat = EXCLUDED.geschat, bijgewerkt = now(),
                        omschrijving = EXCLUDED.omschrijving, kleur = EXCLUDED.kleur, reeks = EXCLUDED.reeks""",
                  {"omschrijving": "", "kleur": "", "reeks": "", **i, "k": kid})
    sleutels = [f"{i['agenda_id']}\x1f{i['extern_id']}" for i in items]
    weg = c.execute("""DELETE FROM agenda_items WHERE koppeling_id = %s AND start < %s AND eind > %s
                       AND NOT (agenda_id || chr(31) || extern_id = ANY(%s)) RETURNING id""",
                    (kid, tot, van, sleutels)).fetchall()
    return len(weg)


def synchroniseer(c, k, forceer=False, nu=None):
    """Eén koppeling bijwerken als hij aan de beurt is. Geeft een korte uitslag terug, of None."""
    nu = nu or dt.datetime.now(config.TZ)
    if not forceer and k["laatst"] and nu - k["laatst"] < INTERVAL[k["soort"]] - dt.timedelta(seconds=20):
        return None
    van, tot = venster(nu)
    try:
        items = ophalen(c, k, van, tot)
        weg = opslaan(c, k["id"], items, van, tot)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        if isinstance(e, bronnen.OpnieuwKoppelen):
            vergeet_toegang(k["id"])
        c.execute("UPDATE agenda_koppelingen SET laatst = %s, fout = %s WHERE id = %s", (nu, str(e)[:500], k["id"]))
        meldingen.melden(c, "belangrijk", "clara", f"Agenda {k['naam']} wordt niet bijgewerkt", str(e), "/clara/koppelingen",
                         sleutel=f"clara-koppeling-{k['id']}-{nu:%Y-%m-%d}")
        return f"{k['naam']}: {e}"
    c.execute("UPDATE agenda_koppelingen SET laatst = %s, fout = NULL WHERE id = %s", (nu, k["id"]))
    return f"{k['naam']}: {len(items)} afspraken" + (f", {weg} weg" if weg else "")


# Afspraken die Martijn niet wil zien. De eerste regel vroeg hij zelf: Whaller-uitnodigingen uit de Bestuurs-sphere.
STANDAARD_VERBERGEN = [{"organisator": "Whaller", "tekst": "Bestuur"}]


def verbergregels(c):
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = 'clara_verbergen'").fetchone()
    if not r:
        return list(STANDAARD_VERBERGEN)
    try:
        return [x for x in json.loads(r["waarde"]) if isinstance(x, dict)]
    except ValueError:
        return []


def verbergregels_opslaan(c, regels):
    instellingen_lijst(c, "clara_verbergen", regels)


def afspraken(c, van, tot, met_verborgen=False):
    """De afspraken in een periode, samengevoegd over alle agenda's, met de naam en het soort van de koppeling.
    Afspraken onder een verbergregel blijven weg: niet in de week, niet bij dubbele boekingen of uitnodigingen,
    en ze houden ook geen tijd bezet bij het zoeken naar vrije tijd."""
    rijen = c.execute("""SELECT i.*, k.naam AS koppeling, k.soort, k.schrijven, k.account
                         FROM agenda_items i JOIN agenda_koppelingen k ON k.id = i.koppeling_id
                         WHERE k.actief AND i.start < %s AND i.eind > %s ORDER BY i.start""", (tot, van)).fetchall()
    regels = verbergregels(c)
    agendas = {(k["id"], a["id"]): (a, len(k["agendas"] or [])) for k in koppelingen(c, alleen_actief=True) for a in (k["agendas"] or [])}
    gekozen = reis.keuzes(c)
    lijst = []
    for r in rijen:
        a = dict(r)
        if spiegel.is_spiegel(a):  # blokken 'Bezet' van Clara zelf: geen echte afspraak
            continue
        ag, aantal = agendas.get((a["koppeling_id"], a["agenda_id"]), (None, 0))
        # Naam van de agenda (zoals 'Sanne' of 'ZEROHERO') als een account er meer heeft.
        a["agenda_naam"] = ag["naam"] if ag and aantal > 1 else ""
        # Kleur zoals in Google Agenda: eigen kleur van de afspraak, anders die van de agenda.
        a["kleur_toon"] = a.get("kleur") or (ag or {}).get("kleur") or ""
        # Agenda's die niet 'bezet voor mij' zijn (bijvoorbeeld die van Sanne): wel zien, niet als bezette tijd tellen.
        a["niet_voor_mij"] = bool(ag) and ag.get("bezet") is False
        # ...tenzij Martijn vervoer koos: dan gaat hij mee (bijvoorbeeld Sanne naar de manege brengen) en telt het wel.
        a["mee"] = a["niet_voor_mij"] and reis.gaat_mee(a, gekozen)
        if a["mee"]:
            a["niet_voor_mij"] = False
        if a["niet_voor_mij"]:
            a["vrij"] = True
        lijst.append(a)
    if not met_verborgen:
        lijst = [a for a in lijst if not analyse.verborgen(a, regels)]
    return analyse.samenvoegen(lijst)


def verborgen_aantal(c, van, tot):
    regels = verbergregels(c)
    return sum(1 for a in afspraken(c, van, tot, met_verborgen=True) if analyse.verborgen(a, regels))


def melden(c, nu=None):
    """Meldingen voor dubbele boekingen (komende week) en nieuwe uitnodigingen."""
    nu = nu or dt.datetime.now(config.TZ)
    lijst = afspraken(c, nu, nu + dt.timedelta(days=7))
    for x in analyse.conflicten(lijst):
        a, b = x["a"], x["b"]
        ids = sorted([f"{a['koppeling_id']}:{a['extern_id']}", f"{b['koppeling_id']}:{b['extern_id']}"])
        meldingen.melden(c, "belangrijk", "clara", f"Dubbele boeking op {x['van'].astimezone(config.TZ):%d-%m om %H.%M}",
                         f"{a['titel']} ({a['koppeling']}) en {b['titel']} ({b['koppeling']})", "/clara",
                         sleutel="clara-conflict-" + "-".join(ids)[:200] + f"-{x['van']:%Y%m%d%H%M}")
    gemeld = instellingen_lijst(c, "clara_uitnodigingen")
    nieuw = [u for u in analyse.uitnodigingen(afspraken(c, nu, venster(nu)[1]), nu)
             if f"{u['koppeling_id']}:{u['extern_id']}" not in gemeld]
    if nieuw:
        titel = (f"Uitnodiging: {nieuw[0]['titel']}" if len(nieuw) == 1 else f"{len(nieuw)} nieuwe uitnodigingen")
        tekst = "; ".join(f"{u['titel']} ({u['start'].astimezone(config.TZ):%d-%m %H.%M})" for u in nieuw[:5])
        meldingen.melden(c, "normaal", "clara", titel, tekst, "/clara#uitnodigingen",
                         sleutel=f"clara-uitnodigingen-{nu:%Y%m%d%H%M}")
        instellingen_lijst(c, "clara_uitnodigingen", (gemeld + [f"{u['koppeling_id']}:{u['extern_id']}" for u in nieuw])[-500:])
    meldingen_opruimen(c, nu)


def meldingen_opruimen(c, nu=None):
    """Meldingen over uitnodigingen die nu onder een verbergregel vallen, gelden als gelezen."""
    nu = nu or dt.datetime.now(config.TZ)
    regels = verbergregels(c)
    titels = {a["titel"] for a in afspraken(c, nu - dt.timedelta(days=1), venster(nu)[1], met_verborgen=True)
              if analyse.verborgen(a, regels)}
    if not titels:
        return
    for m in c.execute("""SELECT id, tekst FROM meldingen WHERE bron = 'clara' AND NOT gelezen
                          AND (titel LIKE 'Uitnodiging%%' OR titel LIKE '%%nieuwe uitnodigingen')""").fetchall():
        delen = [d for d in (m["tekst"] or "").split("; ") if d]
        if delen and all(any(d.startswith(t + " (") for t in titels) for d in delen):
            c.execute("UPDATE meldingen SET gelezen = true WHERE id = %s", (m["id"],))


def instellingen_lijst(c, sleutel, nieuw=None):
    if nieuw is not None:
        c.execute("""INSERT INTO instellingen (sleutel, waarde) VALUES (%s, %s)
                     ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()""",
                  (sleutel, json.dumps(nieuw)))
        return nieuw
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel,)).fetchone()
    try:
        return json.loads(r["waarde"]) if r else []
    except ValueError:
        return []


@planner.taak("agenda", elke=dt.timedelta(minutes=5))
def taak_agenda(c, forceer=False):
    uitslag = [u for k in koppelingen(c, alleen_actief=True) if (u := synchroniseer(c, k, forceer=forceer))]
    if koppelingen(c, alleen_actief=True):
        melden(c)
        r = reistijd_bijwerken(c)
        if r:
            uitslag.append(r)
        r = spiegel.bijwerken(c)
        if r:
            uitslag.append(r)
    return "; ".join(uitslag) or "niets aan de beurt"


def reisplan(c, van, tot, alleen_cache=True):  # noqa: D401
    """Het reisplan voor een periode (met de afspraak ervoor, voor de reis vanaf de vorige plek)."""
    lijst = afspraken(c, van - dt.timedelta(hours=12), tot)
    return reis.plan(c, lijst, instellingen(c), alleen_cache=alleen_cache)


def reistijd_bijwerken(c, nu=None):
    """Adressen en routes voor de komende twee weken opzoeken (de rest komt uit het geheugen), en melden waar de
    reistijd niet past. Geeft een korte uitslag, of None als reistijd niet is ingesteld."""
    nu = nu or dt.datetime.now(config.TZ)
    inst = instellingen(c)
    if not reis.actief() or not inst.get("vertrekpunt"):
        return None
    try:
        p = reisplan(c, nu, nu + dt.timedelta(days=14), alleen_cache=False)
    except bronnen.BronFout as e:
        return f"reistijd: {e}"
    for r in p["regels"]:
        if r.get("minuten") and not r["past"] and not r["ingepland"] and r["start"] < nu + dt.timedelta(days=7):
            a = r["afspraak"]
            meldingen.melden(c, "belangrijk", "clara", f"Reistijd past niet: {a['titel']}",
                             f"{r['minuten']} min {r['vervoer']} vanaf {r['van']}, maar {r['botst']} loopt dan nog. "
                             f"{a['start'].astimezone(config.TZ):%d-%m om %H.%M}.", "/clara",
                             sleutel=f"clara-reis-{r['sleutel']}-{a['start']:%Y%m%d%H%M}"[:250])
    return f"reistijd: {len(p['regels'])} reizen" + (f", {len(p['onbekend'])} plekken onbekend" if p.get("onbekend") else "")
