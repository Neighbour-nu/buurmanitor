"""Vera op het scherm: Nieuws (keepers, politiek en overig op één pagina) en het beheer van haar eigen bronnen."""
import asyncio
import datetime as dt
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import personas
import json

from ..kern import claude, config, db
from ..kern.web import controleer_herkomst, ctx, templates
from . import gesprek, nieuws

router = APIRouter()
TABS = (("alles", "Alles"), ("keepers", "Keepers"), ("politiek", "Politiek"), ("overig", "Overig"))
PERIODES = ((1, "24 uur"), (3, "3 dagen"), (7, "Week"))
WERKRUIMTE_LINK = {"keepers": ("/zerohero/inbox", "Alles in de Doellijn"), "politiek": ("/politiek/inbox?soort=nieuws", "Alles in Promonitor")}


def _terug(pad, **melding):
    pad, _, anker = pad.partition("#")
    if melding:
        pad += ("&" if "?" in pad else "?") + urlencode(melding)
    return RedirectResponse(pad + (f"#{anker}" if anker else ""), status_code=303)


@router.get("/vera", response_class=HTMLResponse)
def overzicht(request: Request, soort: str = "alles", dagen: int = 1, alles: int = 0):
    soort = soort if soort in nieuws.SOORT_NAMEN or soort == "alles" else "alles"
    dagen = dagen if dagen in dict(PERIODES) else 1
    nu = dt.datetime.now(config.TZ)
    with db.pool.connection() as c:
        stromen, fouten = nieuws.verzamel(c, max(dagen, 2))
        bronnen = c.execute("SELECT count(*) FILTER (WHERE actief) AS aan, count(*) FILTER (WHERE actief AND fout <> '') AS fout FROM vera_bronnen").fetchone()
    grens = nu - dt.timedelta(days=dagen)
    for s in stromen:
        if stromen[s] is not None:
            stromen[s] = [b for b in stromen[s] if b["tijd"] and b["tijd"] >= grens]
    boven = nieuws.uitgelicht(stromen, nu) if soort == "alles" else []
    gekozen = {(b["soort"], b["id"]) for b in boven}
    aantal = 40 if alles or soort != "alles" else 10
    kolommen = []
    for s, naam in nieuws.SOORTEN:
        if soort not in ("alles", s):
            continue
        rest = [b for b in stromen[s] if (s, b["id"]) not in gekozen] if stromen[s] is not None else None
        # Ontdubbelen, ook tegen het uitgelichte bericht bovenaan; met 'alles laten zien' juist alles.
        lijst = nieuws.selectie(rest, drempel=0 if alles else 40, aantal=aantal, al=[b for b in boven if b["soort"] == s], dubbels=bool(alles))
        kolommen.append({"soort": s, "naam": naam, "lijst": lijst, "totaal": len(stromen[s] or []),
                         "meer": len(rest) - len(lijst) if lijst is not None else 0, "fout": fouten.get(s), "link": WERKRUIMTE_LINK.get(s),
                         "ongewogen": any(b["score"] is None for b in lijst or [])})
    return templates.TemplateResponse("vera.html", ctx(
        request, "vera", p=personas.get("vera"), kolommen=kolommen, uitgelicht=boven, soort=soort, dagen=dagen, alles=alles,
        periodes=PERIODES, soortnamen=nieuws.SOORT_NAMEN, tabs=TABS, bronnen=bronnen, nu=nu, sleutel_ok=bool(config.ANTHROPIC_API_KEY)))


@router.get("/vera/lees/{soort}/{bid:int}")
def lees(soort: str, bid: int):
    """Doorklikken naar het bericht. Vera onthoudt dat je het gelezen hebt."""
    if soort not in nieuws.SOORT_NAMEN:
        raise HTTPException(404)
    with db.pool.connection() as c:
        url = nieuws.url_van(c, soort, bid)
        if not url or urlsplit(url).scheme not in ("http", "https"):
            raise HTTPException(404, "Dit bericht bestaat niet meer.")
        c.execute("INSERT INTO vera_gelezen (soort, bericht) VALUES (%s, %s) ON CONFLICT DO NOTHING", (soort, bid))
    return RedirectResponse(url, status_code=303)


@router.post("/vera/verberg/{bid:int}")
async def verberg(request: Request, bid: int):
    """Een bericht uit 'overig nieuws' wegklikken."""
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        c.execute("UPDATE vera_berichten SET verborgen = true WHERE id = %s", (bid,))
    terug = str(f.get("terug") or "/vera")
    return _terug(terug if terug.startswith("/vera") and "//" not in terug else "/vera")


@router.post("/vera/vernieuwen")
async def vernieuwen(request: Request):
    controleer_herkomst(request)

    def doe():
        with db.pool.connection() as c:
            return nieuws.taak_nieuws(c)
    uitslag = await asyncio.to_thread(doe)
    return _terug("/vera/bronnen", melding=f"Opgehaald: {uitslag}.")


