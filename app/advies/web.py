"""Schermen voor Iris' adviezen: archief, één advies met feedback, nu een advies maken, en de instellingen (soorten,
achtergrond en wat Iris leerde)."""
import asyncio
import datetime as dt
import json
import re
import threading
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..kern import claude, config, db
from ..kern.web import controleer_herkomst, ctx, templates
from . import maken

router = APIRouter()
EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[a-z]{2,}$", re.I)


def _terug(pad, **params):
    params = {k: v for k, v in params.items() if v}
    pad, _, anker = pad.partition("#")
    return RedirectResponse(pad + (("&" if "?" in pad else "?") + urlencode(params) if params else "") + (f"#{anker}" if anker else ""),
                            status_code=303)


def _veilig_terug(waarde, standaard):
    waarde = str(waarde or "")
    return waarde if re.match(r"^/iris(/[\w/]*)?(\?[\w=&]*)?(#[\w-]*)?$", waarde) else standaard


def feedback_per_punt(c, aid):
    """{punt (None = geheel): laatste feedback} van een advies."""
    uit = {}
    for f in c.execute("SELECT * FROM iris_feedback WHERE advies_id = %s ORDER BY id", (aid,)).fetchall():
        uit[f["punt"]] = f
    return uit


def laatste(c):
    """Het nieuwste advies met zijn feedback, voor het Iris-scherm."""
    a = c.execute("SELECT * FROM iris_adviezen ORDER BY tijd DESC LIMIT 1").fetchone()
    return (a, feedback_per_punt(c, a["id"])) if a else (None, {})


@router.get("/iris/advies", response_class=HTMLResponse)
def archief(request: Request, melding: str = ""):
    with db.pool.connection() as c:
        lijst = c.execute("""SELECT a.*, (SELECT count(*) FROM iris_feedback f WHERE f.advies_id = a.id) AS feedback
                             FROM iris_adviezen a ORDER BY tijd DESC LIMIT 90""").fetchall()
    return templates.TemplateResponse("iris_adviezen.html", ctx(request, "iris", lijst=lijst, soorten_iconen=maken.SOORTEN, melding=melding,
                                                                tz=config.TZ))


@router.get("/iris/advies/{aid:int}", response_class=HTMLResponse)
def een_advies(request: Request, aid: int, melding: str = ""):
    with db.pool.connection() as c:
        a = c.execute("SELECT * FROM iris_adviezen WHERE id = %s", (aid,)).fetchone()
        if not a:
            raise HTTPException(404)
        fb = feedback_per_punt(c, aid)
    return templates.TemplateResponse("iris_advies.html", ctx(request, "iris", a=a, fb=fb, soorten_iconen=maken.SOORTEN, melding=melding,
                                                              tz=config.TZ))


@router.post("/iris/advies/maak")
async def nu_maken(request: Request):
    """Nu een advies maken (zonder te mailen). Iris doet er een halve minuut over."""
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            maken.standaard_maken(c)
            sid = int(f.get("soort")) if str(f.get("soort") or "").isdigit() else 0
            s = c.execute("SELECT * FROM iris_adviessoorten WHERE id = %s", (sid,)).fetchone() or \
                c.execute("SELECT * FROM iris_adviessoorten ORDER BY tijd LIMIT 1").fetchone()
            return maken.maak(c, s)
    try:
        a = await asyncio.to_thread(doe)
    except claude.ClaudeFout as e:
        return _terug("/iris", fout=f"Iris kon nu geen advies maken: {e}")
    return _terug("/iris#advies", melding=f"Iris heeft een {a['naam'].lower()} gemaakt.")


def _leren_op_achtergrond(fid):
    def werk():
        try:
            with db.achtergrond.connection() as c:
                maken.leer(c, fid)
        except Exception:  # noqa: BLE001  (leren is een extra; de feedback zelf is bewaard)
            pass
    threading.Thread(target=werk, name="iris-leren", daemon=True).start()


