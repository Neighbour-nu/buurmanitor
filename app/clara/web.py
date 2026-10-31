"""Schermen van Clara: de week, koppelingen, uitnodigingen, vrije tijd zoeken en het gesprek.

Alles wat iets in een agenda verandert (een blok inplannen, een uitnodiging beantwoorden) is een formulier met een
knop: Clara doet het nooit uit zichzelf (autonomie niveau 1).
"""
import asyncio
import datetime as dt
import json
import re
import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import personas
from ..kern import claude, config, db
from ..kern.web import DAGEN, controleer_herkomst, ctx, templates
from . import analyse, bronnen, gesprek, kluis, reis, spiegel, sync

router = APIRouter()
DOMEINEN = config.DOMEINEN


def _terug(pad, **melding):
    """Terug naar een pagina, met een melding erbij. Het pad mag zelf al een vraag (?week=1) of anker (#gesprek) hebben."""
    from urllib.parse import urlencode  # pylint: disable=import-outside-toplevel
    pad, _, anker = pad.partition("#")
    if melding:
        pad += ("&" if "?" in pad else "?") + urlencode(melding)
    return RedirectResponse(pad + (f"#{anker}" if anker else ""), status_code=303)


DAGEN_IN_BEELD = 14  # de agenda toont twee weken, vanaf vandaag


def _week_start(week):
    vandaag = dt.datetime.now(config.TZ).date()
    return vandaag + dt.timedelta(days=DAGEN_IN_BEELD * week) if week else vandaag


def _reisdoelen(koppelingen):
    """Agenda's voor reistijd en ritten: alleen Martijns eigen agenda's (niet die van bijvoorbeeld Sanne), de
    standaardagenda van elk account eerst."""
    uit = []
    for k in koppelingen:
        if not k["schrijven"] or k["soort"] not in ("google", "microsoft") or k["fout"]:
            continue
        for a in sorted(k["agendas"] or [], key=lambda x: x["id"] != k["schrijfagenda"]):
            if a.get("schrijven") and a.get("bezet") is not False and (a.get("aan") or a["id"] == k["schrijfagenda"]):
                uit.append((f"{k['id']}|{a['id']}", f"{a['naam']} ({k['naam']})", a.get("domein") or k["domein"]))
    return uit or _schrijfdoelen(koppelingen)


def _schrijfdoelen(koppelingen):
    """Agenda's waarin Clara (na een klik) iets mag zetten: (waarde, label, domein)."""
    uit = []
    for k in koppelingen:
        if not k["schrijven"] or k["soort"] not in ("google", "microsoft") or k["fout"]:
            continue
        for a in k["agendas"] or []:
            if a.get("schrijven") and (a.get("aan") or a["id"] == k["schrijfagenda"]):
                uit.append((f"{k['id']}|{a['id']}", f"{a['naam']} ({k['naam']})", a.get("domein") or k["domein"]))
    return uit


# ---------------------------------------------------------------- de week

@router.get("/clara", response_class=HTMLResponse)
def clara(request: Request, week: int = 0, duur: int = 0, dagen: int = 7, g: int | None = None, melding: str = "",
          fout: str = ""):
    tz = config.TZ
    nu = dt.datetime.now(tz)
    start = _week_start(week)
    van = dt.datetime.combine(start, dt.time(0), tzinfo=tz)
    with db.pool.connection() as c:
        koppelingen = sync.koppelingen(c)
        inst = sync.instellingen(c)
        lijst = sync.afspraken(c, van, van + dt.timedelta(days=DAGEN_IN_BEELD))
        komend = sync.afspraken(c, nu, nu + dt.timedelta(days=56))
        vrij = None
        if g is None:
            gs = c.execute("SELECT * FROM gesprekken WHERE persona = 'clara' AND NOT gearchiveerd ORDER BY bijgewerkt DESC LIMIT 1").fetchone()
        else:
            gs = c.execute("SELECT * FROM gesprekken WHERE id = %s AND persona = 'clara'", (g,)).fetchone() if g else None
        berichten = c.execute("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gs["id"],)).fetchall() if gs else []
        voorstellen = c.execute("SELECT * FROM agenda_voorstellen WHERE gesprek_id = %s AND status = 'open' ORDER BY start",
                                (gs["id"],)).fetchall() if gs else []
        eerdere = c.execute("""SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'clara' AND NOT gearchiveerd
                               AND id <> %s ORDER BY bijgewerkt DESC LIMIT 15""", (gs["id"] if gs else 0,)).fetchall()
        archief = c.execute("""SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'clara' AND gearchiveerd
                               ORDER BY bijgewerkt DESC LIMIT 30""").fetchall()
        verborgen = sync.verborgen_aantal(c, nu, nu + dt.timedelta(days=56))
        gedekt = reis.gedekte_reeksen(c)
        reeksen = [dict(r) for r in c.execute("""SELECT r.*, k.naam AS koppeling FROM agenda_reeksen r
                                                  JOIN agenda_koppelingen k ON k.id = r.koppeling_id
                                                  WHERE r.status = 'actief' ORDER BY r.titel""").fetchall()]
        for r in reeksen:
            r["keren"] = [x["start"] for x in c.execute("""SELECT start FROM agenda_items WHERE koppeling_id = %s AND reeks = %s
                                                          AND start > %s ORDER BY start LIMIT 10""",
                                                       (r["koppeling_id"], r["extern_id"], nu)).fetchall()]
        try:
            plan = sync.reisplan(c, van, van + dt.timedelta(days=DAGEN_IN_BEELD))
        except bronnen.BronFout as e:
            plan = {"fout": str(e), "regels": []}
        reis_komend = sync.reisplan(c, nu, nu + dt.timedelta(days=dagen + 1)) if duur and reis.actief() else {"regels": []}
    if duur:  # nog niet ingeplande reistijd telt bij het zoeken als bezet
        with db.pool.connection() as c:
            basis = sync.afspraken(c, nu, nu + dt.timedelta(days=max(1, min(dagen, 28)) + 1))
        vrij = analyse.vrije_blokken(basis + _reis_als_bezet(reis_komend), nu, max(1, min(dagen, 28)), max(15, min(duur, 600)), inst, tz)
    else:
        vrij = []
    per_afspraak = {}
    for r in plan["regels"]:
        per_afspraak.setdefault((r["afspraak"]["koppeling_id"], r["afspraak"]["extern_id"]), []).append(r)
    open_reis = [r for r in plan["regels"] if r.get("minuten") and not r["ingepland"] and r["start"] > nu]
    onbekend_ids = {(a["koppeling_id"], a["extern_id"]) for a in plan.get("onbekend", [])}
    rit_ids = {(int(m.group(1)), m.group(2)) for x in lijst if (m := RIT.search(x.get("omschrijving") or ""))}
    thuis_ids = {(a["koppeling_id"], a["extern_id"]) for a in plan.get("thuis_afspraken", [])}
    thuis_ids |= {(a["koppeling_id"], a["extern_id"]) for a in lijst if reis.is_thuis_tekst(a, inst)}
    conf = analyse.conflicten(komend)
    in_conflict = {id(x["a"]) for x in conf} | {id(x["b"]) for x in conf}
    conflict_ids = {(a["koppeling_id"], a["extern_id"]) for a in komend if id(a) in in_conflict}
    if reis.actief() and inst.get("vertrekpunt") and not plan.get("fout"):
        over = []  # met echte reistijd zie je per afspraak of het past
        krap_ids = {(r["afspraak"]["koppeling_id"], r["afspraak"]["extern_id"]) for r in plan["regels"] if not r["past"]}
    else:
        over = analyse.overgangen(komend, inst["reistijd"])
        krap_ids = {(x["naar"]["koppeling_id"], x["naar"]["extern_id"]) for x in over}
    return templates.TemplateResponse("clara.html", ctx(
        request, "clara", p=personas.get("clara"), nu=nu, week=week, start=start, dagen_lijst=analyse.per_dag(lijst, tz),
        alle_dagen=[start + dt.timedelta(days=d) for d in range(DAGEN_IN_BEELD)], conflicten=conf, overgangen=over,
        conflict_ids=conflict_ids, krap_ids=krap_ids, uitnodigingen=analyse.uitnodigingen(komend, nu),
        koppelingen=koppelingen, doelen=_schrijfdoelen(koppelingen), vrij=vrij, duur=duur, dagen=dagen, tz=tz,
        DAGEN=DAGEN, gesprek=gs, berichten=berichten, voorstellen=voorstellen, melding=melding, fout=fout,
        eerdere=eerdere, archief=archief, reeksen=reeksen, herhaal_tekst=bronnen.herhaal_tekst,
        kluis_ok=kluis.actief(), sleutel_ok=bool(config.ANTHROPIC_API_KEY), inst=inst,
        domeinnaam=dict(DOMEINEN), verborgen=verborgen, plan=plan, per_afspraak=per_afspraak, open_reis=open_reis,
        reis_actief=reis.actief(), is_reisblok=reis.is_reisblok, onbekend_ids=onbekend_ids, thuis_ids=thuis_ids, rit_ids=rit_ids,
        gedekt=gedekt, reis_dienst=reis.dienst_naam()))


def _reis_als_bezet(plan):
    return [{"start": r["start"], "eind": r["eind"], "heledag": False, "status": "bevestigd", "vrij": False, "antwoord": "eigen"}
            for r in plan.get("regels", []) if r.get("minuten") and not r["ingepland"]]


@router.post("/clara/vernieuwen")
def vernieuwen(request: Request):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        uitslag = sync.taak_agenda(c, forceer=True)
    return _terug("/clara", melding=uitslag[:300])


# ---------------------------------------------------------------- iets in de agenda zetten (na een klik)

def _maak(c, doel, titel, start, eind, omschrijving="", bijwerken=True, kleur=None, herhaal=None, locatie=""):
    try:
        kid, agenda_id = doel.split("|", 1)
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (int(kid),)).fetchone()
    except ValueError:
        k = None
    if not k or not k["schrijven"]:
        raise bronnen.BronFout("In deze agenda mag Clara niets zetten.")
    tok = sync.toegang(c, k)
    maak = bronnen.google_maak if k["soort"] == "google" else bronnen.ms_maak
    extern = maak(tok, agenda_id, titel, start, eind, omschrijving, kleur=kleur, **({"herhaal": herhaal} if herhaal else {}),
                  **({"locatie": locatie} if locatie else {}))
    c.execute("INSERT INTO logboek (persona, actie, details) VALUES ('clara', 'afspraak gemaakt', %s)",
              (json.dumps({"koppeling": k["naam"], "titel": titel, "start": start.isoformat(), "eind": eind.isoformat(),
                           "herhaal": herhaal}),))
    if bijwerken:
        sync.synchroniseer(c, k, forceer=True)
    return k["id"], extern


