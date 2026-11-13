"""Jesse op het scherm: Socials (wat er op de socials gezegd wordt, per stroom, zoals Nieuws) en het bronnenbeheer."""
import asyncio
import datetime as dt
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import personas
from ..kern import config, db, planner
from ..kern.web import controleer_herkomst, ctx, templates
from . import luisteren

router = APIRouter()
TABS = (("alles", "Alles"),) + luisteren.STROMEN
PERIODES = ((1, "24 uur"), (3, "3 dagen"), (7, "Week"))


def _terug(pad, **melding):
    pad, _, anker = pad.partition("#")
    if melding:
        pad += ("&" if "?" in pad else "?") + urlencode(melding)
    return RedirectResponse(pad + (f"#{anker}" if anker else ""), status_code=303)


def _veilig_terug(f, standaard="/jesse"):
    terug = str(f.get("terug") or standaard)
    return terug if terug.startswith("/jesse") and "//" not in terug else standaard


@router.get("/jesse", response_class=HTMLResponse)
def scherm(request: Request, stroom: str = "alles", dagen: int = 1, alles: int = 0):
    stroom = stroom if stroom in luisteren.STROOM_NAMEN else "alles"
    dagen = dagen if dagen in dict(PERIODES) else 1
    nu = dt.datetime.now(config.TZ)
    with db.pool.connection() as c:
        per = luisteren.stromen(c, max(dagen, 2))
        stand = c.execute("SELECT laatst, resultaat, fout FROM taken WHERE naam = 'socials'").fetchone()
        aantal_bronnen = c.execute("SELECT count(*) FILTER (WHERE actief) AS aan, count(*) FILTER (WHERE actief AND fout <> '') AS fout FROM jesse_bronnen").fetchone()
    grens = nu - dt.timedelta(days=dagen)
    kolommen = []
    for s, naam in luisteren.STROMEN:
        if stroom not in ("alles", s):
            continue
        lijst = [b for b in per.get(s, []) if b["tijd"] and b["tijd"] >= grens]
        getoond = luisteren.selectie(lijst, drempel=0 if alles else 40, aantal=40 if alles or stroom != "alles" else 8, dubbels=bool(alles))
        kolommen.append({"stroom": s, "naam": naam, "lijst": getoond, "totaal": len(lijst), "meer": max(0, len(lijst) - len(getoond)),
                         "ongewogen": any(b["score"] is None for b in getoond)})
    return templates.TemplateResponse("jesse.html", ctx(
        request, "jesse", p=personas.get("jesse"), kolommen=kolommen, stroom=stroom, dagen=dagen, alles=alles, tabs=TABS,
        periodes=PERIODES, stand=stand, bronnen=aantal_bronnen, sleutel_ok=bool(config.ANTHROPIC_API_KEY)))


@router.get("/jesse/lees/{bid:int}")
def lees(bid: int):
    """Doorklikken naar het bericht op het platform. Jesse onthoudt dat je het gelezen hebt."""
    with db.pool.connection() as c:
        url = luisteren.url_van(c, bid)
        if not url or urlsplit(url).scheme not in ("http", "https"):
            raise HTTPException(404, "Dit bericht bestaat niet meer.")
        c.execute("INSERT INTO jesse_gelezen (bericht) VALUES (%s) ON CONFLICT DO NOTHING", (bid,))
    return RedirectResponse(url, status_code=303)


@router.post("/jesse/gelezen/{bid:int}")
async def gelezen(request: Request, bid: int):
    """Als gelezen markeren (aan=1) of weer als ongelezen (aan=0). Met JavaScript zonder herladen."""
    controleer_herkomst(request)
    f = await request.form()
    aan = str(f.get("aan", "1")) != "0"
    with db.pool.connection() as c:
        if aan:
            c.execute("INSERT INTO jesse_gelezen (bericht) VALUES (%s) ON CONFLICT DO NOTHING", (bid,))
        else:
            c.execute("DELETE FROM jesse_gelezen WHERE bericht = %s", (bid,))
    if request.headers.get("x-jesse") == "1":
        return JSONResponse({"gelezen": aan})
    return _terug(_veilig_terug(f))


