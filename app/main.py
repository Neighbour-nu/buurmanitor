"""Buurmanitor: de persoonlijke portal van Martijn Buurman.

De kern: inloggen via Caddy en Authelia, meldingen, de planner, de gezondheid van de server en gesprekken met Iris.
Werkruimtes: Promonitor (fase 1) onder /politiek en de Doellijn (fase 2) onder /zerohero, elk één op één.
"""
import asyncio
import datetime as dt
import os
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import aankondigen, iris, personas  # noqa: F401  (aankondigen registreert de planner-taak)
from .bram import sync as bram_sync, web as bram_web  # noqa: F401  (sync registreert de planner-taak 'post')
from .clara import sync as clara_sync, web as clara_web  # noqa: F401  (sync registreert de planner-taak)
from .taken import noor, web as taken_web
from .jesse import luisteren as jesse_luisteren, web as jesse_web  # noqa: F401  (luisteren registreert de planner-taak)
from .vera import nieuws as vera_nieuws, web as vera_web  # noqa: F401  (nieuws registreert de planner-taak)
from .kern import claude, config, db, gezondheid, meldingen, planner, prefix, snelheid, web
from .kern.db import q, q1, uitvoeren
from .kern.web import controleer_herkomst, ctx, templates
from .schema import MIGRATIES

# Werkruimte Politiek (Promonitor, één op één) onder /politiek, zodra de database is ingesteld.
politiek = None
if config.POLITIEK_ACTIEF:
    from .politiek import main as politiek  # noqa: E402  pylint: disable=wrong-import-position
# Werkruimte ZEROHERO (de Doellijn, één op één) onder /zerohero, zodra de database is ingesteld.
zerohero = None
if config.ZEROHERO_ACTIEF:
    from .zerohero import main as zerohero  # noqa: E402  pylint: disable=wrong-import-position


@planner.taak("gezondheid", elke=dt.timedelta(minutes=15))
def _gezondheid(c):
    return gezondheid.controle(c)


@asynccontextmanager
async def lifespan(_app):
    db.pool.open(wait=True, timeout=30)
    db.achtergrond.open(wait=True, timeout=30)
    with db.pool.connection() as c:
        db.migreer(c, MIGRATIES)
        planner.opruimen(c)
    lus = asyncio.create_task(planner.lus(db.achtergrond)) if config.PLANNER_AAN else None
    async with AsyncExitStack() as werkruimtes:
        # Gekoppelde apps krijgen hun eigen opstart (database, migraties, planner) niet vanzelf; die starten we hier.
        if politiek:
            await werkruimtes.enter_async_context(politiek.lifespan(politiek.app))
        if zerohero:
            await werkruimtes.enter_async_context(zerohero.lifespan(zerohero.app))
        yield
    if lus:
        lus.cancel()
    db.achtergrond.close()
    db.pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(web.BASIS, "static")), name="static")
app.middleware("http")(web.alleen_via_caddy)
app.middleware("http")(snelheid.meten)  # als laatste toegevoegd, dus buitenom: meet ook de controle hierboven
app.include_router(clara_web.router)
app.include_router(bram_web.router)
app.include_router(taken_web.router)
app.include_router(vera_web.router)
app.include_router(jesse_web.router)
if politiek:
    app.mount("/politiek", prefix.Voorvoegsel(politiek.app, "/politiek"))
if zerohero:
    app.mount("/zerohero", prefix.Voorvoegsel(zerohero.app, "/zerohero"))
    # n8n roept /intern/... aan (zonder voorvoegsel); die gaan langs de portal heen naar de werkruimte.
    app.add_middleware(prefix.Doorgeven, begin="/intern/", doel=zerohero.app)


# ---------- Vandaag ----------

def clara_vandaag(c, nu):
    """Wat Clara vandaag ziet: de afspraken van vandaag en de dubbele boekingen van de komende week."""
    from .clara import analyse  # pylint: disable=import-outside-toplevel
    if not clara_sync.koppelingen(c, alleen_actief=True):
        return None
    begin = dt.datetime.combine(nu.date(), dt.time(0), tzinfo=config.TZ)
    vandaag = clara_sync.afspraken(c, begin, begin + dt.timedelta(days=1))
    week = clara_sync.afspraken(c, nu, nu + dt.timedelta(days=7))
    return {"vandaag": sorted(vandaag, key=lambda a: (not a["heledag"], a["start"])),
            "conflicten": analyse.conflicten(week), "uitnodigingen": analyse.uitnodigingen(week, nu), "tz": config.TZ}


