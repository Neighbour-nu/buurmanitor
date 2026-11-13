"""Schermen: de lijst met overzichten, een overzicht instellen (onderdelen, opmaak, tijd, ontvangers), een voorbeeld en
versturen (proef naar jezelf, of nu naar iedereen)."""
import asyncio
import datetime as dt
import json
import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..kern import config, db
from ..kern.web import DAGEN, controleer_herkomst, ctx, templates
from . import onderdelen as od, opmaak, verzenden

router = APIRouter()
WERKRUIMTES = {"politiek": "Promonitor (politiek)", "zerohero": "ZEROHERO"}
ROLLEN = {"eigen": "Ikzelf", "lid": "Fractielid of ander"}
EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[a-z]{2,}$", re.I)
KLEUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _terug(pad, **params):
    params = {k: v for k, v in params.items() if v}
    pad, _, anker = pad.partition("#")
    return RedirectResponse(pad + (("&" if "?" in pad else "?") + urlencode(params) if params else "") + (f"#{anker}" if anker else ""),
                            status_code=303)


def _overzicht(c, oid):
    o = c.execute("SELECT * FROM overzichten WHERE id = %s", (oid,)).fetchone()
    if not o:
        raise HTTPException(404)
    return o


def _personen():
    """Fractieleden uit Promonitor (voor de eigen deadlines per ontvanger). Leeg als Promonitor hier niet draait."""
    if not config.POLITIEK_ACTIEF:
        return []
    try:
        with od._politiek_pool().connection() as pc:  # pylint: disable=protected-access
            return pc.execute("""SELECT naam, whaller, rol FROM personen WHERE actief AND coalesce(whaller, '') <> ''
                                 ORDER BY volgorde, naam""").fetchall()
    except Exception:  # noqa: BLE001
        return []


def dagen_tekst(dagen):
    """'elke dag', 'werkdagen', 'ma, wo, vr'. Pure functie."""
    dagen = sorted(dagen or [])
    if dagen == list(range(7)):
        return "elke dag"
    if dagen == list(range(5)):
        return "werkdagen"
    return ", ".join(od.DAGEN_KORT[d] for d in dagen) or "geen dagen"