# ---------------------------------------------------------------- bronnen

@router.get("/vera/bronnen", response_class=HTMLResponse)
def bronnen_scherm(request: Request, melding: str = "", fout: str = "", g: int | None = None):
    with db.pool.connection() as c:
        paneel = gesprek.gesprek_paneel(c, g)
        lijst = c.execute("""SELECT b.*, (SELECT count(*) FROM vera_berichten m WHERE m.bron_id = b.id AND m.binnen > now() - interval '7 days') AS week
                             FROM vera_bronnen b ORDER BY b.rubriek, b.naam""").fetchall()
        besteed = nieuws.vandaag_besteed(c)
    return templates.TemplateResponse("vera_bronnen.html", ctx(
        request, "vera", bronnen=lijst, melding=melding, fout=fout, paneel=paneel, besteed=besteed, plafond=nieuws.DAGPLAFOND_EUR,
        sleutel_ok=bool(config.ANTHROPIC_API_KEY)))


@router.post("/vera/bronnen")
async def bron_toevoegen(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    naam, url = str(f.get("naam") or "").strip()[:100], str(f.get("url") or "").strip()[:500]
    soort = "google" if f.get("soort") == "google" else "feed"
    rubriek = str(f.get("rubriek") or "Algemeen").strip()[:40] or "Algemeen"
    if not naam or not url:
        return _terug("/vera/bronnen", fout="Geef de bron een naam en een adres of zoekopdracht.")
    if soort == "feed" and urlsplit(url).scheme not in ("http", "https"):
        return _terug("/vera/bronnen", fout="Een feed heeft een adres dat begint met https://.")
    with db.pool.connection() as c:
        c.execute("INSERT INTO vera_bronnen (naam, url, soort, rubriek) VALUES (%s, %s, %s, %s)", (naam, url, soort, rubriek))
    return _terug("/vera/bronnen", melding=f"Bron '{naam}' toegevoegd. Vera haalt hem binnen een kwartier op.")


@router.post("/vera/bronnen/{bid:int}/{actie}")
async def bron_actie(request: Request, bid: int, actie: str):
    controleer_herkomst(request)
    if actie not in ("aan", "uit", "weg"):
        raise HTTPException(404)
    with db.pool.connection() as c:
        if actie == "weg":
            c.execute("DELETE FROM vera_bronnen WHERE id = %s", (bid,))
        else:
            c.execute("UPDATE vera_bronnen SET actief = %s WHERE id = %s", (actie == "aan", bid))
    return _terug("/vera/bronnen")


# ---------------------------------------------------------------- gesprek met Vera over de bronnen

def _gesprek_adres(gid):
    return "/vera/bronnen" + (f"?g={gid}" if gid else "") + "#vera"


@router.post("/vera/gesprek/vraag")
async def vera_vraag(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    vraag = str(f.get("vraag") or "").strip()
    gid = int(f.get("gesprek") or 0) or None
    if not vraag:
        return _terug(_gesprek_adres(gid))
    with db.pool.connection() as c:
        if gid and not c.execute("SELECT 1 FROM gesprekken WHERE id = %s AND persona = 'vera'", (gid,)).fetchone():
            gid = None
        if not gid:
            gid = c.execute("INSERT INTO gesprekken (persona, titel, kader) VALUES ('vera', %s, 'bronnen') RETURNING id",
                            (" ".join(vraag.split())[:80],)).fetchone()["id"]
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (gid, vraag))
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


@router.post("/vera/gesprek/nieuw")
async def vera_nieuw(request: Request):
    controleer_herkomst(request)
    return _terug("/vera/bronnen?g=0#vera")


@router.post("/vera/voorstel/{vid:int}")
async def vera_voorstel(request: Request, vid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        v = c.execute("SELECT * FROM vera_voorstellen WHERE id = %s AND status = 'open'", (vid,)).fetchone()
        if not v:
            return _terug("/vera/bronnen#vera", fout="Dit voorstel is al afgehandeld.")
        if f.get("actie") == "weg":
            c.execute("UPDATE vera_voorstellen SET status = 'afgewezen' WHERE id = %s", (vid,))
            return _terug(_gesprek_adres(v["gesprek_id"]))
        bid, uitslag = gesprek.toepassen(c, v)
        c.execute("UPDATE vera_voorstellen SET status = 'uitgevoerd', uitslag = %s WHERE id = %s", (uitslag[:500], vid))
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (v["gesprek_id"], f"(Gedaan: {uitslag})"))
        c.execute("INSERT INTO logboek (persona, actie, details) VALUES ('vera', 'bron', %s)",
                  (json.dumps({"soort": v["soort"], "bron": bid, "uitslag": uitslag[:200]}),))
    return _terug(_gesprek_adres(v["gesprek_id"]), melding=uitslag)
