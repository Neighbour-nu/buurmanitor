"""Schermen voor Signal (onder Post): koppelen, chats kiezen, meelezen en de voorstellen uitvoeren."""
import asyncio
import datetime as dt
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..clara import sync as clara_sync, web as clara_web
from ..kern import config, db, planner
from ..kern.web import controleer_herkomst, ctx, templates
from . import bron, verwerken

router = APIRouter()
DOMEINEN = config.DOMEINEN


def _terug(pad, **params):
    params = {k: v for k, v in params.items() if v}
    pad, _, anker = pad.partition("#")
    return RedirectResponse(pad + (("&" if "?" in pad else "?") + urlencode(params) if params else "") + (f"#{anker}" if anker else ""),
                            status_code=303)


def chatnaam(ch):
    return ch.get("eigen_naam") or ch.get("naam") or ch.get("sleutel", "").split(":", 1)[-1][:12] or "onbekend"


@router.get("/bram/signal", response_class=HTMLResponse)
def scherm(request: Request, melding: str = "", fout: str = "", alle: int = 0):
    with db.pool.connection() as c:
        nummer = verwerken.instelling(c, "signal_nummer")
        bereikbaar = True
        if not nummer and bron.actief():  # misschien net gescand: kijk bij de container
            try:
                nummer = verwerken.nummer(c)
            except bron.SignalFout as e:
                bereikbaar, fout = False, fout or str(e)
        chats = c.execute("""SELECT ch.*, (SELECT count(*) FROM signal_berichten b WHERE b.chat_id = ch.id) AS bewaard
                             FROM signal_chats ch ORDER BY ch.meelezen DESC, ch.laatst DESC NULLS LAST LIMIT %s""",
                          (500 if alle else 60,)).fetchall()
        totaal = c.execute("SELECT count(*) AS n FROM signal_chats").fetchone()["n"]
        voorstellen = c.execute("""SELECT v.*, ch.naam, ch.eigen_naam, ch.sleutel, ch.domein FROM signal_voorstellen v
                                   JOIN signal_chats ch ON ch.id = v.chat_id WHERE v.status = 'open' ORDER BY v.datum NULLS LAST, v.id""").fetchall()
        taak = c.execute("SELECT * FROM taken WHERE naam = 'signal'").fetchone()
        agendas = clara_sync.koppelingen(c, alleen_actief=True)
    doelen = clara_web._schrijfdoelen(agendas)  # pylint: disable=protected-access
    return templates.TemplateResponse("bram_signal.html", ctx(
        request, "bram", actief=bron.actief(), bereikbaar=bereikbaar, nummer=nummer, chats=chats, totaal=totaal, alle=alle, voorstellen=voorstellen, taak=taak,
        doelen=doelen, doel_voor=clara_web._doel_voor, chatnaam=chatnaam, domeinen=DOMEINEN,  # pylint: disable=protected-access
        sleutel_ok=bool(config.ANTHROPIC_API_KEY), planner_aan=config.PLANNER_AAN, tz=config.TZ, melding=melding, fout=fout))


@router.get("/bram/signal/qr")
async def qr(request: Request):
    """De QR-code om te koppelen (komt van de Signal-container, wordt niet bewaard)."""
    try:
        png = await asyncio.to_thread(bron.qr_code, "Buurmanitor")
    except bron.SignalFout as e:
        raise HTTPException(502, str(e)) from e
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/bram/signal/gekoppeld")
async def gekoppeld(request: Request):
    controleer_herkomst(request)

    def doe():
        with db.pool.connection() as c:
            n = verwerken.nummer(c)
            if n:
                verwerken.groepnamen(c, n, vers=True)
            return n
    try:
        n = await asyncio.to_thread(doe)
    except bron.SignalFout as e:
        return _terug("/bram/signal", fout=str(e))
    if not n:
        return _terug("/bram/signal", fout="Nog geen koppeling gevonden. Scan de QR-code in Signal (Instellingen > Gekoppelde apparaten) en probeer het opnieuw.")
    return _terug("/bram/signal", melding=f"Gekoppeld met {n}. Over een paar minuten verschijnen hier je chats: kies daarna waar Bram mag meelezen.")


@router.post("/bram/signal/ophalen")
async def ophalen(request: Request):
    controleer_herkomst(request)
    uitslag = await asyncio.to_thread(planner.draai, db.achtergrond, "signal", True)
    return _terug("/bram/signal", melding=uitslag or planner.BEZIG)


@router.post("/bram/signal/vergeten")
async def vergeten(request: Request):
    """Alles van Signal uit Buurmanitor wissen (chats, berichten, voorstellen). Ontkoppelen doe je in Signal op je telefoon."""
    controleer_herkomst(request)
    with db.pool.connection() as c:
        c.execute("DELETE FROM signal_chats")
        c.execute("DELETE FROM instellingen WHERE sleutel IN ('signal_nummer', 'signal_groepen', 'signal_laatst')")
    return _terug("/bram/signal", melding="Alles van Signal is uit Buurmanitor gewist. Haal Buurmanitor nu ook weg in Signal op je telefoon "
                                          "(Instellingen > Gekoppelde apparaten), anders komen er nieuwe berichten binnen.")