def _tijd(tekst):
    return dt.datetime.fromisoformat(tekst).replace(tzinfo=config.TZ) if "+" not in tekst[10:] else dt.datetime.fromisoformat(tekst)


@router.post("/clara/blok")
async def blok(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    try:
        start, eind = _tijd(str(f["start"])), _tijd(str(f["eind"]))
    except (KeyError, ValueError):
        return _terug("/clara", fout="Begin- of eindtijd klopt niet.")
    titel = str(f.get("titel") or "").strip()[:200] or "Blok"
    if eind <= start:
        return _terug("/clara", fout="De eindtijd moet na de begintijd liggen.")

    def doe():
        with db.pool.connection() as c:
            _maak(c, str(f.get("doel", "")), titel, start, eind, "Ingepland via Clara (Buurmanitor).")
    try:
        await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug("/clara", fout=str(e))
    return _terug("/clara", melding=f"'{titel}' staat in je agenda op {start:%d-%m om %H.%M}.")


@router.post("/clara/voorstel/{vid}")
async def voorstel(request: Request, vid: int):
    controleer_herkomst(request)
    f = await request.form()
    actie = str(f.get("actie", ""))
    with db.pool.connection() as c:
        v = c.execute("SELECT * FROM agenda_voorstellen WHERE id = %s AND status = 'open'", (vid,)).fetchone()
        if not v:
            return _terug("/clara")
        if actie == "weg":
            c.execute("UPDATE agenda_voorstellen SET status = 'afgewezen' WHERE id = %s", (vid,))
            return _terug(f"/clara?g={v['gesprek_id'] or 0}#gesprek")

    def doe():
        with db.pool.connection() as c:
            if v["soort"] == "nieuw":
                return _nieuw_uit_voorstel(c, v, str(f.get("doel", "")))
            r = c.execute("SELECT * FROM agenda_reeksen WHERE id = %s AND status = 'actief'", (v["reeks_id"],)).fetchone()
            if not r:
                raise bronnen.BronFout("Deze herhalende afspraak bestaat niet meer.")
            tekst = _reeks_actie(c, r, v["soort"], v["datum"])
            c.execute("UPDATE agenda_voorstellen SET status = 'ingepland' WHERE id = %s", (vid,))
            return tekst
    try:
        tekst = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/clara?g={v['gesprek_id'] or 0}", fout=str(e))
    return _terug(f"/clara?g={v['gesprek_id'] or 0}", melding=tekst)


def _nieuw_uit_voorstel(c, v, doel):
    """Een voorstel van Clara inplannen; met herhaling als reeks (die Clara daarna kan stoppen of overslaan)."""
    h = bronnen.herhaal_normaal(v["herhaal"]) if v.get("herhaal") else None
    start, eind = v["start"], v["eind"]
    if h:
        stap = bronnen.eerste_keer(start.astimezone(config.TZ), h)
        start, eind = start + stap, eind + stap
    kid, extern = _maak(c, doel, v["titel"], start, eind, "Voorstel van Clara (Buurmanitor).", bijwerken=False, herhaal=h)
    k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone()
    c.execute("UPDATE agenda_voorstellen SET status = 'ingepland', koppeling_id = %s, extern_id = %s WHERE id = %s", (kid, extern, v["id"]))
    tekst = f"'{v['titel']}' staat in je agenda"
    if h:
        voor = next((a["naam"] for a in (k["agendas"] or []) if f"{kid}|{a['id']}" == doel), k["naam"])
        c.execute("""INSERT INTO agenda_reeksen (koppeling_id, agenda_id, extern_id, titel, start, eind, herhaal, voor)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                  (kid, doel.split("|", 1)[1], extern, v["titel"], start, eind, json.dumps(h), voor))
        tekst += f", {bronnen.herhaal_tekst(h)}, vanaf {start.astimezone(config.TZ):%d-%m}"
        oud = c.execute("SELECT * FROM agenda_reeksen WHERE id = %s AND status = 'actief'", (v["reeks_id"],)).fetchone() if v.get("reeks_id") else None
        if oud:
            _reeks_actie(c, oud, "stop", start.astimezone(config.TZ).date(), bijwerken=oud["koppeling_id"] != kid)
            tekst += "; de oude reeks stopt vanaf dan"
    sync.synchroniseer(c, k, forceer=True)
    return tekst + "."


def _reeks_actie(c, r, actie, datum, bijwerken=True):
    """Een reeks van Clara stoppen (vanaf datum), één keer overslaan (op datum) of helemaal verwijderen."""
    tz = config.TZ
    k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (r["koppeling_id"],)).fetchone()
    if not k or not k["schrijven"]:
        raise bronnen.BronFout("In deze agenda mag Clara niets (meer) wijzigen.")
    tok = sync.toegang(c, k)
    google = k["soort"] == "google"
    weg = bronnen.google_verwijder if google else bronnen.ms_verwijder
    eerste = r["start"].astimezone(tz).date()
    if actie == "stop" and datum <= eerste:
        actie = "verwijder"
    if actie == "stop":
        h = dict(r["herhaal"])
        laatste = datum - dt.timedelta(days=1)
        if not h.get("tot") or dt.date.fromisoformat(h["tot"]) > laatste:
            h["tot"] = laatste.isoformat()
        (bronnen.google_reeks if google else bronnen.ms_reeks)(tok, r["agenda_id"], r["extern_id"], h, r["start"])
        c.execute("UPDATE agenda_reeksen SET herhaal = %s, status = CASE WHEN %s < now()::date THEN 'gestopt' ELSE status END WHERE id = %s",
                  (json.dumps(h), laatste, r["id"]))
        tekst = f"'{r['titel']}' stopt vanaf {DAGEN[datum.weekday()]} {datum:%d-%m}."
    elif actie == "verwijder":
        try:
            weg(tok, r["agenda_id"], r["extern_id"])
        except bronnen.NietGevonden:
            pass
        c.execute("UPDATE agenda_reeksen SET status = 'verwijderd' WHERE id = %s", (r["id"],))
        tekst = f"De hele reeks '{r['titel']}' is uit je agenda gehaald."
    elif actie == "overslaan":
        i = c.execute("""SELECT * FROM agenda_items WHERE koppeling_id = %s AND reeks = %s
                         AND (start AT TIME ZONE 'Europe/Amsterdam')::date = %s""", (k["id"], r["extern_id"], datum)).fetchone()
        if not i:
            raise bronnen.BronFout(f"Op {datum:%d-%m} staat geen '{r['titel']}' (Clara kijkt acht weken vooruit).")
        try:
            weg(tok, i["agenda_id"], i["extern_id"])
        except bronnen.NietGevonden:
            pass
        tekst = f"'{r['titel']}' vervalt op {DAGEN[datum.weekday()]} {datum:%d-%m}; de rest van de reeks blijft staan."
    else:
        raise bronnen.BronFout("Onbekende actie.")
    c.execute("INSERT INTO logboek (persona, actie, details) VALUES ('clara', %s, %s)",
              (f"reeks {actie}", json.dumps({"reeks": r["id"], "titel": r["titel"], "datum": datum.isoformat() if datum else None})))
    if bijwerken:
        sync.synchroniseer(c, k, forceer=True)
    return tekst


@router.post("/clara/reeks/{rid}")
async def reeks(request: Request, rid: int):
    """Een herhalende afspraak van Clara stoppen, een keer overslaan of verwijderen (na Martijns klik)."""
    controleer_herkomst(request)
    f = await request.form()
    actie = str(f.get("actie", ""))
    try:
        datum = dt.date.fromisoformat(str(f.get("datum", ""))[:10]) if f.get("datum") else None
    except ValueError:
        datum = None
    if actie not in ("stop", "overslaan", "verwijder") or (actie != "verwijder" and not datum):
        return _terug("/clara#reeksen", fout="Kies een datum.")

    def doe():
        with db.pool.connection() as c:
            r = c.execute("SELECT * FROM agenda_reeksen WHERE id = %s AND status = 'actief'", (rid,)).fetchone()
            if not r:
                raise bronnen.BronFout("Deze herhalende afspraak bestaat niet meer.")
            return _reeks_actie(c, r, actie, datum)
    try:
        tekst = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug("/clara#reeksen", fout=str(e))
    return _terug("/clara#reeksen", melding=tekst)


@router.post("/clara/uitnodiging/{item_id}")
async def uitnodiging(request: Request, item_id: int):
    controleer_herkomst(request)
    f = await request.form()
    antwoord = str(f.get("antwoord", ""))
    if antwoord not in ("geaccepteerd", "voorlopig", "afgewezen"):
        raise HTTPException(400)

    def doe():
        with db.pool.connection() as c:
            i = c.execute("""SELECT i.*, k.soort, k.schrijven, k.naam AS koppeling FROM agenda_items i
                             JOIN agenda_koppelingen k ON k.id = i.koppeling_id WHERE i.id = %s""", (item_id,)).fetchone()
            if not i or not i["schrijven"] or i["soort"] not in ("google", "microsoft"):
                raise bronnen.BronFout("Deze uitnodiging kan Clara niet beantwoorden (agenda alleen lezen).")
            k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (i["koppeling_id"],)).fetchone()
            tok = sync.toegang(c, k)
            (bronnen.google_antwoord if k["soort"] == "google" else bronnen.ms_antwoord)(tok, i["agenda_id"], i["extern_id"], antwoord)
            c.execute("UPDATE agenda_items SET antwoord = %s WHERE id = %s", (antwoord, item_id))
            c.execute("INSERT INTO logboek (persona, actie, details) VALUES ('clara', 'uitnodiging beantwoord', %s)",
                      (json.dumps({"titel": i["titel"], "antwoord": antwoord, "koppeling": i["koppeling"]}),))
            return i["titel"]
    try:
        titel = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug("/clara", fout=str(e))
    woord = {"geaccepteerd": "geaccepteerd", "voorlopig": "voorlopig geaccepteerd", "afgewezen": "afgewezen"}[antwoord]
    return _terug("/clara", melding=f"'{titel}' {woord}; de organisator krijgt bericht.")


# ---------------------------------------------------------------- gesprek met Clara

@router.post("/clara/vraag")
async def vraag(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    tekst = str(form.get("vraag", "")).strip()
    gid = int(form.get("gesprek") or 0) or None
    if not tekst:
        return _terug(f"/clara?g={gid or 0}")
    with db.pool.connection() as c:
        if not gid:
            gid = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('clara', %s) RETURNING id",
                            (" ".join(tekst.split())[:80],)).fetchone()["id"]
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (gid, tekst))
        c.execute("UPDATE gesprekken SET gearchiveerd = false WHERE id = %s", (gid,))

    def antwoorden():
        with db.pool.connection() as c:
            try:
                gesprek.antwoord(c, gid)
            except claude.ClaudeFout as e:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)",
                          (gid, f"(Ik kan nu niet antwoorden: {e})"))
    await asyncio.to_thread(antwoorden)
    return RedirectResponse(f"/clara?g={gid}#gesprek", status_code=303)


@router.post("/clara/nieuw")
def nieuw(request: Request):
    controleer_herkomst(request)
    return RedirectResponse("/clara?g=0#gesprek", status_code=303)


# ---------------------------------------------------------------- koppelingen

@router.get("/clara/koppelingen", response_class=HTMLResponse)
def koppelingen_scherm(request: Request, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        lijst = sync.koppelingen(c)
        inst = sync.instellingen(c)
        regels = sync.verbergregels(c)
        vertrekpunt_label = (reis.adres(c, inst["vertrekpunt"], alleen_cache=True) or {}).get("label", "") if inst.get("vertrekpunt") else ""
        spiegelplan = spiegel.plan(c)
        spiegelstatus = spiegel.status(c)
        reisblokken = c.execute("""SELECT count(*) AS n FROM agenda_items i JOIN agenda_koppelingen k ON k.id = i.koppeling_id
                                   WHERE k.soort = 'google' AND i.eind > now() AND i.omschrijving LIKE %s""",
                                (reis.MARKER + "%",)).fetchone()["n"]
        aantallen = {r["koppeling_id"]: r["n"] for r in c.execute(
            "SELECT koppeling_id, count(*) AS n FROM agenda_items WHERE start > now() GROUP BY koppeling_id").fetchall()}
    return templates.TemplateResponse("clara_koppelingen.html", ctx(
        request, "clara", koppelingen=lijst, aantallen=aantallen, inst=inst, melding=melding, fout=fout, regels=regels,
        reis_actief=reis.actief(), vertrekpunt_label=vertrekpunt_label, reis_dienst=reis.dienst_naam(),
        kleuren=bronnen.GOOGLE_KLEUREN, reisblokken=reisblokken, spiegelplan=spiegelplan, spiegelstatus=spiegelstatus,
        kluis_ok=kluis.actief(), google_ok=bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
        ms_ok=bool(config.MS_CLIENT_ID and config.MS_CLIENT_SECRET), politiek_ok=config.POLITIEK_ACTIEF,
        beheerder_url=bronnen.ms_beheerder_url() if config.MS_CLIENT_ID else "", DAGEN=DAGEN,
        terug_google=bronnen.terugadres("google"), terug_ms=bronnen.terugadres("microsoft"),
        terug_ms_beheerder=bronnen.terugadres("microsoft", "beheerder"), domeinnaam=dict(DOMEINEN)))


def _nieuwe_staat(c, soort, opties):
    staat = secrets.token_urlsafe(24)
    c.execute("DELETE FROM oauth_staat WHERE aangemaakt < now() - interval '1 hour'")
    c.execute("INSERT INTO oauth_staat (staat, soort, opties) VALUES (%s, %s, %s)", (staat, soort, json.dumps(opties)))
    return staat


def _neem_staat(c, staat, soort):
    r = c.execute("""DELETE FROM oauth_staat WHERE staat = %s AND soort = %s AND aangemaakt > now() - interval '20 minutes'
                     RETURNING opties""", (staat or "", soort)).fetchone()
    return r["opties"] if r else None


@router.post("/clara/koppel/{soort}")
async def koppel_start(request: Request, soort: str):
    controleer_herkomst(request)
    f = await request.form()
    domein = str(f.get("domein", "prive"))
    domein = domein if domein in dict(DOMEINEN) else "prive"
    naam = str(f.get("naam", "")).strip()[:80]
    if not kluis.actief():
        return _terug("/clara/koppelingen", fout="Er is nog geen kluissleutel. Draai op de server: bash ~/buurmanitor/app/deploy/clara.sh")
    with db.pool.connection() as c:
        if soort == "google":
            if not (config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET):
                return _terug("/clara/koppelingen", fout="De Google-app is nog niet ingesteld (clara.sh).")
            return RedirectResponse(bronnen.google_url(_nieuwe_staat(c, "google", {"domein": domein, "naam": naam})), status_code=303)
        if soort == "microsoft":
            if not (config.MS_CLIENT_ID and config.MS_CLIENT_SECRET):
                return _terug("/clara/koppelingen", fout="De Microsoft-app is nog niet ingesteld (clara.sh).")
            lezen = f.get("alleen_lezen") == "1"
            return RedirectResponse(bronnen.ms_url(_nieuwe_staat(c, "microsoft", {"domein": domein, "naam": naam, "lezen": lezen}),
                                                   lezen), status_code=303)
        if soort == "ics":
            url = str(f.get("url", "")).strip()
            if not url:
                return _terug("/clara/koppelingen", fout="Vul de agendalink in.")
            try:
                ruw = await asyncio.to_thread(bronnen.ics_tekst, url)
                van, tot = sync.venster()
                aantal = len(bronnen.ics_items(ruw, van, tot))
            except bronnen.BronFout as e:
                return _terug("/clara/koppelingen", fout=str(e))
            k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, domein, geheim) VALUES ('ics', %s, %s, %s)
                             RETURNING *""", (naam or "Agendalink", domein, kluis.sluit(url))).fetchone()
            sync.synchroniseer(c, k, forceer=True)
            return _terug("/clara/koppelingen", melding=f"Agendalink '{k['naam']}' gekoppeld ({aantal} afspraken in het venster).")
        if soort == "raad":
            if c.execute("SELECT 1 FROM agenda_koppelingen WHERE soort = 'raad'").fetchone():
                return _terug("/clara/koppelingen", melding="De raadsagenda was al gekoppeld.")
            k = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, domein) VALUES ('raad', 'Raadsagenda Helmond', 'politiek')
                             RETURNING *""").fetchone()
            uitslag = await asyncio.to_thread(_sync_los, k["id"])
            return _terug("/clara/koppelingen", melding=f"Raadsagenda gekoppeld. {uitslag}")
    raise HTTPException(404)


def _sync_los(kid):
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone()
        try:
            agendas = sync.agendas_bijwerken(c, k) if k["soort"] in ("google", "microsoft") else None
            if agendas is not None:
                k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone()
        except (bronnen.BronFout, kluis.KluisFout) as e:
            c.execute("UPDATE agenda_koppelingen SET fout = %s WHERE id = %s", (str(e), kid))
            return str(e)
        uitslag = sync.synchroniseer(c, k, forceer=True) or ""
        sync.melden(c)
        return uitslag


def _bewaar_koppeling(c, soort, gegevens, opties):
    """Nieuw account, of een bestaand account opnieuw gekoppeld (zelfde soort en adres): token vervangen."""
    bestaand = c.execute("SELECT * FROM agenda_koppelingen WHERE soort = %s AND lower(account) = lower(%s) AND account <> ''",
                         (soort, gegevens["account"])).fetchone()
    schrijven = not opties.get("lezen", False)
    if bestaand:
        c.execute("""UPDATE agenda_koppelingen SET geheim = %s, schrijven = %s, tenant = %s, fout = NULL, actief = true
                     WHERE id = %s""", (kluis.sluit(gegevens["refresh"]), schrijven, gegevens.get("tenant", ""), bestaand["id"]))
        sync.vergeet_toegang(bestaand["id"])
        return bestaand["id"], False
    naam = opties.get("naam") or gegevens["account"] or soort.capitalize()
    r = c.execute("""INSERT INTO agenda_koppelingen (soort, naam, account, tenant, domein, geheim, schrijven)
                     VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  (soort, naam, gegevens["account"], gegevens.get("tenant", ""), opties.get("domein", "prive"),
                   kluis.sluit(gegevens["refresh"]), schrijven)).fetchone()
    return r["id"], True


