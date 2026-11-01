"""Jesse op het scherm: Socials (cijfers per kanaal, posts, wat klaarstaat, vermeldingen) en het gesprek met Jesse."""
import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import personas
from ..kern import claude, config, db
from ..kern.web import controleer_herkomst, ctx, templates
from . import gesprek, socials

router = APIRouter()
PERIODES = ((7, "7 dagen"), (30, "30 dagen"), (90, "90 dagen"))


def _terug(pad, **melding):
    pad, _, anker = pad.partition("#")
    if melding:
        pad += ("&" if "?" in pad else "?") + urlencode(melding)
    return RedirectResponse(pad + (f"#{anker}" if anker else ""), status_code=303)


def _paneel(c, g=None):
    if g:
        gs = c.execute("SELECT * FROM gesprekken WHERE id = %s AND persona = 'jesse'", (g,)).fetchone()
    elif g is None:
        gs = c.execute("SELECT * FROM gesprekken WHERE persona = 'jesse' AND NOT gearchiveerd ORDER BY bijgewerkt DESC LIMIT 1").fetchone()
    else:
        gs = None
    uit = {"gesprek": gs, "berichten": [], "voorstellen": [],
           "eerdere": c.execute("""SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'jesse' AND NOT gearchiveerd
                                   ORDER BY bijgewerkt DESC LIMIT 10""").fetchall(),
           "bewaard": c.execute("SELECT * FROM jesse_voorstellen WHERE status = 'bewaard' ORDER BY id DESC LIMIT 20").fetchall()}
    if gs:
        uit["berichten"] = c.execute("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gs["id"],)).fetchall()
        uit["voorstellen"] = c.execute("SELECT * FROM jesse_voorstellen WHERE gesprek_id = %s AND status = 'open' ORDER BY id", (gs["id"],)).fetchall()
    return uit


@router.get("/jesse", response_class=HTMLResponse)
def scherm(request: Request, dagen: int = 7, praat: int = 0, g: int | None = None, melding: str = ""):
    dagen = dagen if dagen in dict(PERIODES) else 7
    o = socials.overzicht(dagen)
    with db.pool.connection() as c:
        paneel = _paneel(c, g)
    return templates.TemplateResponse("jesse.html", ctx(
        request, "jesse", p=personas.get("jesse"), o=o, dagen=dagen, periodes=PERIODES, paneel=paneel, praat=praat, melding=melding,
        kanaal_namen=gesprek.KANAAL_NAMEN, voor_namen=gesprek.VOOR, sleutel_ok=bool(config.ANTHROPIC_API_KEY)))


def _gesprek_adres(gid):
    return "/jesse" + (f"?g={gid}" if gid else "") + "#jesse"


@router.post("/jesse/gesprek/vraag")
async def vraag(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    tekst = str(f.get("vraag") or "").strip()
    gid = int(f.get("gesprek") or 0) or None
    if not tekst:
        return _terug(_gesprek_adres(gid))
    with db.pool.connection() as c:
        if gid and not c.execute("SELECT 1 FROM gesprekken WHERE id = %s AND persona = 'jesse'", (gid,)).fetchone():
            gid = None
        if not gid:
            gid = c.execute("INSERT INTO gesprekken (persona, titel, kader) VALUES ('jesse', %s, 'socials') RETURNING id",
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
    return _terug(_gesprek_adres(gid))


@router.post("/jesse/gesprek/nieuw")
async def nieuw(request: Request):
    controleer_herkomst(request)
    return _terug("/jesse?g=0#jesse")


@router.post("/jesse/voorstel/{vid:int}/{actie}")
async def voorstel(request: Request, vid: int, actie: str):
    """Een postvoorstel bewaren (voor later), wegdoen, of de tekst aanpassen. Jesse post nooit zelf."""
    controleer_herkomst(request)
    if actie not in ("bewaar", "weg", "tekst"):
        raise HTTPException(404)
    f = await request.form()
    with db.pool.connection() as c:
        v = c.execute("SELECT * FROM jesse_voorstellen WHERE id = %s", (vid,)).fetchone()
        if not v:
            raise HTTPException(404)
        if actie == "tekst":
            c.execute("UPDATE jesse_voorstellen SET tekst = %s WHERE id = %s", (str(f.get("tekst") or v["tekst"]).strip()[:5000], vid))
        else:
            c.execute("UPDATE jesse_voorstellen SET status = %s WHERE id = %s", ("bewaard" if actie == "bewaar" else "weg", vid))
    if request.headers.get("x-jesse") == "1":
        return JSONResponse({"ok": True})
    return _terug(_gesprek_adres(v["gesprek_id"]))