@router.post("/iris/advies/{aid:int}/feedback")
async def feedback(request: Request, aid: int):
    """Feedback op een punt (punt = nummer) of op het hele advies (punt leeg): nuttig, niet nuttig, en/of een opmerking.
    Nog eens op hetzelfde punt vervangt de vorige feedback."""
    controleer_herkomst(request)
    f = await request.form()
    punt = int(f["punt"]) if str(f.get("punt") or "").isdigit() else None
    oordeel = {"1": 1, "-1": -1}.get(str(f.get("oordeel") or ""), 0)
    opmerking = str(f.get("opmerking") or "").strip()[:1000]
    terug = _veilig_terug(f.get("terug"), f"/iris/advies/{aid}")
    with db.pool.connection() as c:
        a = c.execute("SELECT * FROM iris_adviezen WHERE id = %s", (aid,)).fetchone()
        if not a or (punt is not None and punt >= len(a["punten"] or [])):
            raise HTTPException(404)
        vorige = feedback_per_punt(c, aid).get(punt)
        if not oordeel and not opmerking:
            return _terug(terug)
        if vorige:  # een duim verandert het oordeel en houdt de opmerking; een opmerking houdt het oordeel
            fid = c.execute("""UPDATE iris_feedback SET oordeel = %s, opmerking = %s, verwerkt = false, tijd = now() WHERE id = %s RETURNING id""",
                            (oordeel or vorige["oordeel"], opmerking or vorige["opmerking"], vorige["id"])).fetchone()["id"]
        else:
            fid = c.execute("INSERT INTO iris_feedback (advies_id, punt, oordeel, opmerking) VALUES (%s, %s, %s, %s) RETURNING id",
                            (aid, punt, oordeel, opmerking)).fetchone()["id"]
        f2 = c.execute("SELECT * FROM iris_feedback WHERE id = %s", (fid,)).fetchone()
        if punt is None:
            c.execute("UPDATE iris_adviezen SET oordeel = %s, opmerking = %s WHERE id = %s", (f2["oordeel"] or None, f2["opmerking"], aid))
    if f2["opmerking"] or f2["oordeel"] < 0:
        _leren_op_achtergrond(fid)
        melding = "Dank je. Iris maakt er een les van; die zie je bij Instellingen onder Wat Iris leerde."
    else:
        melding = "Dank je, genoteerd."
    return _terug(terug, melding=melding)


# ---------------------------------------------------------------- instellingen