def bram_vandaag(c):
    """Wat Bram ziet: hoeveel berichten om actie vragen en hoeveel er weg kunnen."""
    if not bram_sync.koppelingen(c, alleen_actief=True):
        return None
    rijen = c.execute("""SELECT b.id, b.categorie, b.prioriteit, b.actie, b.groep_handmatig, b.van_naam, b.van_adres, b.onderwerp,
                                b.regel, b.ontvangen, b.gelezen, k.naam AS koppeling FROM post_berichten b JOIN post_koppelingen k
                         ON k.id = b.koppeling_id WHERE b.status = 'open' AND k.actief ORDER BY b.ontvangen DESC""").fetchall()
    tel = {"actie": 0, "lezen": 0, "weg": 0}
    lijst = []
    for r in rijen:
        g = bram_web.groep(r)
        tel[g] += 1
        if g != "weg":
            lijst.append(dict(r, groep=g))
    # Miniweergave: eerst wat om actie vraagt, dan om te lezen; nieuwste eerst.
    tel["lijst"] = sorted(lijst, key=lambda r: (r["groep"] != "actie", -r["ontvangen"].timestamp()))[:8]
    return tel


def _meldingen_in_map(c, map_, limiet=50):
    volgorde = "gelezen, CASE niveau WHEN 'urgent' THEN 0 WHEN 'belangrijk' THEN 1 ELSE 2 END, id DESC" if map_ == "open" \
        else "coalesce(afgehandeld, tijd) DESC, id DESC"
    return c.execute(f"SELECT * FROM meldingen WHERE status = %s ORDER BY {volgorde} LIMIT %s", (map_, limiet)).fetchall()


@app.get("/", response_class=HTMLResponse)
def vandaag(request: Request):
    nu = dt.datetime.now(config.TZ)
    # De meldingen staan op Vandaag, met de mappen (Open, Opgelost, Archief, Prullenbak) eronder.
    meldingmap = request.query_params.get("meldingen", "open")
    if meldingmap not in {m[0] for m in meldingen.MAPPEN}:
        meldingmap = "open"
    with db.pool.connection() as c:
        open_ = meldingen.open_meldingen(c, 12)
        lijst_meldingen = _meldingen_in_map(c, meldingmap)
        aantallen = meldingen.aantallen(c)
        agenda = clara_vandaag(c, nu)
        post = bram_vandaag(c)
        taken = noor.vandaag_overzicht(c, nu)
        stromen, _ = vera_nieuws.verzamel(c, 2)
        luister = jesse_luisteren.voor_vandaag(c)
        koppen = vera_nieuws.koppen_per_soort(stromen, nu, per={"keepers": 4, "politiek": 3, "landelijk": 2, "overig": 4})
    urgent = [m for m in open_ if m["niveau"] == "urgent"]
    return templates.TemplateResponse("vandaag.html", ctx(
        request, "vandaag", nu=nu, meldingen=open_, urgent=urgent, werkruimtes=config.werkruimtes(), agenda=agenda,
        lijst_meldingen=lijst_meldingen, meldingmap=meldingmap, meldingmappen=meldingen.MAPPEN, aantallen=aantallen,
        post=post, taken=taken, koppen=koppen, luister=luister, socialstromen=[x for x in jesse_luisteren.STROMEN if x[0] in luister], categorieen=vera_nieuws.CATEGORIEEN, deelnamen=vera_nieuws.DEEL_NAMEN,
        taakmelding=request.query_params.get("melding", "")[:300], DAGEN=web.DAGEN))


VERNIEUW_TAKEN = ("agenda", "post", "nieuws", "socials")  # wat de knop Vernieuwen op Vandaag meteen ophaalt