@router.get("/clara/koppel/{soort}/terug")
async def koppel_terug(soort: str, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if soort not in ("google", "microsoft"):
        raise HTTPException(404)
    if error:
        uitleg = error_description or error
        if "AADSTS65001" in uitleg or "AADSTS90094" in uitleg or "admin" in uitleg.lower():
            uitleg = ("Deze organisatie laat alleen apps toe die de beheerder heeft goedgekeurd. Stuur de beheerderslink "
                      "(onderaan deze pagina) naar de ICT van die organisatie, of gebruik een agendalink (ICS).")
        return _terug("/clara/koppelingen", fout=f"Koppelen is niet gelukt: {uitleg[:300]}")
    with db.pool.connection() as c:
        opties = _neem_staat(c, state, soort)
    if opties is None:
        return _terug("/clara/koppelingen", fout="Dit koppelverzoek is verlopen of onbekend. Probeer het opnieuw.")
    try:
        gegevens = await asyncio.to_thread(
            bronnen.google_code if soort == "google" else (lambda x: bronnen.ms_code(x, opties.get("lezen", False))), code)
    except bronnen.BronFout as e:
        return _terug("/clara/koppelingen", fout=str(e))
    with db.pool.connection() as c:
        kid, nieuw_ = _bewaar_koppeling(c, soort, gegevens, opties)
    uitslag = await asyncio.to_thread(_sync_los, kid)
    return _terug("/clara/koppelingen", melding=f"{gegevens['account'] or soort} {'gekoppeld' if nieuw_ else 'opnieuw gekoppeld'}. {uitslag}")


@router.get("/clara/koppel/microsoft/beheerder")
def beheerder_terug(admin_consent: str = "", tenant: str = "", error: str = "", error_description: str = ""):
    if admin_consent.lower() == "true":
        return _terug("/clara/koppelingen", melding="De beheerder heeft toestemming gegeven. Koppel het account nu gewoon.")
    return _terug("/clara/koppelingen", fout=f"Geen toestemming van de beheerder: {(error_description or error or 'onbekend')[:300]}")


@router.post("/clara/koppeling/{kid}")
async def koppeling_bijwerken(request: Request, kid: int):
    controleer_herkomst(request)
    f = await request.form()
    actie = str(f.get("actie", "opslaan"))
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone()
        if not k:
            raise HTTPException(404)
        if actie == "weg":
            c.execute("DELETE FROM agenda_koppelingen WHERE id = %s", (kid,))
            sync.vergeet_toegang(kid)
            return _terug("/clara/koppelingen", melding=f"'{k['naam']}' ontkoppeld. Afspraken in de bron zelf blijven staan.")
        if actie == "agendas":
            uitslag = await asyncio.to_thread(_sync_los, kid)
            return _terug("/clara/koppelingen", melding=f"Lijst met agenda's van '{k['naam']}' bijgewerkt. {uitslag}")
        domein = str(f.get("domein", k["domein"]))
        agendas = []
        for a in k["agendas"] or []:
            d = str(f.get(f"domein_{a['id']}", a.get("domein") or domein))
            agendas.append({**a, "aan": f.get(f"aan_{a['id']}") == "1", "domein": d if d in dict(DOMEINEN) else domein,
                            "bezet": f.get(f"bezet_{a['id']}") == "1"})
        c.execute("""UPDATE agenda_koppelingen SET naam = %s, domein = %s, agendas = %s, schrijfagenda = %s, actief = %s
                     WHERE id = %s""",
                  (str(f.get("naam") or k["naam"])[:80], domein if domein in dict(DOMEINEN) else k["domein"], json.dumps(agendas),
                   str(f.get("schrijfagenda", k["schrijfagenda"])), f.get("actief") == "1", kid))
    uitslag = await asyncio.to_thread(_sync_los, kid)
    return _terug("/clara/koppelingen", melding=f"Opgeslagen. {uitslag}")


def _doel_voor(doelen, domein):
    """De agenda voor een reisblok: een schrijfbare agenda van hetzelfde domein, anders de eerste."""
    return next((w for w, _l, d in doelen if d == domein), doelen[0][0] if doelen else "")


@router.post("/clara/reistijd")
async def reistijd_inplannen(request: Request):
    """Reistijdblokken in de agenda zetten (na een klik): één, of alle open reizen van de getoonde week."""
    controleer_herkomst(request)
    f = await request.form()
    gekozen = set(f.getlist("sleutel"))
    try:
        week = int(str(f.get("week", "0")))
    except ValueError:
        week = 0

    def doe():
        tz = config.TZ
        van = dt.datetime.combine(_week_start(week), dt.time(0), tzinfo=tz)
        with db.pool.connection() as c:
            doelen = _reisdoelen(sync.koppelingen(c))
            if not doelen:
                raise bronnen.BronFout("Om reistijd in te plannen is een Google- of Microsoft-agenda met schrijfrechten nodig.")
            vast = str(f.get("doel", ""))
            vast = vast if vast in {w for w, _l, _d in doelen} else ""
            plan = sync.reisplan(c, van, van + dt.timedelta(days=DAGEN_IN_BEELD), alleen_cache=False)
            nu = dt.datetime.now(tz)
            klaar, gebruikt = 0, set()
            for r in plan["regels"]:
                if r["sleutel"] not in gekozen or r["ingepland"] or not r.get("minuten") or r["start"] < nu:
                    continue
                doel = vast or _doel_voor(doelen, r["afspraak"].get("domein"))
                _maak(c, doel, reis.blok_titel(r), r["start"], r["eind"], reis.blok_omschrijving(r), bijwerken=False,
                      kleur=str(sync.instellingen(c).get("reis_kleur", "8")))
                gebruikt.add(int(doel.split("|")[0]))
                klaar += 1
            for kid in gebruikt:
                k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone()
                sync.synchroniseer(c, k, forceer=True)
            return klaar
    try:
        n = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/clara?week={week}", fout=str(e))
    return _terug(f"/clara?week={week}", melding=f"{n} reistijdblok{'ken' if n != 1 else ''} in je agenda gezet." if n else "Er was niets meer in te plannen.")


def _item_met_info(c, item_id):
    """Eén afspraak zoals de week hem ziet (met reisadres, 'mee' en dergelijke), of None."""
    i = c.execute("SELECT * FROM agenda_items WHERE id = %s", (item_id,)).fetchone()
    if not i:
        return None
    for a in sync.afspraken(c, i["start"] - dt.timedelta(minutes=1), i["eind"] + dt.timedelta(minutes=1), met_verborgen=True):
        if a["id"] == item_id or (a["koppeling_id"], a["extern_id"]) == (i["koppeling_id"], i["extern_id"]):
            return a
    return dict(i)


def _herhalend(c, a):
    if a.get("reeks"):
        return True
    uid = (a.get("ical_uid") or "").strip()
    return bool(uid) and c.execute("SELECT count(*) AS n FROM agenda_items WHERE lower(ical_uid) = lower(%s)", (uid,)).fetchone()["n"] > 1


def _reistijd_scherm(request, c, a, week, fout="", adres=None, vervoer=None, elke=None):
    gekozen = reis.keuzes(c).get(reis.keuze_sleutel(a))
    return templates.TemplateResponse("clara_reistijd.html", ctx(
        request, "clara", a=a, week=week, fout=fout, tz=config.TZ, DAGEN=DAGEN, herhalend=_herhalend(c, a),
        adres=adres if adres is not None else (a.get("reisadres") or (
            "" if a.get("online") or bronnen.ONLINE.search(a.get("locatie") or "") else a.get("locatie") or "")),
        online=bool(a.get("online") or bronnen.ONLINE.search(a.get("locatie") or "")),
        vervoer=vervoer or gekozen or "automatisch", elke=a.get("reis_auto") if elke is None else elke,
        reis_actief=reis.actief(), vertrekpunt=sync.instellingen(c).get("vertrekpunt"), reis_dienst=reis.dienst_naam(),
        doelen=_schrijfdoelen(sync.koppelingen(c))))


@router.get("/clara/reistijd/{item_id:int}", response_class=HTMLResponse)
def reistijd_vragen(request: Request, item_id: int, week: int = 0):
    """Reistijd voor één afspraak (of elke keer van een reeks): adres en vervoer nakijken of invullen."""
    with db.pool.connection() as c:
        a = _item_met_info(c, item_id)
        if not a:
            raise HTTPException(404)
        return _reistijd_scherm(request, c, a, week)


@router.post("/clara/reistijd/{item_id:int}")
async def reistijd_toevoegen(request: Request, item_id: int):
    """Na de klik: adres en vervoer vastleggen, reistijd uitrekenen en de blokken in de agenda zetten. Met 'elke keer'
    plant Clara voortaan ook de volgende keren van de reeks zelf in (tot vier weken vooruit, als de reis past)."""
    controleer_herkomst(request)
    f = await request.form()
    try:
        week = int(str(f.get("week", "0")))
    except ValueError:
        week = 0
    adres = str(f.get("adres", "")).strip()[:300]
    vervoer = str(f.get("vervoer", "automatisch"))
    elke = f.get("elke") == "1"
    modus = str(f.get("modus", ""))

    def doe():
        with db.pool.connection() as c:
            a = _item_met_info(c, item_id)
            if not a:
                raise HTTPException(404)
            inst = sync.instellingen(c)
            if not reis.actief() or not inst.get("vertrekpunt"):
                return None, "Stel eerst je vertrekpunt en de routedienst in bij Agenda-instellingen › Reistijd."
            if not adres:
                return a, "Vul het adres in waar je naartoe gaat."
            thuis = reis.adres(c, inst["vertrekpunt"])
            if not thuis:
                return a, "Je vertrekpunt is niet gevonden. Kijk het na bij Agenda-instellingen › Reistijd."
            c.execute("DELETE FROM plekken WHERE sleutel = %s AND NOT gevonden", (reis._sleutel(adres),))  # pylint: disable=protected-access
            if not reis.adres(c, adres, focus=thuis["punt"]):
                return a, f"Dit adres vindt {reis.dienst_naam()} niet. Probeer straat, huisnummer en plaats, of een bekende plek met plaatsnaam."
            eigen = "" if adres == (a.get("locatie") or "").strip() and not a.get("online") else adres
            herhalend = _herhalend(c, a)
            reis.reisinfo_opslaan(c, a, adres=eigen, auto=False)
            ander = a.get("niet_voor_mij") or a.get("mee")  # afspraak uit een andere agenda, zoals die van Sanne
            if ander and modus in ("beide", "brengen", "halen"):
                if a.get("mee"):  # eerder 'mee' gekozen: nu brengt en haalt hij alleen
                    reis.kies(c, a, "automatisch")
                    a = _item_met_info(c, item_id)
                a["reisadres"] = eigen
                return _brengen_halen(c, a, modus, vervoer, elke and herhalend, inst, _reisdoelen(sync.koppelingen(c)), thuis)
            keuze = vervoer
            if ander and modus == "mee":  # hij blijft erbij: dan is het zijn afspraak, met gewone reistijd
                keuze = vervoer if vervoer in ("fiets", "auto", "ov", "lopen") else reis.vervoer(
                    thuis["punt"], reis.adres(c, adres, focus=thuis["punt"])["punt"], inst)
                reis.kies(c, a, keuze)
                a = _item_met_info(c, item_id)
            reis.kies(c, a, keuze)
            doelen = _reisdoelen(sync.koppelingen(c))
            if not doelen:
                return a, "Om reistijd in te plannen is een Google- of Microsoft-agenda met schrijfrechten nodig."
            dag = dt.datetime.combine(a["start"].astimezone(config.TZ).date(), dt.time(0), tzinfo=config.TZ)
            plan = sync.reisplan(c, dag, dag + dt.timedelta(days=1), alleen_cache=False)
            nu = dt.datetime.now(config.TZ)
            regels = [r for r in plan["regels"] if (r["afspraak"]["koppeling_id"], r["afspraak"]["extern_id"]) == (a["koppeling_id"], a["extern_id"])]
            if not regels:
                return None, (f"Adres voor '{a['titel']}' opgeslagen. Er is geen reistijd nodig: het is (bijna) thuis, of je komt "
                              "van een afspraak op dezelfde plek.")
            if elke and herhalend:
                tekst = _reistijd_voor_reeks(c, a, regels, doelen, inst, nu)
                if tekst:
                    return None, tekst
                reis.reisinfo_opslaan(c, a, auto=True)  # kan niet als reeks: dan steeds vier weken vooruit
            klaar, botst, gebruikt = [], [], set()
            for r in regels:
                if not r.get("minuten") or r["ingepland"] or r["start"] < nu:
                    continue
                doel = _doel_voor(doelen, a.get("domein"))
                _maak(c, doel, reis.blok_titel(r), r["start"], r["eind"], reis.blok_omschrijving(r), bijwerken=False,
                      kleur=str(inst.get("reis_kleur", "8")))
                gebruikt.add(int(doel.split("|")[0]))
                klaar.append(f"{'heen' if r['richting'] == 'heen' else 'terug'} {r['minuten']} min {reis.VERVOER[r['vervoer']]}")
                if not r["past"]:
                    botst.append(r["botst"])
            for kid in gebruikt:
                sync.synchroniseer(c, c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone(), forceer=True)
            tekst = (f"Reistijd voor '{a['titel']}' ingepland: " + ", ".join(klaar) + "." if klaar
                     else f"Reistijd voor '{a['titel']}' stond al in je agenda (of er was geen route te vinden).")
            if botst:
                tekst += f" Let op: dat botst met {botst[0]}."
            if elke and herhalend:
                tekst += (" Deze reeks herhaalt op een manier die Clara niet als reeks kan overnemen (bijvoorbeeld maandelijks): "
                          "ze zet de reistijd voor de volgende keren steeds vier weken vooruit zelf in je agenda.")
                n = sync.reistijd_automatisch(c)
                if n:
                    tekst += f" Nu al: {n.split(': ', 1)[1]}."
            return None, tekst
    try:
        a, tekst = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/clara?week={week}", fout=str(e))
    if a is not None:  # er mist nog iets: het formulier opnieuw, met de vraag erbij
        with db.pool.connection() as c:
            return _reistijd_scherm(request, c, a, week, fout=tekst, adres=adres, vervoer=vervoer, elke=elke)
    return _terug(f"/clara?week={week}", melding=tekst)


def _reeks_herhaling(c, a):
    """De herhaling van de reeks waar deze afspraak bij hoort (in Clara's vorm), of None."""
    k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (a["koppeling_id"],)).fetchone()
    if not a.get("reeks") or not k or k["soort"] not in ("google", "microsoft"):
        return None
    tok = sync.toegang(c, k)
    if k["soort"] == "google":
        return bronnen.herhaal_uit_google(bronnen.google_reeks_ophalen(tok, a["agenda_id"], a["reeks"]))
    return bronnen.herhaal_uit_ms(bronnen.ms_reeks_ophalen(tok, a["agenda_id"], a["reeks"]))