@router.get("/iris/instellingen", response_class=HTMLResponse)
def instellingen(request: Request, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        maken.standaard_maken(c)
        soorten = c.execute("SELECT * FROM iris_adviessoorten ORDER BY tijd, id").fetchall()
        lessen = c.execute("""SELECT l.*, f.opmerking AS fb_opmerking FROM iris_lessen l LEFT JOIN iris_feedback f ON f.id = l.feedback_id
                              ORDER BY l.actief DESC, l.id DESC""").fetchall()
        taak = c.execute("SELECT * FROM taken WHERE naam = 'irisadvies'").fetchone()
        achter = maken.achtergrond(c)
    return templates.TemplateResponse("iris_instellingen.html", ctx(
        request, "iris", soorten=soorten, lessen=lessen, achtergrond=achter, bronnen=maken.BRONNEN, lengtes=maken.LENGTES,
        dagen_kort=["ma", "di", "wo", "do", "vr", "za", "zo"], taak=taak, planner_aan=config.PLANNER_AAN, melding=melding, fout=fout))


def _soort_uit_formulier(f):
    try:
        u, m = (int(x) for x in str(f.get("tijd") or "07:00").replace(".", ":").split(":")[:2])
        tijd = dt.time(u, m)
    except ValueError:
        return None, "Die tijd begrijp ik niet; gebruik bijvoorbeeld 07:00."
    adres = str(f.get("mail_aan") or "").strip()
    if adres and not EMAIL.match(adres):
        return None, "Dat mailadres klopt niet."
    if f.get("mail") and not adres:
        return None, "Vul het mailadres in, of zet mailen uit."
    v = {"naam": str(f.get("naam") or "").strip()[:80] or "Advies", "actief": bool(f.get("actief")), "tijd": tijd,
         "dagen": [d for d in range(7) if f.get(f"dag_{d}")], "opdracht": str(f.get("opdracht") or "").strip()[:3000],
         "bronnen": json.dumps([b for b in maken.BRONNEN if f.get(f"bron_{b}")]),
         "horizon": max(1, min(60, int(f.get("horizon")) if str(f.get("horizon") or "").isdigit() else 7)),
         "lengte": f.get("lengte") if f.get("lengte") in maken.LENGTES else "kort", "mail": bool(f.get("mail")), "mail_aan": adres,
         "telegram": bool(f.get("telegram"))}
    if not v["opdracht"]:
        return None, "Geef Iris een opdracht: waar moet het advies over gaan?"
    return v, ""


@router.post("/iris/instellingen/soort/{sid:int}")
async def soort_opslaan(request: Request, sid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        if not c.execute("SELECT 1 FROM iris_adviessoorten WHERE id = %s", (sid,)).fetchone():
            raise HTTPException(404)
        if f.get("actie") == "weg":
            c.execute("DELETE FROM iris_adviessoorten WHERE id = %s", (sid,))
            return _terug("/iris/instellingen", melding="Verwijderd. Eerdere adviezen blijven in het archief.")
        v, fout = _soort_uit_formulier(f)
        if fout:
            return _terug(f"/iris/instellingen#soort-{sid}", fout=fout)
        c.execute("""UPDATE iris_adviessoorten SET naam = %(naam)s, actief = %(actief)s, tijd = %(tijd)s, dagen = %(dagen)s,
                     opdracht = %(opdracht)s, bronnen = %(bronnen)s, horizon = %(horizon)s, lengte = %(lengte)s, mail = %(mail)s,
                     mail_aan = %(mail_aan)s, telegram = %(telegram)s WHERE id = %(id)s""", v | {"id": sid})
    return _terug(f"/iris/instellingen#soort-{sid}", melding="Opgeslagen.")


@router.post("/iris/instellingen/soort/nieuw")
async def soort_nieuw(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    v, fout = _soort_uit_formulier(f)
    if fout:
        return _terug("/iris/instellingen#nieuw", fout=fout)
    with db.pool.connection() as c:
        sid = c.execute("""INSERT INTO iris_adviessoorten (naam, actief, tijd, dagen, opdracht, bronnen, horizon, lengte, mail, mail_aan, telegram)
                           VALUES (%(naam)s, %(actief)s, %(tijd)s, %(dagen)s, %(opdracht)s, %(bronnen)s, %(horizon)s, %(lengte)s, %(mail)s,
                                   %(mail_aan)s, %(telegram)s) RETURNING id""", v).fetchone()["id"]
    return _terug(f"/iris/instellingen#soort-{sid}", melding=f"'{v['naam']}' toegevoegd.")


@router.post("/iris/instellingen/achtergrond")
async def achtergrond_opslaan(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        c.execute("""INSERT INTO instellingen (sleutel, waarde) VALUES ('iris_achtergrond', %s)
                     ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()""",
                  (str(f.get("achtergrond") or "").strip()[:4000],))
    return _terug("/iris/instellingen#achtergrond", melding="Achtergrond opgeslagen.")


@router.post("/iris/lessen")
async def les_nieuw(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    tekst = str(f.get("tekst") or "").strip()[:300]
    if tekst:
        with db.pool.connection() as c:
            c.execute("INSERT INTO iris_lessen (tekst, bron) VALUES (%s, 'zelf')", (tekst,))
    return _terug("/iris/instellingen#lessen", melding="Les toegevoegd." if tekst else "")


@router.post("/iris/les/{lid:int}")
async def les_bijwerken(request: Request, lid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        if not c.execute("SELECT 1 FROM iris_lessen WHERE id = %s", (lid,)).fetchone():
            raise HTTPException(404)
        if f.get("actie") == "weg":
            c.execute("DELETE FROM iris_lessen WHERE id = %s", (lid,))
            return _terug("/iris/instellingen#lessen", melding="Les verwijderd.")
        tekst = str(f.get("tekst") or "").strip()[:300]
        if not tekst:
            return _terug("/iris/instellingen#lessen", fout="Een les zonder tekst kan niet; verwijder hem dan.")
        c.execute("UPDATE iris_lessen SET tekst = %s, actief = %s, bijgewerkt = now() WHERE id = %s", (tekst, bool(f.get("actief")), lid))
    return _terug("/iris/instellingen#lessen", melding="Les opgeslagen.")