@app.post("/vandaag/vernieuwen")
def vandaag_vernieuwen(request: Request):
    """Mail, agenda's, nieuws en socials nu ophalen (op de achtergrond); de pagina wacht met /api/vernieuwen tot het klaar is."""
    controleer_herkomst(request)
    gestart = [n for n in VERNIEUW_TAKEN if n in planner.TAKEN]
    for naam in gestart:
        planner.nu_draaien(db.achtergrond, naam)
    if request.headers.get("x-vandaag") == "1":
        return JSONResponse({"gestart": gestart})
    return RedirectResponse("/", status_code=303)


def vernieuw_stand(rijen, sinds, namen):
    """Per taak: klaar (na 'sinds' afgerond) of nog bezig. Pure functie."""
    per = {r["naam"]: r for r in rijen}
    uit = {}
    for n in namen:
        r = per.get(n)
        uit[n] = "bezig" if r and r["bezig_sinds"] else ("klaar" if r and r["laatst"] and r["laatst"] >= sinds else "wacht")
    return uit


@app.get("/api/vernieuwen")
def vernieuwen_stand(sinds: float = 0):
    """Hoe ver het vernieuwen is: per taak klaar, bezig of wacht (nog niet begonnen)."""
    moment = dt.datetime.fromtimestamp(sinds, dt.timezone.utc) if sinds else dt.datetime.now(dt.timezone.utc)
    namen = [n for n in VERNIEUW_TAKEN if n in planner.TAKEN]
    with db.pool.connection() as c:
        rijen = c.execute("SELECT naam, laatst, bezig_sinds FROM taken WHERE naam = ANY(%s)", (namen,)).fetchall()
    return JSONResponse({"taken": vernieuw_stand(rijen, moment, namen)})


# ---------- Iris ----------