RIT = re.compile(re.escape(reis.MARKER) + r"(\d+):(.+):(brengen|ophalen|brengenhalen)\]")


def _brengen_halen(c, a, modus, vervoer, elke, inst, doelen, thuis):
    """Ritten voor een afspraak in een andere agenda (bijvoorbeeld Sanne naar paardrijles): wegbrengen (heen en weer
    terug naar huis rond het begin) en/of ophalen (rond het einde), in Martijns eigen agenda. Met 'elke keer' als
    herhalende ritten voor de hele reeks. Geeft (afspraak, vraag) als er iets mist, anders (None, melding)."""
    tz = config.TZ
    plek = reis.adres(c, reis.plek_tekst(a), focus=thuis["punt"])
    if not plek:
        return a, f"Dit adres vindt {reis.dienst_naam()} niet."
    soort = vervoer if vervoer in ("fiets", "auto", "ov", "lopen") else reis.vervoer(thuis["punt"], plek["punt"], inst)
    marge = int(inst.get("marge", 5))

    def rit(moment):  # minuten heen (aankomen op moment) en terug (vertrekken op moment)
        h, _ = reis.reistijd(c, thuis["punt"], plek["punt"], soort, tijd=moment, aankomst=True)
        t, _ = reis.reistijd(c, plek["punt"], thuis["punt"], soort, tijd=moment, aankomst=False)
        return h, t
    naam = a.get("agenda_naam") or a.get("koppeling") or "iemand"
    icoon = reis.ICOON[soort]
    blokken = []
    if modus in ("beide", "brengen"):
        h, t = rit(a["start"])
        if h is None or t is None:
            return a, f"{reis.dienst_naam()} vindt geen route met {reis.VERVOER[soort]}. Kies ander vervoer."
        blokken.append(["brengen", a["start"] - dt.timedelta(minutes=h + marge), a["start"] + dt.timedelta(minutes=t),
                        f"{icoon} {naam} brengen ({a['titel']})", f"{h} min heen, {t} min terug"])
    if modus in ("beide", "halen"):
        h, t = rit(a["eind"])
        if h is None or t is None:
            return a, f"{reis.dienst_naam()} vindt geen route met {reis.VERVOER[soort]}. Kies ander vervoer."
        blokken.append(["ophalen", a["eind"] - dt.timedelta(minutes=h), a["eind"] + dt.timedelta(minutes=t + marge),
                        f"{icoon} {naam} ophalen ({a['titel']})", f"{h} min heen, {t} min terug"])
    samen = len(blokken) == 2 and blokken[1][1] <= blokken[0][2] + dt.timedelta(minutes=15)
    if samen:  # de les is korter dan twee keer heen en weer: dan blijf je er beter tussendoor
        blokken = [["brengenhalen", blokken[0][1], blokken[1][2], f"{icoon} {naam} brengen en ophalen ({a['titel']})", blokken[0][4]]]
    doel = _doel_voor(doelen, a.get("domein"))
    if not doel:
        return a, "Om ritten in te plannen is een eigen Google- of Microsoft-agenda met schrijfrechten nodig."
    dk = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (int(doel.split("|")[0]),)).fetchone()
    voor = next((x["naam"] for x in (dk["agendas"] or []) if f"{dk['id']}|{x['id']}" == doel), dk["naam"])
    h = _reeks_herhaling(c, a) if elke else None
    if h and c.execute("""SELECT 1 FROM agenda_reeksen WHERE soort = 'reistijd' AND status = 'actief' AND bron_koppeling = %s
                          AND bron_reeks = %s""", (a["koppeling_id"], a["reeks"])).fetchone():
        return None, f"Voor '{a['titel']}' staan de ritten al als reeks in je agenda. Aanpassen kan bij Herhalende afspraken."
    staat = {m.group(3) for i in c.execute("SELECT omschrijving FROM agenda_items WHERE omschrijving LIKE %s",
                                            (f"{reis.MARKER}{a['koppeling_id']}:%",)).fetchall()
             if (m := RIT.search(i["omschrijving"] or "")) and m.group(2) == a["extern_id"]}
    klaar = []
    for wat, start, eind, titel, uitleg in blokken:
        if not h and wat in staat:
            continue
        oms = (f"{reis.MARKER}{a['koppeling_id']}:{a['extern_id']}:{wat}] Ingepland door Clara (Buurmanitor): {titel}. "
               f"Vervoer: {soort}, {uitleg} ({reis.dienst_naam()}).")
        _, extern = _maak(c, doel, titel, start, eind, oms, bijwerken=False, kleur=str(inst.get("reis_kleur", "8")),
                          herhaal=h)
        if h:
            c.execute("""INSERT INTO agenda_reeksen (koppeling_id, agenda_id, extern_id, titel, start, eind, herhaal, voor, soort,
                                                     bron_koppeling, bron_reeks)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'reistijd', %s, %s)""",
                      (dk["id"], doel.split("|", 1)[1], extern, titel, start, eind, json.dumps(h), voor, a["koppeling_id"], a["reeks"]))
        klaar.append(f"{titel.split(' (')[0][2:]} {start.astimezone(tz):%H.%M}-{eind.astimezone(tz):%H.%M}")
    if not klaar:
        return None, f"De ritten voor '{a['titel']}' stonden al in je agenda."
    sync.synchroniseer(c, dk, forceer=True)
    tekst = f"In je agenda ({voor}): " + "; ".join(klaar)
    if h:
        tekst += f", {bronnen.herhaal_tekst(h)} vanaf {a['start'].astimezone(tz):%d-%m}. Valt een keer weg, dan haalt Clara de rit die dag ook weg"
    elif elke:
        tekst += ". Deze reeks kan Clara niet als reeks overnemen (bijvoorbeeld maandelijks); dit is alleen voor deze keer"
    if samen:
        tekst += ". De les is kort, dus het is één blok: je blijft er tussendoor"
    return None, tekst + "."


