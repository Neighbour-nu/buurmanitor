"""Schermen van Clara: de week, koppelingen, uitnodigingen, vrije tijd zoeken en het gesprek.

Alles wat iets in een agenda verandert (een blok inplannen, een uitnodiging beantwoorden) is een formulier met een
knop: Clara doet het nooit uit zichzelf (autonomie niveau 1).
"""
import asyncio
import datetime as dt
import json
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import personas
from ..kern import claude, config, db
from ..kern.web import DAGEN, controleer_herkomst, ctx, templates
from . import analyse, bronnen, gesprek, kluis, sync

router = APIRouter()
DOMEINEN = config.DOMEINEN


def _terug(pad, **melding):
    from urllib.parse import urlencode  # pylint: disable=import-outside-toplevel
    return RedirectResponse(pad + ("?" + urlencode(melding) if melding else ""), status_code=303)


def _week_start(week):
    vandaag = dt.datetime.now(config.TZ).date()
    return vandaag + dt.timedelta(days=7 * week) if week else vandaag


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
        lijst = sync.afspraken(c, van, van + dt.timedelta(days=7))
        komend = sync.afspraken(c, nu, nu + dt.timedelta(days=56))
        vrij = analyse.vrije_blokken(sync.afspraken(c, nu, nu + dt.timedelta(days=max(1, min(dagen, 28)) + 1)), nu,
                                     max(1, min(dagen, 28)), max(15, min(duur, 600)), inst, tz) if duur else []
        if g is None:
            gs = c.execute("SELECT * FROM gesprekken WHERE persona = 'clara' ORDER BY bijgewerkt DESC LIMIT 1").fetchone()
        else:
            gs = c.execute("SELECT * FROM gesprekken WHERE id = %s AND persona = 'clara'", (g,)).fetchone() if g else None
        berichten = c.execute("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gs["id"],)).fetchall() if gs else []
        voorstellen = c.execute("SELECT * FROM agenda_voorstellen WHERE gesprek_id = %s AND status = 'open' ORDER BY start",
                                (gs["id"],)).fetchall() if gs else []
    conf = analyse.conflicten(komend)
    in_conflict = {id(x["a"]) for x in conf} | {id(x["b"]) for x in conf}
    conflict_ids = {(a["koppeling_id"], a["extern_id"]) for a in komend if id(a) in in_conflict}
    over = analyse.overgangen(komend, inst["reistijd"])
    krap_ids = {(x["naar"]["koppeling_id"], x["naar"]["extern_id"]) for x in over}
    return templates.TemplateResponse("clara.html", ctx(
        request, "clara", p=personas.get("clara"), nu=nu, week=week, start=start, dagen_lijst=analyse.per_dag(lijst, tz),
        alle_dagen=[start + dt.timedelta(days=d) for d in range(7)], conflicten=conf, overgangen=over,
        conflict_ids=conflict_ids, krap_ids=krap_ids, uitnodigingen=analyse.uitnodigingen(komend, nu),
        koppelingen=koppelingen, doelen=_schrijfdoelen(koppelingen), vrij=vrij, duur=duur, dagen=dagen, tz=tz,
        DAGEN=DAGEN, gesprek=gs, berichten=berichten, voorstellen=voorstellen, melding=melding, fout=fout,
        kluis_ok=kluis.actief(), sleutel_ok=bool(config.ANTHROPIC_API_KEY), inst=inst,
        domeinnaam=dict(DOMEINEN)))


@router.post("/clara/vernieuwen")
def vernieuwen(request: Request):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        uitslag = sync.taak_agenda(c, forceer=True)
    return _terug("/clara", melding=uitslag[:300])


# ---------------------------------------------------------------- iets in de agenda zetten (na een klik)

def _maak(c, doel, titel, start, eind, omschrijving=""):
    try:
        kid, agenda_id = doel.split("|", 1)
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (int(kid),)).fetchone()
    except ValueError:
        k = None
    if not k or not k["schrijven"]:
        raise bronnen.BronFout("In deze agenda mag Clara niets zetten.")
    tok = sync.toegang(c, k)
    maak = bronnen.google_maak if k["soort"] == "google" else bronnen.ms_maak
    extern = maak(tok, agenda_id, titel, start, eind, omschrijving)
    c.execute("INSERT INTO logboek (persona, actie, details) VALUES ('clara', 'afspraak gemaakt', %s)",
              (json.dumps({"koppeling": k["naam"], "titel": titel, "start": start.isoformat(), "eind": eind.isoformat()}),))
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
            kid, extern = _maak(c, str(f.get("doel", "")), v["titel"], v["start"], v["eind"], "Voorstel van Clara (Buurmanitor).")
            c.execute("UPDATE agenda_voorstellen SET status = 'ingepland', koppeling_id = %s, extern_id = %s WHERE id = %s",
                      (kid, extern, vid))
    try:
        await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/clara?g={v['gesprek_id'] or 0}", fout=str(e))
    return _terug(f"/clara?g={v['gesprek_id'] or 0}", melding=f"'{v['titel']}' staat in je agenda.")


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
        aantallen = {r["koppeling_id"]: r["n"] for r in c.execute(
            "SELECT koppeling_id, count(*) AS n FROM agenda_items WHERE start > now() GROUP BY koppeling_id").fetchall()}
    return templates.TemplateResponse("clara_koppelingen.html", ctx(
        request, "clara", koppelingen=lijst, aantallen=aantallen, inst=inst, melding=melding, fout=fout,
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
            agendas.append({**a, "aan": f.get(f"aan_{a['id']}") == "1", "domein": d if d in dict(DOMEINEN) else domein})
        c.execute("""UPDATE agenda_koppelingen SET naam = %s, domein = %s, agendas = %s, schrijfagenda = %s, actief = %s
                     WHERE id = %s""",
                  (str(f.get("naam") or k["naam"])[:80], domein if domein in dict(DOMEINEN) else k["domein"], json.dumps(agendas),
                   str(f.get("schrijfagenda", k["schrijfagenda"])), f.get("actief") == "1", kid))
    uitslag = await asyncio.to_thread(_sync_los, kid)
    return _terug("/clara/koppelingen", melding=f"Opgeslagen. {uitslag}")


@router.post("/clara/instellingen")
async def instellingen(request: Request):
    controleer_herkomst(request)
    f = await request.form()

    def getal(naam, standaard, lo, hi):
        try:
            return max(lo, min(hi, int(str(f.get(naam)))))
        except (TypeError, ValueError):
            return standaard
    inst = {"werkdagen": [d for d in range(7) if f.get(f"dag_{d}") == "1"] or [0, 1, 2, 3, 4],
            "begin": str(f.get("begin") or "08:30")[:5], "eind": str(f.get("eind") or "17:30")[:5],
            "avond": f.get("avond") == "1", "buffer": getal("buffer", 10, 0, 60), "reistijd": getal("reistijd", 30, 0, 180)}
    with db.pool.connection() as c:
        sync.instellingen_opslaan(c, inst)
    return _terug("/clara/koppelingen", melding="Instellingen opgeslagen.")