@app.get("/iris", response_class=HTMLResponse)
def iris_scherm(request: Request, g: int | None = None):
    if g is None:  # zonder keuze: het laatste gesprek
        gesprek = q1("SELECT * FROM gesprekken WHERE persona = 'iris' AND NOT gearchiveerd ORDER BY bijgewerkt DESC LIMIT 1")
    else:  # g=0 is een nieuw, leeg gesprek
        gesprek = q1("SELECT * FROM gesprekken WHERE id = %s AND persona = 'iris'", (g,)) if g else None
    berichten = q("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gesprek["id"],)) if gesprek else []
    eerdere = q("SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'iris' AND NOT gearchiveerd ORDER BY bijgewerkt DESC LIMIT 15")
    archief = q("SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'iris' AND gearchiveerd ORDER BY bijgewerkt DESC LIMIT 30")
    return templates.TemplateResponse("iris.html", ctx(
        request, "iris", gesprek=gesprek, berichten=berichten, eerdere=eerdere, archief=archief, p=personas.get("iris"),
        sleutel_ok=bool(config.ANTHROPIC_API_KEY)))


@app.post("/iris")
async def iris_vraag(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    vraag = str(form.get("vraag", "")).strip()
    gid = int(form.get("gesprek") or 0) or None
    if not vraag:
        return RedirectResponse("/iris" + (f"?g={gid}" if gid else ""), status_code=303)
    if not gid:
        gid = q1("INSERT INTO gesprekken (persona, titel) VALUES ('iris', %s) RETURNING id", (" ".join(vraag.split())[:80],))["id"]
    uitvoeren("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (gid, vraag))
    uitvoeren("UPDATE gesprekken SET gearchiveerd = false WHERE id = %s", (gid,))

    def antwoorden():
        with db.pool.connection() as c:
            try:
                iris.antwoord(c, gid)
            except claude.ClaudeFout as e:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)",
                          (gid, f"(Ik kan nu niet antwoorden: {e})"))
    await asyncio.to_thread(antwoorden)
    return RedirectResponse(f"/iris?g={gid}#laatste", status_code=303)


def _terug_gesprek(form, persona):
    terug = str(form.get("terug") or f"/{persona}")
    return terug if terug.startswith(("/clara", "/iris")) and "//" not in terug else f"/{persona}"


@app.post("/gesprek/{gid}/archief")
async def gesprek_archief(request: Request, gid: int):
    """Gesprek archiveren, of uit het archief terughalen."""
    controleer_herkomst(request)
    form = await request.form()
    g = q1("UPDATE gesprekken SET gearchiveerd = NOT gearchiveerd WHERE id = %s RETURNING persona, gearchiveerd", (gid,))
    if not g:
        raise HTTPException(404)
    terug = _terug_gesprek(form, g["persona"])
    return RedirectResponse(terug if g["gearchiveerd"] else f"/{g['persona']}?g={gid}", status_code=303)


@app.post("/gesprek/{gid}/weg")
async def gesprek_weg(request: Request, gid: int):
    """Gesprek definitief verwijderen, met alle berichten. Open voorstellen uit dat gesprek vervallen."""
    controleer_herkomst(request)
    form = await request.form()
    g = q1("SELECT persona FROM gesprekken WHERE id = %s", (gid,))
    if not g:
        raise HTTPException(404)
    uitvoeren("UPDATE agenda_voorstellen SET status = 'afgewezen' WHERE gesprek_id = %s AND status = 'open'", (gid,))
    uitvoeren("DELETE FROM gesprekken WHERE id = %s", (gid,))
    return RedirectResponse(_terug_gesprek(form, g["persona"]), status_code=303)


@app.post("/iris/nieuw")
def iris_nieuw(request: Request):
    controleer_herkomst(request)
    return RedirectResponse("/iris?g=0", status_code=303)


# ---------- Meldingen ----------

BRONNAMEN = {"clara": "Clara (agenda)", "bram": "Bram (post)", "noor": "Noor (taken)", "vera": "Vera (nieuws)",
             "jesse": "Jesse (socials)", "iris": "Iris", "aankondiging": "Aankondigingen", "gezondheid": "Server",
             "politiek": "Politiek", "zerohero": "ZEROHERO"}
MELDINGEN_PER_PAGINA = 100
BULK_ACTIES = ("gelezen", "ongelezen", *meldingen.ACTIES)


def _bronnaam(b):
    return BRONNAMEN.get(b, (b or "").replace("_", " ").capitalize())


def _meldingfilter(bron):
    """Uit de vraag of het formulier: map, bron, niveau, zoekterm, alleen ongelezen."""
    mappen = {m[0] for m in meldingen.MAPPEN}
    f = {"map": str(bron.get("map") or "open"), "bron": str(bron.get("bron") or "")[:40], "niveau": str(bron.get("niveau") or ""),
         "q": str(bron.get("q") or "").strip()[:100], "ongelezen": "1" if str(bron.get("ongelezen") or "") == "1" else ""}
    f["map"] = f["map"] if f["map"] in mappen else "open"
    f["niveau"] = f["niveau"] if f["niveau"] in meldingen.NIVEAUS else ""
    return f


def _meldingwaar(f):
    delen, args = ["status = %s"], [f["map"]]
    if f["bron"]:
        delen.append("bron = %s")
        args.append(f["bron"])
    if f["niveau"]:
        delen.append("niveau = %s")
        args.append(f["niveau"])
    if f["q"]:
        delen.append("(titel ILIKE %s OR tekst ILIKE %s)")
        args += [f"%{f['q']}%"] * 2
    if f["ongelezen"]:
        delen.append("NOT gelezen")
    return " AND ".join(delen), args


def _meldingadres(f, **extra):
    return "/meldingen?" + urlencode({k: v for k, v in (f | extra).items() if v and not (k == "map" and v == "open")})


@app.get("/meldingen", response_class=HTMLResponse)
def meldingen_scherm(request: Request, pagina: int = 1, melding: str = ""):
    """Alle meldingen beheren: per map, filteren op bron, niveau en tekst, en meerdere tegelijk afhandelen."""
    f = _meldingfilter(request.query_params)
    pagina = max(1, pagina)
    waar, args = _meldingwaar(f)
    volgorde = "gelezen, CASE niveau WHEN 'urgent' THEN 0 WHEN 'belangrijk' THEN 1 ELSE 2 END, id DESC" if f["map"] == "open" \
        else "coalesce(afgehandeld, tijd) DESC, id DESC"
    with db.pool.connection() as c:
        totaal = c.execute(f"SELECT count(*) AS n FROM meldingen WHERE {waar}", args).fetchone()["n"]
        lijst = c.execute(f"SELECT * FROM meldingen WHERE {waar} ORDER BY {volgorde} LIMIT %s OFFSET %s",
                          (*args, MELDINGEN_PER_PAGINA, (pagina - 1) * MELDINGEN_PER_PAGINA)).fetchall()
        bronnen = c.execute("""SELECT bron, count(*) FILTER (WHERE status = %s) AS n, count(*) AS totaal,
                                      count(*) FILTER (WHERE status = 'open' AND NOT gelezen) AS ongelezen
                               FROM meldingen WHERE status <> 'gewist' GROUP BY bron ORDER BY bron""", (f["map"],)).fetchall()
        aantallen = meldingen.aantallen(c)
        stil = meldingen.stil(c)
    return templates.TemplateResponse("meldingen.html", ctx(
        request, "vandaag", lijst=lijst, f=f, totaal=totaal, pagina=pagina, meer=pagina * MELDINGEN_PER_PAGINA < totaal,
        bronnen=bronnen, bronnaam=_bronnaam, stil=stil, aantallen=aantallen, meldingmappen=meldingen.MAPPEN,
        niveaus=meldingen.NIVEAUS, adres=lambda **x: _meldingadres(f, **x), melding=melding,
        telegram=bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)))