def _reistijd_voor_reeks(c, a, regels, doelen, inst, nu):
    """Reistijd voor de hele reeks: herhalende reistijdblokken met dezelfde herhaling als de afspraak, vanaf deze keer.
    Geeft de melding, of None als het niet als reeks kan (dan plant Clara steeds vier weken vooruit)."""
    h = _reeks_herhaling(c, a)
    if not h:
        return None
    doel = _doel_voor(doelen, a.get("domein"))
    dk = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (int(doel.split("|")[0]),)).fetchone()
    voor = next((x["naam"] for x in (dk["agendas"] or []) if f"{dk['id']}|{x['id']}" == doel), dk["naam"])
    klaar = []
    for r in regels:
        if not r.get("minuten") or r["start"] < nu:
            continue
        titel = reis.blok_titel(r)
        _, extern = _maak(c, doel, titel, r["start"], r["eind"], reis.blok_omschrijving(r), bijwerken=False,
                          kleur=str(inst.get("reis_kleur", "8")), herhaal=h)
        c.execute("""INSERT INTO agenda_reeksen (koppeling_id, agenda_id, extern_id, titel, start, eind, herhaal, voor, soort,
                                                 bron_koppeling, bron_reeks)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'reistijd', %s, %s)""",
                  (dk["id"], doel.split("|", 1)[1], extern, titel, r["start"], r["eind"], json.dumps(h), voor, a["koppeling_id"], a["reeks"]))
        klaar.append(f"{'heen' if r['richting'] == 'heen' else 'terug'} {r['minuten']} min {reis.VERVOER[r['vervoer']]}")
        if r["ingepland"]:  # er stond al een los blok voor deze keer: dat haalt Clara weg (de reeks dekt het nu)
            los = c.execute("""SELECT * FROM agenda_items WHERE omschrijving LIKE %s AND reeks = ''""",
                            (reis.MARKER + r["sleutel"] + "]%",)).fetchone()
            if los:
                lk = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (los["koppeling_id"],)).fetchone()
                try:
                    (bronnen.google_verwijder if lk["soort"] == "google" else bronnen.ms_verwijder)(sync.toegang(c, lk), los["agenda_id"], los["extern_id"])
                except bronnen.NietGevonden:
                    pass
    if not klaar:
        return None
    sync.synchroniseer(c, dk, forceer=True)
    return (f"Reistijd voor elke keer van '{a['titel']}' staat in je agenda ({bronnen.herhaal_tekst(h)}, vanaf "
            f"{a['start'].astimezone(config.TZ):%d-%m}): " + ", ".join(klaar) + ". Vervalt een keer, dan haalt Clara de reistijd "
            "die dag ook weg. Je vindt de reistijdreeks bij Herhalende afspraken.")


