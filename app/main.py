"""Buurmanitor: de persoonlijke portal van Martijn Buurman.

De kern: inloggen via Caddy en Authelia, meldingen, de planner, de gezondheid van de server en gesprekken met Iris.
Werkruimtes: Promonitor (fase 1) onder /politiek en de Doellijn (fase 2) onder /zerohero, elk één op één.
"""
import asyncio
import datetime as dt
import os
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import iris, personas
from .clara import sync as clara_sync, web as clara_web  # noqa: F401  (sync registreert de planner-taak)
from .kern import claude, config, db, gezondheid, meldingen, planner, prefix, web
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
    with db.pool.connection() as c:
        db.migreer(c, MIGRATIES)
        planner.opruimen(c)
    lus = asyncio.create_task(planner.lus(db.pool)) if config.PLANNER_AAN else None
    async with AsyncExitStack() as werkruimtes:
        # Gekoppelde apps krijgen hun eigen opstart (database, migraties, planner) niet vanzelf; die starten we hier.
        if politiek:
            await werkruimtes.enter_async_context(politiek.lifespan(politiek.app))
        if zerohero:
            await werkruimtes.enter_async_context(zerohero.lifespan(zerohero.app))
        yield
    if lus:
        lus.cancel()
    db.pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(web.BASIS, "static")), name="static")
app.middleware("http")(web.alleen_via_caddy)
app.include_router(clara_web.router)
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


@app.get("/", response_class=HTMLResponse)
def vandaag(request: Request):
    nu = dt.datetime.now(config.TZ)
    with db.pool.connection() as c:
        open_ = meldingen.open_meldingen(c, 12)
        besteed, _ = claude.verbruik_maand(c)
        agenda = clara_vandaag(c, nu)
    urgent = [m for m in open_ if m["niveau"] == "urgent"]
    g, s = gezondheid.geheugen(), gezondheid.schijf()
    return templates.TemplateResponse("vandaag.html", ctx(
        request, "vandaag", nu=nu, meldingen=open_, urgent=urgent, werkruimtes=config.werkruimtes(), agenda=agenda,
        team=personas.alle().values(), geheugen=g, schijf=s, backups=gezondheid.backups(), besteed=besteed,
        plafond=config.MAANDPLAFOND_EUR, fase=config.FASE))


# ---------- Iris ----------

@app.get("/iris", response_class=HTMLResponse)
def iris_scherm(request: Request, g: int | None = None):
    if g is None:  # zonder keuze: het laatste gesprek
        gesprek = q1("SELECT * FROM gesprekken WHERE persona = 'iris' ORDER BY bijgewerkt DESC LIMIT 1")
    else:  # g=0 is een nieuw, leeg gesprek
        gesprek = q1("SELECT * FROM gesprekken WHERE id = %s AND persona = 'iris'", (g,)) if g else None
    berichten = q("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gesprek["id"],)) if gesprek else []
    eerdere = q("SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'iris' ORDER BY bijgewerkt DESC LIMIT 15")
    return templates.TemplateResponse("iris.html", ctx(
        request, "iris", gesprek=gesprek, berichten=berichten, eerdere=eerdere, p=personas.get("iris"),
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

    def antwoorden():
        with db.pool.connection() as c:
            try:
                iris.antwoord(c, gid)
            except claude.ClaudeFout as e:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)",
                          (gid, f"(Ik kan nu niet antwoorden: {e})"))
    await asyncio.to_thread(antwoorden)
    return RedirectResponse(f"/iris?g={gid}#laatste", status_code=303)


@app.post("/iris/nieuw")
def iris_nieuw(request: Request):
    controleer_herkomst(request)
    return RedirectResponse("/iris?g=0", status_code=303)


# ---------- Meldingen ----------

@app.get("/meldingen", response_class=HTMLResponse)
def meldingen_scherm(request: Request):
    alle = q("SELECT * FROM meldingen ORDER BY id DESC LIMIT 100")
    return templates.TemplateResponse("meldingen.html", ctx(request, "meldingen", alle=alle))


@app.post("/meldingen/{mid}/gelezen")
def melding_gelezen(request: Request, mid: int):
    controleer_herkomst(request)
    uitvoeren("UPDATE meldingen SET gelezen = true WHERE id = %s", (mid,))
    terug = request.headers.get("referer") or "/meldingen"
    return RedirectResponse(terug if web.herkomst_toegestaan(None, terug, config.TOEGESTANE_HOSTS) else "/", status_code=303)


@app.post("/meldingen/alles-gelezen")
def meldingen_alles_gelezen(request: Request):
    controleer_herkomst(request)
    uitvoeren("UPDATE meldingen SET gelezen = true WHERE NOT gelezen")
    return RedirectResponse("/meldingen", status_code=303)


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
    return templates.TemplateResponse("gezondheid.html", ctx(
        request, "gezondheid", geheugen=gezondheid.geheugen(), schijf=gezondheid.schijf(), belasting=gezondheid.belasting(),
        backups=gezondheid.backups(), extern=gezondheid.extern(), gb=gezondheid.gb, grenzen=gezondheid, besteed=besteed, aantal=aantal,
        per_persona=per_persona, plafond=config.MAANDPLAFOND_EUR, taken=taken, geregistreerd=planner.TAKEN,
        sleutel_ok=bool(config.ANTHROPIC_API_KEY), telegram_ok=bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)))


@app.post("/gezondheid/taak/{naam}")
def taak_nu(request: Request, naam: str):
    controleer_herkomst(request)
    if naam not in planner.TAKEN:
        raise HTTPException(404)
    planner.nu_draaien(db.pool, naam)
    return RedirectResponse("/gezondheid", status_code=303)


@app.get("/gezond", response_class=PlainTextResponse)
def gezond():
    q1("SELECT 1")
    return "ok"