@app.post("/meldingen/bulk")
async def meldingen_bulk(request: Request):
    """Meerdere meldingen tegelijk: de aangevinkte, of (alle=1) alles wat aan het filter voldoet."""
    controleer_herkomst(request)
    form = await request.form()
    f = _meldingfilter(form)
    actie = str(form.get("actie", ""))
    if actie not in BULK_ACTIES:
        raise HTTPException(400)
    with db.pool.connection() as c:
        if form.get("alle") == "1":
            waar, args = _meldingwaar(f)
            ids = [r["id"] for r in c.execute(f"SELECT id FROM meldingen WHERE {waar}", args).fetchall()]
        else:
            ids = [int(i) for i in form.getlist("id") if str(i).isdigit()]
        n = meldingen.afhandelen_veel(c, ids, actie)
    wat = {"gelezen": "als gelezen gemarkeerd", "ongelezen": "als ongelezen gemarkeerd", "opgelost": "afgevinkt",
           "archief": "gearchiveerd", "verwijder": "naar de prullenbak", "terug": "teruggezet naar Open"}[actie]
    tekst = f"{n} melding{'en' if n != 1 else ''} {wat}." if n else ("Vink eerst meldingen aan." if not ids else "Er veranderde niets.")
    return RedirectResponse(_meldingadres(f, melding=tekst), status_code=303)


@app.post("/meldingen/legen")
def meldingen_legen(request: Request):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        n = meldingen.prullenbak_legen(c)
    return RedirectResponse(_meldingadres({"map": "verwijderd"}, melding=f"Prullenbak geleegd ({n})."), status_code=303)


@app.post("/meldingen/stil")
async def meldingen_stil(request: Request):
    """Per bron: verschijnen de meldingen op je scherm (banner en bureaublad) of alleen in de lijst."""
    controleer_herkomst(request)
    form = await request.form()
    with db.pool.connection() as c:
        alle = {r["bron"] for r in c.execute("SELECT DISTINCT bron FROM meldingen").fetchall()}
        meldingen.stil_opslaan(c, {b for b in alle if form.get(f"stil_{b}") == "1"})
    return RedirectResponse(_meldingadres(_meldingfilter(form), melding="Opgeslagen.") + "#bronnen", status_code=303)


def _terug_meldingen(request):
    terug = request.headers.get("referer") or "/#meldingen"
    return RedirectResponse(terug if web.herkomst_toegestaan(None, terug, config.TOEGESTANE_HOSTS) else "/", status_code=303)


@app.post("/meldingen/{mid}/gelezen")
def melding_gelezen(request: Request, mid: int):
    controleer_herkomst(request)
    uitvoeren("UPDATE meldingen SET gelezen = true WHERE id = %s", (mid,))
    return _terug_meldingen(request)


@app.post("/meldingen/{mid}/{actie}")
def melding_afhandelen(request: Request, mid: int, actie: str):
    """Afvinken (opgelost), archiveren, verwijderen (naar de prullenbak) of terugzetten naar Open."""
    controleer_herkomst(request)
    if actie not in meldingen.ACTIES:
        raise HTTPException(404)
    with db.pool.connection() as c:
        if not meldingen.afhandelen(c, mid, actie):
            raise HTTPException(404)
    return _terug_meldingen(request)