@router.post("/clara/reistijd/{item_id:int}/stop")
async def reistijd_stop(request: Request, item_id: int):
    """'Elke keer' weer uitzetten (blokken die er al staan blijven staan)."""
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        a = _item_met_info(c, item_id)
        if not a:
            raise HTTPException(404)
        reis.reisinfo_opslaan(c, a, auto=False)
    return _terug(f"/clara?week={f.get('week') or 0}", melding=f"Clara plant de reistijd voor '{a['titel']}' niet meer zelf in.")


def _kopieen(c, a):
    """Dezelfde afspraak in alle gekoppelde agenda's (zelfde iCalUID en begin, of zelfde titel en tijd)."""
    uid = (a.get("ical_uid") or "").strip().lower()
    titel = " ".join((a.get("titel") or "").lower().split())
    rijen = c.execute("""SELECT i.*, k.naam AS koppeling, k.soort, k.schrijven, k.agendas AS k_agendas, k.fout
                         FROM agenda_items i JOIN agenda_koppelingen k ON k.id = i.koppeling_id
                         WHERE k.actief AND i.start = %s""", (a["start"],)).fetchall()
    uit = []
    for r in rijen:
        r = dict(r)
        if (spiegel.is_spiegel(r) or reis.is_reisblok(r)) and r["id"] != a.get("id"):
            continue
        zelfde = (uid and (r.get("ical_uid") or "").strip().lower() == uid) or (
            " ".join((r.get("titel") or "").lower().split()) == titel and r["eind"] == a["eind"])
        if not zelfde and r["id"] != a.get("id"):
            continue
        ag = next((x for x in (r["k_agendas"] or []) if x["id"] == r["agenda_id"]), {})
        r["agenda_naam"] = ag.get("naam") or r["koppeling"]
        r["kan"] = r["soort"] in ("google", "microsoft") and r["schrijven"] and not r["fout"] and ag.get("schrijven", True) is not False
        r["organisator_ik"] = r.get("antwoord") == "organisator"
        uit.append(r)
    return uit


def _bijbehorende_reistijd(c, kopieen, hele_reeks):
    """Reistijd en ritten van Clara bij deze afspraak (losse blokken, en bij de hele reeks ook de reistijdreeksen)."""
    blokken, reeksen = [], []
    for k in kopieen:
        blokken += c.execute("""SELECT i.*, kk.soort FROM agenda_items i JOIN agenda_koppelingen kk ON kk.id = i.koppeling_id
                                WHERE i.omschrijving LIKE %s AND i.reeks = ''""",
                             (f"{reis.MARKER}{k['koppeling_id']}:{k['extern_id']}:%",)).fetchall()
        if hele_reeks and k.get("reeks"):
            reeksen += c.execute("""SELECT * FROM agenda_reeksen WHERE soort = 'reistijd' AND status = 'actief'
                                    AND bron_koppeling = %s AND bron_reeks = %s""", (k["koppeling_id"], k["reeks"])).fetchall()
    return blokken, reeksen


@router.get("/clara/verwijder/{item_id:int}", response_class=HTMLResponse)
def verwijder_vragen(request: Request, item_id: int, week: int = 0):
    """Eerst laten zien wat er weggaat: in welke agenda's, deze keer of de hele reeks, afzegging, reistijd."""
    with db.pool.connection() as c:
        a = _item_met_info(c, item_id)
        if not a:
            raise HTTPException(404)
        kopieen = _kopieen(c, a)
        blokken, reeksen = _bijbehorende_reistijd(c, kopieen, True)
        return templates.TemplateResponse("clara_verwijder.html", ctx(
            request, "clara", a=a, kopieen=kopieen, week=week, tz=config.TZ, DAGEN=DAGEN, herhalend=_herhalend(c, a),
            organisator=any(k["organisator_ik"] for k in kopieen if k["kan"]), reistijd=bool(blokken or reeksen)))