@router.post("/bram/signal/chat/{cid:int}")
async def chat_opslaan(request: Request, cid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        ch = c.execute("SELECT * FROM signal_chats WHERE id = %s", (cid,)).fetchone()
        if not ch:
            raise HTTPException(404)
        meelezen = bool(f.get("meelezen"))
        domein = str(f.get("domein") or ch["domein"])
        c.execute("""UPDATE signal_chats SET meelezen = %s, voorstellen = %s, domein = %s, eigen_naam = %s,
                     geanalyseerd_tot = CASE WHEN %s AND NOT meelezen THEN (SELECT coalesce(max(id), 0) FROM signal_berichten) ELSE geanalyseerd_tot END
                     WHERE id = %s""",
                  (meelezen, bool(f.get("voorstellen")), domein if domein in dict(DOMEINEN) else "prive", str(f.get("eigen_naam") or "").strip()[:80],
                   meelezen, cid))
        gewist = 0
        if ch["meelezen"] and not meelezen:  # uitgezet: wat er van deze chat bewaard was, gaat weg
            gewist = c.execute("DELETE FROM signal_berichten WHERE chat_id = %s", (cid,)).rowcount
            c.execute("DELETE FROM signal_voorstellen WHERE chat_id = %s AND status = 'open'", (cid,))
    naam = chatnaam(ch | {"eigen_naam": str(f.get("eigen_naam") or "").strip()})
    if meelezen and not ch["meelezen"]:
        melding = f"Bram leest vanaf nu mee in {naam} (alleen nieuwe berichten)."
    elif gewist or (ch["meelezen"] and not meelezen):
        melding = f"Bram leest niet meer mee in {naam}; de {gewist} bewaarde berichten zijn gewist."
    else:
        melding = "Opgeslagen."
    return _terug(f"/bram/signal#chat-{cid}", melding=melding)


@router.get("/bram/signal/chat/{cid:int}", response_class=HTMLResponse)
def chat_scherm(request: Request, cid: int):
    with db.pool.connection() as c:
        ch = c.execute("SELECT * FROM signal_chats WHERE id = %s", (cid,)).fetchone()
        if not ch:
            raise HTTPException(404)
        berichten = c.execute("SELECT * FROM signal_berichten WHERE chat_id = %s ORDER BY id DESC LIMIT 300", (cid,)).fetchall()[::-1]
    return templates.TemplateResponse("bram_signal_chat.html", ctx(request, "bram", ch=ch, berichten=berichten, chatnaam=chatnaam, tz=config.TZ))


@router.post("/bram/signal/voorstel/{vid:int}")
async def voorstel(request: Request, vid: int):
    """Een voorstel uitvoeren (na je klik): als taak op het takenbord, of als afspraak in de gekozen agenda."""
    controleer_herkomst(request)
    f = await request.form()
    actie = str(f.get("actie") or "")
    with db.pool.connection() as c:
        v = c.execute("""SELECT v.*, ch.naam, ch.eigen_naam, ch.sleutel FROM signal_voorstellen v JOIN signal_chats ch ON ch.id = v.chat_id
                         WHERE v.id = %s AND v.status = 'open'""", (vid,)).fetchone()
        if not v:
            return _terug("/bram/signal", fout="Dit voorstel is al afgehandeld.")
        if actie == "weg":
            c.execute("UPDATE signal_voorstellen SET status = 'weg' WHERE id = %s", (vid,))
            return _terug("/bram/signal#voorstellen")
    titel = str(f.get("titel") or v["titel"]).strip()[:160] or v["titel"]
    datum = verwerken._datum(f.get("datum")) if f.get("datum") else v["datum"]  # pylint: disable=protected-access
    begin = verwerken._tijd(f.get("begintijd")) if f.get("begintijd") else v["begintijd"]  # pylint: disable=protected-access
    eind = verwerken._tijd(f.get("eindtijd")) if f.get("eindtijd") else v["eindtijd"]  # pylint: disable=protected-access
    uitleg = f"Uit Signal ({chatnaam(v)}): {v['toelichting']}".strip()

    def doe():
        from ..taken import web as taken_web  # pylint: disable=import-outside-toplevel
        with db.pool.connection() as c:
            if actie == "taak":
                tid = taken_web.nieuw(c, {"titel": titel, "beschrijving": uitleg, "prio": "middel", "status": "nieuw", "wacht_op": "",
                                          "startdatum": None, "starttijd": None, "einddatum": datum, "eindtijd": None, "doel": "",
                                          "project_id": None})
                if datum:
                    taken_web.agenda_op_achtergrond([tid])
                tekst = f"'{titel}' staat op het takenbord" + (f" (deadline {datum:%d-%m})" if datum else "") + "."
            elif actie == "afspraak":
                if not datum:
                    raise ValueError("Geef de afspraak een datum.")
                heledag = begin is None
                start = dt.datetime.combine(datum, begin or dt.time(0), tzinfo=config.TZ)
                einde = (dt.datetime.combine(datum, eind, tzinfo=config.TZ) if eind and begin and eind > begin
                         else start + (dt.timedelta(days=1) if heledag else dt.timedelta(hours=1)))
                clara_web._maak(c, str(f.get("doel") or ""), titel, start, einde, uitleg, locatie=v["locatie"], heledag=heledag)  # pylint: disable=protected-access
                tekst = f"'{titel}' staat in je agenda op {start:%d-%m}" + ("" if heledag else f" om {start:%H.%M}") + "."
            else:
                raise ValueError("Onbekende actie.")
            c.execute("UPDATE signal_voorstellen SET status = 'gedaan', uitslag = %s WHERE id = %s", (tekst, vid))
            return tekst
    try:
        tekst = await asyncio.to_thread(doe)
    except (ValueError, clara_web.bronnen.BronFout) as e:
        return _terug("/bram/signal#voorstellen", fout=str(e)[:300])
    return _terug("/bram/signal#voorstellen", melding=tekst)