@router.post("/jesse/verberg/{bid:int}")
async def verberg(request: Request, bid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        c.execute("UPDATE jesse_berichten SET verborgen = true WHERE id = %s", (bid,))
    return _terug(_veilig_terug(f))


@router.post("/jesse/vernieuwen")
async def vernieuwen(request: Request):
    controleer_herkomst(request)

    uitslag = await asyncio.to_thread(planner.draai, db.achtergrond, "socials", True)  # onder het slot van de planner
    return _terug("/jesse/bronnen", melding=f"Opgehaald: {uitslag}." if uitslag else planner.BEZIG)


# ---------------------------------------------------------------- bronnen

@router.get("/jesse/bronnen", response_class=HTMLResponse)
def bronnen_scherm(request: Request, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        lijst = c.execute("""SELECT b.*, (SELECT count(*) FROM jesse_berichten m WHERE m.bron_id = b.id AND m.binnen > now() - interval '7 days') AS week
                             FROM jesse_bronnen b ORDER BY array_position(ARRAY['keepers', 'politiek', 'overig'], b.stroom), b.naam""").fetchall()
        stand = c.execute("SELECT laatst, resultaat, fout FROM taken WHERE naam = 'socials'").fetchone()
        reacties = c.execute("SELECT platform, count(*) AS n FROM jesse_berichten WHERE stroom = 'reacties' AND binnen > now() - interval '7 days' GROUP BY platform").fetchall()
        besteed = luisteren.vandaag_besteed(c)
    return templates.TemplateResponse("jesse_bronnen.html", ctx(
        request, "jesse", bronnen=lijst, melding=melding, fout=fout, stand=stand, reacties=reacties, besteed=besteed,
        plafond=luisteren.DAGPLAFOND_EUR, soorten=luisteren.SOORTEN, stromen=dict(luisteren.STROMEN), bron_stromen=luisteren.BRON_STROMEN,
        doellijn=config.ZEROHERO_ACTIEF, youtube=bool(luisteren.youtube_sleutel()), sleutel_ok=bool(config.ANTHROPIC_API_KEY)))


@router.post("/jesse/bronnen")
async def bron_toevoegen(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    naam, adres = str(f.get("naam") or "").strip()[:100], str(f.get("adres") or "").strip()[:500]
    soort, stroom = str(f.get("soort") or ""), str(f.get("stroom") or "")
    if soort not in luisteren.SOORTEN or stroom not in luisteren.BRON_STROMEN:
        return _terug("/jesse/bronnen", fout="Kies een soort bron en waar hij bij hoort.")
    if not naam or not adres:
        return _terug("/jesse/bronnen", fout="Geef de bron een naam en een adres, subreddit, hashtag of zoekopdracht.")
    if soort == "rss" and urlsplit(adres).scheme not in ("http", "https"):
        return _terug("/jesse/bronnen", fout="Een feed heeft een adres dat begint met https://.")
    with db.pool.connection() as c:
        c.execute("INSERT INTO jesse_bronnen (naam, soort, adres, stroom) VALUES (%s, %s, %s, %s)", (naam, soort, adres, stroom))
    return _terug("/jesse/bronnen", melding=f"Bron '{naam}' toegevoegd. Jesse luistert er binnen een kwartier naar.")


@router.post("/jesse/bronnen/{bid:int}/{actie}")
async def bron_actie(request: Request, bid: int, actie: str):
    controleer_herkomst(request)
    if actie not in ("aan", "uit", "weg"):
        raise HTTPException(404)
    with db.pool.connection() as c:
        if actie == "weg":
            c.execute("DELETE FROM jesse_bronnen WHERE id = %s", (bid,))
        else:
            c.execute("UPDATE jesse_bronnen SET actief = %s WHERE id = %s", (actie == "aan", bid))
    return _terug("/jesse/bronnen")