@router.post("/clara/verwijder/{item_id:int}")
async def verwijderen(request: Request, item_id: int):
    """Na de bevestiging: de afspraak uit de aangevinkte agenda's halen (deze keer of de hele reeks)."""
    controleer_herkomst(request)
    f = await request.form()
    week = str(f.get("week") or 0)
    gekozen = {int(x) for x in f.getlist("kopie") if str(x).isdigit()}
    hele_reeks = f.get("bereik") == "reeks"
    melden = f.get("melden") == "1"
    met_reistijd = f.get("reistijd") == "1"
    if not gekozen:
        return _terug(f"/clara?week={week}", fout="Niets aangevinkt; er is niets verwijderd.")

    def doe():
        with db.pool.connection() as c:
            a = _item_met_info(c, item_id)
            if not a:
                raise bronnen.BronFout("Deze afspraak bestaat niet meer.")
            kopieen = [k for k in _kopieen(c, a) if k["id"] in gekozen and k["kan"]]
            if not kopieen:
                raise bronnen.BronFout("In de aangevinkte agenda's mag Clara niets verwijderen.")
            blokken, reeksen = _bijbehorende_reistijd(c, kopieen, hele_reeks) if met_reistijd else ([], [])
            weg, gebruikt = [], set()
            for k in kopieen:
                kk = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (k["koppeling_id"],)).fetchone()
                tok = sync.toegang(c, kk)
                doel = k["reeks"] if hele_reeks and k.get("reeks") else k["extern_id"]
                try:
                    (bronnen.google_verwijder if kk["soort"] == "google" else bronnen.ms_verwijder)(
                        tok, k["agenda_id"], doel, melden=melden and k["organisator_ik"])
                except bronnen.NietGevonden:
                    pass
                if hele_reeks and k.get("reeks"):
                    c.execute("UPDATE agenda_reeksen SET status = 'verwijderd' WHERE koppeling_id = %s AND extern_id = %s",
                              (kk["id"], k["reeks"]))
                weg.append(k["agenda_naam"])
                gebruikt.add(kk["id"])
            for b in blokken:
                kk = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (b["koppeling_id"],)).fetchone()
                try:
                    (bronnen.google_verwijder if kk["soort"] == "google" else bronnen.ms_verwijder)(sync.toegang(c, kk), b["agenda_id"], b["extern_id"])
                except bronnen.NietGevonden:
                    pass
                gebruikt.add(kk["id"])
            for r in reeksen:
                kk = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (r["koppeling_id"],)).fetchone()
                try:
                    (bronnen.google_verwijder if kk["soort"] == "google" else bronnen.ms_verwijder)(sync.toegang(c, kk), r["agenda_id"], r["extern_id"])
                except bronnen.NietGevonden:
                    pass
                c.execute("UPDATE agenda_reeksen SET status = 'verwijderd' WHERE id = %s", (r["id"],))
                gebruikt.add(kk["id"])
            c.execute("INSERT INTO logboek (persona, actie, details) VALUES ('clara', 'afspraak verwijderd', %s)",
                      (json.dumps({"titel": a["titel"], "start": a["start"].isoformat(), "agendas": weg, "hele_reeks": hele_reeks,
                                   "afzegging": melden, "reistijd": len(blokken) + len(reeksen)}),))
            for kid in gebruikt:
                sync.synchroniseer(c, c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone(), forceer=True)
            tekst = (f"'{a['titel']}' {'(de hele reeks) ' if hele_reeks else ''}is verwijderd uit " + ", ".join(weg) + ".")
            if blokken or reeksen:
                tekst += " De reistijd erbij is ook weg."
            return tekst
    try:
        tekst = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/clara?week={week}", fout=str(e))
    return _terug(f"/clara?week={week}", melding=tekst)


RAAD_MARKER = "[clara-raad:"


def _raadsbronnen(c, ook_uit=False):
    """Bronnen voor de pagina Raadsagenda: de raadsagenda altijd (ook als hij niet in de week staat), agendalinks als ze aan staan."""
    return [k for k in sync.koppelingen(c) if k["soort"] == "raad" or (k["soort"] == "ics" and (k["actief"] or ook_uit))]


@router.get("/clara/raadsagenda", response_class=HTMLResponse)
def raadsagenda(request: Request, melding: str = "", fout: str = ""):
    """Vergaderingen uit de raadsagenda (Notubiz) en agendalinks (zoals Bundeling): kies wat in je agenda komt."""
    tz = config.TZ
    nu = dt.datetime.now(tz)
    with db.pool.connection() as c:
        bronnen_ = _raadsbronnen(c)
        namen = {k["id"]: k["naam"] for k in bronnen_}
        items = [dict(r) for r in c.execute("""SELECT * FROM agenda_items WHERE koppeling_id = ANY(%s) AND eind > %s
                                                AND status <> 'geannuleerd' ORDER BY start, koppeling_id""",
                                             ([k["id"] for k in bronnen_] or [0], nu)).fetchall()]
        over = {(r["bron_koppeling"], r["bron_extern"]): r for r in c.execute(
            "SELECT * FROM agenda_overgenomen WHERE status = 'actief'").fetchall()}
        eigen = [dict(r) for r in c.execute("""SELECT i.titel, i.start, i.eind, i.domein FROM agenda_items i
                                                JOIN agenda_koppelingen k ON k.id = i.koppeling_id
                                                WHERE k.actief AND k.soort IN ('google', 'microsoft') AND i.eind > %s""", (nu,)).fetchall()]
        doelen = _reisdoelen(sync.koppelingen(c))
    for i in items:
        i["bron"] = namen.get(i["koppeling_id"], "")
        i["over"] = over.get((i["koppeling_id"], i["extern_id"]))
        t = " ".join((i["titel"] or "").lower().split())
        i["al"] = not i["over"] and any(
            (e["start"] == i["start"] and " ".join((e["titel"] or "").lower().split()) == t)
            or (e.get("domein") == "politiek" and abs((e["start"] - i["start"]).total_seconds()) <= 45 * 60) for e in eigen)
    standaard = next((w for w, _l, d in doelen if d == "politiek"), doelen[0][0] if doelen else "")
    # Per bron laten zien wat er is opgehaald (en of het lukte), zodat een lege lijst te verklaren is.
    with db.pool.connection() as c:
        alle = _raadsbronnen(c, ook_uit=True)
        tellen = {r["koppeling_id"]: r for r in c.execute("""SELECT koppeling_id, count(*) AS alles,
                                                               count(*) FILTER (WHERE eind > %s) AS komend
                                                             FROM agenda_items WHERE koppeling_id = ANY(%s) GROUP BY koppeling_id""",
                                                          (nu, [k["id"] for k in alle] or [0])).fetchall()}
    status = [{"k": k, "alles": tellen.get(k["id"], {}).get("alles", 0), "komend": tellen.get(k["id"], {}).get("komend", 0)} for k in alle]
    return templates.TemplateResponse("clara_raadsagenda.html", ctx(
        request, "clara", items=items, bronnen=bronnen_, doelen=doelen, standaard=standaard, tz=tz, DAGEN=DAGEN,
        melding=melding, fout=fout, politiek_ok=config.POLITIEK_ACTIEF, status=status,
        heeft_raad=any(k["soort"] == "raad" for k in alle)))


@router.post("/clara/raadsagenda/vernieuwen")
async def raadsagenda_vernieuwen(request: Request):
    """De raadsagenda en de agendalinks nu meteen opnieuw ophalen."""
    controleer_herkomst(request)
    with db.pool.connection() as c:
        ids = [k["id"] for k in _raadsbronnen(c)]
    uitslag = [u for kid in ids if (u := await asyncio.to_thread(_sync_los, kid))]
    return _terug("/clara/raadsagenda", melding=("Opgehaald. " + "; ".join(uitslag)) if uitslag else "Er is geen actieve raadsagenda of agendalink.")


@router.post("/clara/raadsagenda")
async def raadsagenda_overnemen(request: Request):
    """Na de klik: de aangevinkte vergaderingen als afspraak in de gekozen agenda zetten."""
    controleer_herkomst(request)
    f = await request.form()
    gekozen = [int(x) for x in f.getlist("item") if str(x).isdigit()]
    doel = str(f.get("doel", ""))
    if not gekozen:
        return _terug("/clara/raadsagenda", fout="Niets aangevinkt.")

    def doe():
        with db.pool.connection() as c:
            doelen = {w for w, _l, _d in _reisdoelen(sync.koppelingen(c))}
            if doel not in doelen:
                raise bronnen.BronFout("Kies een agenda om de vergaderingen in te zetten.")
            bron_ids = [k["id"] for k in _raadsbronnen(c)]
            klaar, kid = [], int(doel.split("|")[0])
            for i in c.execute("SELECT * FROM agenda_items WHERE id = ANY(%s) AND koppeling_id = ANY(%s)", (gekozen, bron_ids or [0])).fetchall():
                if c.execute("""SELECT 1 FROM agenda_overgenomen WHERE bron_koppeling = %s AND bron_extern = %s AND doel_koppeling = %s
                                AND status = 'actief'""", (i["koppeling_id"], i["extern_id"], kid)).fetchone():
                    continue
                oms = (f"{RAAD_MARKER}{i['koppeling_id']}:{i['extern_id']}] Overgenomen uit de raadsagenda door Clara (Buurmanitor)."
                       + (" Het einde is geschat (Notubiz geeft alleen een begintijd)." if i["geschat"] else "")
                       + (f" Stukken: {i['link']}" if i["link"] else ""))
                _, extern = _maak(c, doel, i["titel"], i["start"], i["eind"], oms, bijwerken=False, locatie=i["locatie"] or "")
                c.execute("""INSERT INTO agenda_overgenomen (bron_koppeling, bron_extern, doel_koppeling, doel_agenda, extern_id, titel,
                                                             start, eind, locatie)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (bron_koppeling, bron_extern, doel_koppeling) DO UPDATE SET extern_id = EXCLUDED.extern_id,
                               doel_agenda = EXCLUDED.doel_agenda, titel = EXCLUDED.titel, start = EXCLUDED.start, eind = EXCLUDED.eind,
                               locatie = EXCLUDED.locatie, status = 'actief'""",
                          (i["koppeling_id"], i["extern_id"], kid, doel.split("|", 1)[1], extern, i["titel"], i["start"], i["eind"], i["locatie"] or ""))
                klaar.append(i["titel"])
            if klaar:
                sync.synchroniseer(c, c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone(), forceer=True)
            return klaar
    try:
        klaar = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug("/clara/raadsagenda", fout=str(e))
    if not klaar:
        return _terug("/clara/raadsagenda", melding="Die vergaderingen stonden al in je agenda.")
    return _terug("/clara/raadsagenda", melding=f"{len(klaar)} vergadering{'en' if len(klaar) != 1 else ''} in je agenda gezet. "
                  "Verandert de tijd of plek in de raadsagenda, dan past Clara je afspraak aan.")