@app.post("/meldingen/alles-gelezen")
def meldingen_alles_gelezen(request: Request):
    controleer_herkomst(request)
    uitvoeren("UPDATE meldingen SET gelezen = true WHERE NOT gelezen")
    return RedirectResponse("/#meldingen", status_code=303)


@app.get("/meldingen/aankondigingen", response_class=HTMLResponse)
def aankondigingen_scherm(request: Request, melding: str = ""):
    with db.pool.connection() as c:
        inst = aankondigen.instellingen(c)
        recent = c.execute("""SELECT titel, tijd FROM meldingen WHERE bron = 'aankondiging' ORDER BY id DESC LIMIT 8""").fetchall()
    return templates.TemplateResponse("aankondigingen.html", ctx(
        request, "vandaag", inst=inst, keuzes=aankondigen.KEUZE_MINUTEN, recent=recent, melding=melding))


@app.post("/meldingen/aankondigingen")
async def aankondigingen_opslaan(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    try:
        minuten = int(f.get("minuten") or 15)
    except ValueError:
        minuten = 15
    ochtend = str(f.get("ochtend") or "08:00")[:5]
    try:
        dt.time.fromisoformat(ochtend)
    except ValueError:
        ochtend = "08:00"
    waarden = {"vergaderingen": f.get("vergaderingen") == "1", "taken": f.get("taken") == "1", "deadlines": f.get("deadlines") == "1",
               "minuten": minuten if minuten in aankondigen.KEUZE_MINUTEN else 15, "ochtend": ochtend}
    with db.pool.connection() as c:
        aankondigen.opslaan(c, waarden)
    return RedirectResponse("/meldingen/aankondigingen?melding=Opgeslagen.", status_code=303)


@app.get("/api/meldingen")
def meldingen_nieuw(na: int = 0):
    """Voor het dashboard: nieuwe meldingen sinds id 'na', voor de banner en de systeemmelding op je scherm."""
    with db.pool.connection() as c:
        nieuw = meldingen.nieuw_sinds(c, na)
        hoogste = c.execute("SELECT coalesce(max(id), 0) AS m FROM meldingen").fetchone()["m"]
    return JSONResponse({"hoogste": hoogste, "nieuw": [dict(m) for m in nieuw]})


# ---------- Gezondheid ----------

@app.get("/gezondheid", response_class=HTMLResponse)
def gezondheid_scherm(request: Request):
    with db.pool.connection() as c:
        besteed, aantal = claude.verbruik_maand(c)
        per_persona = c.execute("""SELECT persona, count(*) AS aantal, sum(kosten_eur) AS eur FROM claude_gebruik
                                   WHERE tijd >= date_trunc('month', now()) GROUP BY persona ORDER BY 3 DESC""").fetchall()
        taken = {t["naam"]: t for t in c.execute("SELECT * FROM taken").fetchall()}
    pools = snelheid.pools(schermen=db.pool, achtergrond=db.achtergrond, politiek=getattr(politiek, "pool", None),
                           zerohero=getattr(zerohero, "pool", None))
    return templates.TemplateResponse("gezondheid.html", ctx(
        request, "gezondheid", geheugen=gezondheid.geheugen(), schijf=gezondheid.schijf(), belasting=gezondheid.belasting(),
        backups=gezondheid.backups(), extern=gezondheid.extern(), gb=gezondheid.gb, grenzen=gezondheid, besteed=besteed, aantal=aantal,
        per_persona=per_persona, plafond=config.MAANDPLAFOND_EUR, taken=taken, geregistreerd=planner.TAKEN,
        snel=snelheid.overzicht(), pools=pools, bezig=planner.bezig(),
        sleutel_ok=bool(config.ANTHROPIC_API_KEY), telegram_ok=bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)))


@app.post("/gezondheid/taak/{naam}")
def taak_nu(request: Request, naam: str):
    controleer_herkomst(request)
    if naam not in planner.TAKEN:
        raise HTTPException(404)
    planner.nu_draaien(db.achtergrond, naam)
    return RedirectResponse("/gezondheid", status_code=303)


@app.get("/gezond", response_class=PlainTextResponse)
def gezond():
    q1("SELECT 1")
    return "ok"