@router.get("/overzichten", response_class=HTMLResponse)
def lijst(request: Request, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        verzenden.standaard_maken(c)
        rijen = c.execute("""SELECT o.*, (SELECT count(*) FROM overzicht_ontvangers r WHERE r.overzicht_id = o.id AND r.actief) AS ontvangers
                             FROM overzichten o ORDER BY werkruimte, naam, id""").fetchall()
        taak = c.execute("SELECT * FROM taken WHERE naam = 'overzichten'").fetchone()
    return templates.TemplateResponse("overzichten.html", ctx(
        request, "overzichten", lijst=rijen, soorten=WERKRUIMTES, dagen_tekst=dagen_tekst,
        melding=melding, fout=fout, taak=taak, planner_aan=config.PLANNER_AAN))


@router.post("/overzichten/nieuw")
async def maak(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    naam, werkruimte = str(f.get("naam") or "").strip()[:120], str(f.get("werkruimte") or "")
    if not naam or werkruimte not in WERKRUIMTES:
        return _terug("/overzichten", fout="Geef het overzicht een naam en kies Promonitor of ZEROHERO.")
    with db.pool.connection() as c:
        oid = verzenden.nieuw(c, naam, werkruimte, kop=naam.upper()[:40], kleur="#FF0000" if werkruimte == "politiek" else "#14427E",
                              afzender="Promonitor" if werkruimte == "politiek" else "ZEROHERO")
    return _terug(f"/overzichten/{oid}", melding="Overzicht gemaakt. Het staat nog uit: kijk de onderdelen na en voeg ontvangers toe.")


@router.get("/overzichten/{oid:int}", response_class=HTMLResponse)
def scherm(request: Request, oid: int, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        o = _overzicht(c, oid)
        ontvangers = c.execute("SELECT * FROM overzicht_ontvangers WHERE overzicht_id = %s ORDER BY rol = 'eigen' DESC, naam, id",
                               (oid,)).fetchall()
        log = c.execute("SELECT * FROM overzicht_verzonden WHERE overzicht_id = %s ORDER BY tijd DESC LIMIT 12", (oid,)).fetchall()
    onderdelen = [(inst, od.ONDERDELEN[inst["code"]]) for inst in od.aangevuld(o["werkruimte"], o["onderdelen"] or [])]
    personen = _personen() if o["werkruimte"] == "politiek" else []
    return templates.TemplateResponse("overzicht.html", ctx(
        request, "overzichten", o=o, onderdelen=onderdelen, optie=od.optie, ontvangers=ontvangers, log=log, personen=personen,
        soorten=WERKRUIMTES, rollen=ROLLEN, DAGEN=DAGEN, dagen_kort=od.DAGEN_KORT, dagen_tekst=dagen_tekst, melding=melding,
        fout=fout, tz=config.TZ, planner_aan=config.PLANNER_AAN))


def _getal(waarde, standaard, laag, hoog):
    try:
        return max(laag, min(hoog, int(str(waarde).strip())))
    except (TypeError, ValueError):
        return standaard


def onderdelen_uit_formulier(f, werkruimte, bestaand):
    """De onderdelen zoals ingevuld, in de volgorde van het formulier, met verschuiven (verplaats = 'index:op' of 'index:neer')."""
    uit = []
    for i, inst in enumerate(od.aangevuld(werkruimte, bestaand)):
        code = str(f.get(f"code_{i}") or inst["code"])
        defn = od.ONDERDELEN.get(code)
        if not defn:
            continue
        opties = {}
        for sleutel, _label, soort, standaard in defn.opties:
            veld = f"opt_{i}_{sleutel}"
            opties[sleutel] = bool(f.get(veld)) if soort == "vink" else _getal(f.get(veld), standaard, 0, 999)
        uit.append({"code": code, "aan": bool(f.get(f"aan_{i}")), "titel": str(f.get(f"titel_{i}") or defn.titel).strip()[:80],
                    "max": _getal(f.get(f"max_{i}"), defn.max, 1, 30), "opties": opties})
    verplaats = str(f.get("verplaats") or "")
    if ":" in verplaats:
        nr, richting = verplaats.split(":", 1)
        i = int(nr) if nr.isdigit() else -1
        j = i - 1 if richting == "op" else i + 1
        if 0 <= i < len(uit) and 0 <= j < len(uit):
            uit[i], uit[j] = uit[j], uit[i]
    return uit


@router.post("/overzichten/{oid:int}")
async def opslaan(request: Request, oid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        o = _overzicht(c, oid)
        try:
            u, m = (int(x) for x in str(f.get("tijd") or "08:00").replace(".", ":").split(":")[:2])
            tijd = dt.time(u, m)
        except ValueError:
            return _terug(f"/overzichten/{oid}", fout="Die tijd begrijp ik niet; gebruik bijvoorbeeld 07:30.")
        antwoord = str(f.get("antwoord_naar") or "").strip()
        if antwoord and not EMAIL.match(antwoord):
            return _terug(f"/overzichten/{oid}", fout="Het adres bij 'Antwoorden naar' is geen geldig mailadres.")
        kleur = str(f.get("kleur") or "")
        onderdelen = onderdelen_uit_formulier(f, o["werkruimte"], o["onderdelen"] or [])
        c.execute("""UPDATE overzichten SET naam = %s, actief = %s, tijd = %s, dagen = %s, onderwerp = %s, kop = %s, intro = %s,
                       afsluiting = %s, kleur = %s, afzender = %s, antwoord_naar = %s, telegram = %s, leeg_versturen = %s,
                       onderdelen = %s, bijgewerkt = now() WHERE id = %s""",
                  (str(f.get("naam") or o["naam"]).strip()[:120] or o["naam"], bool(f.get("actief")), tijd,
                   [d for d in range(7) if f.get(f"dag_{d}")], str(f.get("onderwerp") or "").strip()[:200],
                   str(f.get("kop") or "").strip()[:60], str(f.get("intro") or "").strip()[:2000],
                   str(f.get("afsluiting") or "").strip()[:2000], kleur if KLEUR.match(kleur) else o["kleur"],
                   str(f.get("afzender") or "").strip()[:80], antwoord, bool(f.get("telegram")), bool(f.get("leeg_versturen")),
                   json.dumps(onderdelen), oid))
        try:
            verzenden.politiek_beheer_bijwerken(c)
        except Exception:  # noqa: BLE001  (Promonitor even niet bereikbaar: bij de volgende keer opslaan opnieuw)
            pass
    anker = "#onderdelen" if f.get("verplaats") else ""
    return _terug(f"/overzichten/{oid}{anker}", melding="" if f.get("verplaats") else "Opgeslagen.")


@router.post("/overzichten/{oid:int}/ontvangers")
async def ontvanger_toevoegen(request: Request, oid: int):
    controleer_herkomst(request)
    f = await request.form()
    email = str(f.get("email") or "").strip().lower()
    if not EMAIL.match(email):
        return _terug(f"/overzichten/{oid}#ontvangers", fout="Vul een geldig mailadres in.")
    rol = f.get("rol") if f.get("rol") in ROLLEN else "lid"
    persoon = str(f.get("persoon") or "").strip()
    naam = str(f.get("naam") or "").strip()[:120]
    if not naam and persoon:
        naam = next((p["naam"] for p in _personen() if p["whaller"] == persoon), "")
    with db.pool.connection() as c:
        _overzicht(c, oid)
        r = c.execute("""INSERT INTO overzicht_ontvangers (overzicht_id, naam, email, rol, persoon) VALUES (%s, %s, %s, %s, %s)
                         ON CONFLICT DO NOTHING RETURNING id""", (oid, naam, email, rol, persoon)).fetchone()
    if not r:
        return _terug(f"/overzichten/{oid}#ontvangers", fout=f"{email} staat al bij de ontvangers.")
    return _terug(f"/overzichten/{oid}#ontvangers", melding=f"{naam or email} toegevoegd. Bekijk bij Voorbeeld hoe de mail er voor hem of haar uitziet.")


@router.post("/overzichten/ontvanger/{rid:int}")
async def ontvanger_bijwerken(request: Request, rid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        r = c.execute("SELECT * FROM overzicht_ontvangers WHERE id = %s", (rid,)).fetchone()
        if not r:
            raise HTTPException(404)
        if f.get("actie") == "weg":
            c.execute("DELETE FROM overzicht_ontvangers WHERE id = %s", (rid,))
            return _terug(f"/overzichten/{r['overzicht_id']}#ontvangers", melding=f"{r['naam'] or r['email']} verwijderd.")
        email = str(f.get("email") or r["email"]).strip().lower()
        if not EMAIL.match(email):
            return _terug(f"/overzichten/{r['overzicht_id']}#ontvangers", fout="Vul een geldig mailadres in.")
        try:
            c.execute("UPDATE overzicht_ontvangers SET naam = %s, email = %s, rol = %s, persoon = %s, actief = %s WHERE id = %s",
                      (str(f.get("naam") or "").strip()[:120], email, f.get("rol") if f.get("rol") in ROLLEN else r["rol"],
                       str(f.get("persoon") or "").strip(), bool(f.get("actief")), rid))
        except Exception:  # noqa: BLE001  (hetzelfde adres staat er al)
            return _terug(f"/overzichten/{r['overzicht_id']}#ontvangers", fout=f"{email} staat al bij de ontvangers.")
    return _terug(f"/overzichten/{r['overzicht_id']}#ontvangers", melding="Ontvanger opgeslagen.")


@router.get("/overzichten/{oid:int}/voorbeeld", response_class=HTMLResponse)
async def voorbeeld(oid: int, ontvanger: str = "eigen", claude: int = 0):
    """De mail zoals een ontvanger hem krijgt (in het kader op het scherm). Claude schrijft alleen mee als je daarom vraagt."""
    def maak():
        with db.pool.connection() as c:
            o = _overzicht(c, oid)
            r = c.execute("SELECT * FROM overzicht_ontvangers WHERE id = %s AND overzicht_id = %s",
                          (int(ontvanger), oid)).fetchone() if ontvanger.isdigit() else None
            r = r or {"rol": "lid" if ontvanger == "lid" else "eigen", "naam": "", "persoon": "", "email": ""}
            return verzenden.bekijken(c, o, r, met_claude=bool(claude))
    onderwerp, html = await asyncio.to_thread(maak)
    kop = f'<base target="_blank"><title>{opmaak.e(onderwerp)}</title>'
    return HTMLResponse(html.replace("<head>", "<head>" + kop, 1), headers={"Cache-Control": "no-store"})


@router.post("/overzichten/{oid:int}/stuur")
async def stuur(request: Request, oid: int):
    controleer_herkomst(request)
    f = await request.form()
    proef = f.get("wat") != "iedereen"

    def doe():
        with db.pool.connection() as c:
            return verzenden.versturen(c, _overzicht(c, oid), proef=proef)
    uitslag = await asyncio.to_thread(doe)
    return _terug(f"/overzichten/{oid}", melding=("Proef: " if proef else "Verstuurd: ") + uitslag)


@router.post("/overzichten/{oid:int}/kopie")
async def kopie(request: Request, oid: int):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        o = _overzicht(c, oid)
        nid = c.execute("""INSERT INTO overzichten (naam, werkruimte, actief, tijd, dagen, onderwerp, kop, intro, afsluiting, kleur, afzender,
                                                    antwoord_naar, telegram, leeg_versturen, onderdelen)
                           SELECT naam || ' (kopie)', werkruimte, false, tijd, dagen, onderwerp, kop, intro, afsluiting, kleur, afzender,
                                  antwoord_naar, false, leeg_versturen, onderdelen FROM overzichten WHERE id = %s RETURNING id""",
                        (o["id"],)).fetchone()["id"]
    return _terug(f"/overzichten/{nid}", melding="Kopie gemaakt (zonder ontvangers, en nog uit).")


@router.post("/overzichten/{oid:int}/weg")
async def weg(request: Request, oid: int):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        o = _overzicht(c, oid)
        c.execute("DELETE FROM overzichten WHERE id = %s", (oid,))
        try:
            verzenden.politiek_beheer_bijwerken(c)
        except Exception:  # noqa: BLE001
            pass
    return _terug("/overzichten", melding=f"'{o['naam']}' verwijderd.")