@router.get("/clara/reistijd/overzicht", response_class=HTMLResponse)
def reistijd_overzicht(request: Request, week: int = 0, sleutel: list[str] = Query(default=[])):
    """Eerst laten zien waar Clara reistijd wil zetten; Martijn vinkt aan wat er echt in de agenda komt."""
    tz = config.TZ
    van = dt.datetime.combine(_week_start(week), dt.time(0), tzinfo=tz)
    nu = dt.datetime.now(tz)
    fout = ""
    with db.pool.connection() as c:
        doelen = _reisdoelen(sync.koppelingen(c))
        try:
            plan = sync.reisplan(c, van, van + dt.timedelta(days=DAGEN_IN_BEELD), alleen_cache=False)
        except bronnen.BronFout as e:
            plan, fout = {"regels": []}, str(e)
    gekozen = set(sleutel)
    namen = {w: label for w, label, _d in doelen}
    regels = [dict(r, doel=namen.get(_doel_voor(doelen, r["afspraak"].get("domein")), "")) for r in plan.get("regels", [])
              if r.get("minuten") and not r["ingepland"] and r["start"] > nu and (not gekozen or r["sleutel"] in gekozen)]
    return templates.TemplateResponse("clara_reistijd_overzicht.html", ctx(
        request, "clara", regels=regels, week=week, tz=tz, DAGEN=DAGEN, doelen=doelen, fout=fout or plan.get("fout", ""),
        alleen=bool(gekozen), ICOON=reis.ICOON, VERVOER=reis.VERVOER))


@router.post("/clara/vervoer/{item_id}")
async def vervoer_kiezen(request: Request, item_id: int):
    """Vervoer voor één afspraak (en bij een reeks voor alle keren) kiezen, of terug naar automatisch."""
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        a = c.execute("SELECT * FROM agenda_items WHERE id = %s", (item_id,)).fetchone()
        if not a:
            raise HTTPException(404)
        gekozen = reis.kies(c, a, str(f.get("vervoer", "")))

    def opnieuw():  # meteen uitrekenen, zodat de nieuwe reistijd direct te zien is
        with db.pool.connection() as c2:
            sync.reistijd_bijwerken(c2)
    await asyncio.to_thread(opnieuw)
    tekst = f"'{a['titel']}': {reis.VERVOER[gekozen]}" if gekozen else f"'{a['titel']}': weer automatisch (fiets dichtbij, OV in een grote stad, anders auto)"
    return _terug(f"/clara?week={f.get('week') or 0}", melding=tekst + ".")


@router.post("/clara/reistijd/instellingen")
async def reistijd_instellingen(request: Request):
    controleer_herkomst(request)
    f = await request.form()

    def getal(naam, standaard, lo, hi):
        try:
            return max(lo, min(hi, float(str(f.get(naam)).replace(",", "."))))
        except (TypeError, ValueError):
            return standaard
    with db.pool.connection() as c:
        inst = sync.instellingen(c)
        inst.update({"vertrekpunt": str(f.get("vertrekpunt", "")).strip()[:200], "fiets_km": getal("fiets_km", 6, 0, 50),
                     "marge": int(getal("marge", 5, 0, 60)),
                     "reis_kleur": str(f.get("reis_kleur", "8")) if str(f.get("reis_kleur", "8")) in bronnen.GOOGLE_KLEUREN else "8"})
        sync.instellingen_opslaan(c, inst)
        if not inst["vertrekpunt"]:
            return _terug("/clara/koppelingen#reistijd", melding="Reistijd: vertrekpunt leeggemaakt.")
        if not reis.actief():
            return _terug("/clara/koppelingen#reistijd", fout="Opgeslagen, maar er is nog geen sleutel voor Google Maps of OpenRouteService (draai clara.sh).")
        try:
            c.execute("DELETE FROM plekken WHERE sleutel = %s AND NOT gevonden", (reis._sleutel(inst["vertrekpunt"]),))  # pylint: disable=protected-access
            gevonden = await asyncio.to_thread(lambda: _adres_los(inst["vertrekpunt"]))
        except bronnen.BronFout as e:
            return _terug("/clara/koppelingen#reistijd", fout=f"Opgeslagen, maar het opzoeken lukte niet: {e}")
    if not gevonden:
        return _terug("/clara/koppelingen#reistijd", fout=f"Dit adres vond {reis.dienst_naam()} niet. Probeer straat, huisnummer, postcode en plaats.")
    return _terug("/clara/koppelingen#reistijd", melding=f"Vertrekpunt gevonden: {gevonden['label']}. Clara rekent de reistijd binnen vijf minuten uit.")


@router.post("/clara/spiegel")
async def spiegel_instellingen(request: Request):
    """Beschikbaarheid delen: aan/uit, wat anderen zien, en naar welke accounts."""
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        inst = sync.instellingen(c)
        aan = f.get("spiegel_aan") == "1"
        inst.update({"spiegel_aan": aan, "spiegel_titel": "titel" if f.get("spiegel_titel") == "titel" else "bezet",
                     "spiegel_raad": f.get("raad_mee") == "1"})
        sync.instellingen_opslaan(c, inst)
        for k in sync.koppelingen(c):
            if spiegel.rol(k)[0] or f.get(f"toon_{k['id']}"):
                c.execute("UPDATE agenda_koppelingen SET spiegel = %s WHERE id = %s", (f.get(f"doel_{k['id']}") == "1", k["id"]))
    return _terug("/clara/koppelingen#beschikbaarheid", melding=(
        "Opgeslagen. Clara zet je beschikbaarheid binnen vijf minuten door (de eerste keer in een paar rondes)." if aan
        else "Opgeslagen. Doorzetten staat uit; blokken die Clara al zette haalt ze binnen vijf minuten weg."))


@router.post("/clara/reistijd/bijwerken")
async def reistijd_blokken_bijwerken(request: Request):
    """Bestaande reistijdblokken in Google Agenda het icoon en de gekozen kleur geven (na een klik)."""
    controleer_herkomst(request)

    def doe():
        with db.pool.connection() as c:
            kleur = str(sync.instellingen(c).get("reis_kleur", "8"))
            rijen = c.execute("""SELECT i.*, k.id AS kid FROM agenda_items i JOIN agenda_koppelingen k ON k.id = i.koppeling_id
                                 WHERE k.soort = 'google' AND k.schrijven AND i.eind > now() AND i.omschrijving LIKE %s""",
                              (reis.MARKER + "%",)).fetchall()
            klaar, gebruikt = 0, set()
            for r in rijen:
                k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (r["kid"],)).fetchone()
                bronnen.google_wijzig(sync.toegang(c, k), r["agenda_id"], r["extern_id"], titel=reis.titel_met_icoon(r), kleur=kleur)
                gebruikt.add(r["kid"])
                klaar += 1
            for kid in gebruikt:
                sync.synchroniseer(c, c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone(), forceer=True)
            return klaar
    try:
        n = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug("/clara/koppelingen#reistijd", fout=str(e))
    return _terug("/clara/koppelingen#reistijd", melding=f"{n} reistijdblok{'ken' if n != 1 else ''} bijgewerkt (icoon en kleur)." if n
                  else "Er stonden geen komende reistijdblokken van Clara in Google Agenda.")


def _adres_los(tekst):
    with db.pool.connection() as c:
        return reis.adres(c, tekst)


@router.post("/clara/verbergen")
async def verbergen(request: Request):
    """Een verbergregel toevoegen (vanaf een uitnodiging of met het formulier) of weghalen."""
    controleer_herkomst(request)
    f = await request.form()
    terug = "/clara" if f.get("van") == "week" else "/clara/koppelingen#verbergen"
    with db.pool.connection() as c:
        regels = sync.verbergregels(c)
        if f.get("weg") is not None:
            try:
                regels.pop(int(str(f.get("weg"))))
            except (ValueError, IndexError):
                pass
            sync.verbergregels_opslaan(c, regels)
            return _terug(terug, melding="Regel weggehaald; die afspraken zijn weer te zien.")
        if f.get("item"):
            i = c.execute("SELECT organisator, titel FROM agenda_items WHERE id = %s", (int(str(f.get("item"))),)).fetchone()
            nieuw = {"organisator": (i["organisator"] if i else "")[:120], "tekst": (i["titel"] if i else "")[:200]}
        else:
            nieuw = {"organisator": str(f.get("organisator", "")).strip()[:120], "tekst": str(f.get("tekst", "")).strip()[:200]}
        if not (nieuw["organisator"] or nieuw["tekst"]):
            return _terug(terug, fout="Vul een organisator of een stukje tekst in.")
        if nieuw not in regels:
            regels.append(nieuw)
        sync.verbergregels_opslaan(c, regels)
        sync.meldingen_opruimen(c)
    return _terug(terug, melding="Verborgen. Clara laat deze afspraken voortaan weg (in de agenda zelf blijven ze staan).")


@router.post("/clara/instellingen")
async def instellingen(request: Request):
    controleer_herkomst(request)
    f = await request.form()

    def getal(naam, standaard, lo, hi):
        try:
            return max(lo, min(hi, int(str(f.get(naam)))))
        except (TypeError, ValueError):
            return standaard
    nieuw = {"werkdagen": [d for d in range(7) if f.get(f"dag_{d}") == "1"] or [0, 1, 2, 3, 4],
             "begin": str(f.get("begin") or "08:30")[:5], "eind": str(f.get("eind") or "17:30")[:5],
             "avond": f.get("avond") == "1", "buffer": getal("buffer", 10, 0, 60), "reistijd": getal("reistijd", 30, 0, 180)}
    with db.pool.connection() as c:
        sync.instellingen_opslaan(c, {**sync.instellingen(c), **nieuw})
    return _terug("/clara/koppelingen#werktijden", melding="Instellingen opgeslagen.")
