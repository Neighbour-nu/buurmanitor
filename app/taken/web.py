"""Taken: een kanbanbord met zes kolommen (Nieuw, Triage, Gepland, Actief, Wachtend op, Afgerond).

Een taak heeft een korte titel, een beschrijving, een prioriteit en eventueel een start- en een einddatum (met of
zonder tijd). Staat er een datum bij, dan zet Buurmanitor de taak als afspraak in de agenda (de eigen agenda van
Martijn), en past die aan of haalt hem weg als de taak verandert. Een taak kan ook uit een mail in Post komen.
"""
import asyncio
import datetime as dt
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from urllib.parse import urlencode

from ..clara import bronnen as agenda_bronnen, kluis, sync as agenda_sync, web as clara_web
from ..kern import claude, config, db
from ..kern.web import DAGEN, controleer_herkomst, ctx, templates
from . import noor

router = APIRouter()
STATUSSEN = (("nieuw", "Nieuw"), ("triage", "Triage"), ("gepland", "Gepland"), ("actief", "Actief"),
             ("wachtend", "Wachtend op"), ("afgerond", "Afgerond"))
STATUS_NAMEN = dict(STATUSSEN)
PRIO = (("hoog", "Hoog"), ("middel", "Middel"), ("laag", "Laag"))
PRIO_NAMEN = dict(PRIO)
MARKER = "[taak:"
AFGEROND_DAGEN = 30  # zo lang blijven afgeronde taken op het bord


def _terug(pad, **melding):
    pad, _, anker = pad.partition("#")
    if melding:
        pad += ("&" if "?" in pad else "?") + urlencode(melding)
    return RedirectResponse(pad + (f"#{anker}" if anker else ""), status_code=303)


def _datum(tekst):
    try:
        return dt.date.fromisoformat(str(tekst or "").strip()[:10]) if str(tekst or "").strip() else None
    except ValueError:
        return None


def _tijd(tekst):
    try:
        return dt.time.fromisoformat(str(tekst or "").strip()[:5]) if str(tekst or "").strip() else None
    except ValueError:
        return None


def afspraak(t, tz=config.TZ):
    """De afspraak die bij een taak hoort, of None als er geen datum is. Pure functie.
    Met een tijd: een gewone afspraak (bijvoorbeeld 9.00-12.00 aan het rapport werken). Zonder tijd: een afspraak voor
    de hele dag, of voor alle dagen van start tot en met einde. Alleen een einddatum: een deadline op die dag."""
    s, e = t.get("startdatum"), t.get("einddatum")
    if not s and not e:
        return None
    klaar = t.get("status") == "afgerond"
    teken = "✓ " if klaar else "📋 "
    if t.get("starttijd") or t.get("eindtijd"):
        dag = s or e
        begin_tijd = t.get("starttijd") or ((dt.datetime.combine(dag, t["eindtijd"]) - dt.timedelta(hours=1)).time()
                                            if t.get("eindtijd") else dt.time(9, 0))
        start = dt.datetime.combine(dag, begin_tijd, tzinfo=tz)
        eind = dt.datetime.combine(e or dag, t["eindtijd"], tzinfo=tz) if t.get("eindtijd") else start + dt.timedelta(hours=1)
        if eind <= start:
            eind = start + dt.timedelta(hours=1)
        return {"titel": teken + t["titel"], "start": start, "eind": eind, "heledag": False}
    if s and e:
        s, e = min(s, e), max(s, e)
        return {"titel": teken + t["titel"], "start": dt.datetime.combine(s, dt.time(0), tzinfo=tz),
                "eind": dt.datetime.combine(e + dt.timedelta(days=1), dt.time(0), tzinfo=tz), "heledag": True}
    if s:
        return {"titel": teken + t["titel"], "start": dt.datetime.combine(s, dt.time(0), tzinfo=tz),
                "eind": dt.datetime.combine(s + dt.timedelta(days=1), dt.time(0), tzinfo=tz), "heledag": True}
    return {"titel": ("✓ " if klaar else "⏰ Deadline: ") + t["titel"], "start": dt.datetime.combine(e, dt.time(0), tzinfo=tz),
            "eind": dt.datetime.combine(e + dt.timedelta(days=1), dt.time(0), tzinfo=tz), "heledag": True}


def _spec(a, doel):
    return json.dumps({"titel": a["titel"], "start": a["start"].isoformat(), "eind": a["eind"].isoformat(), "heledag": a["heledag"],
                       "doel": doel}, sort_keys=True) if a else ""


def _weg_uit_agenda(c, t):
    if not t["agenda_extern"]:
        return
    k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (t["agenda_koppeling"],)).fetchone()
    if k:
        try:
            (agenda_bronnen.google_verwijder if k["soort"] == "google" else agenda_bronnen.ms_verwijder)(
                agenda_sync.toegang(c, k), t["agenda_id"], t["agenda_extern"])
        except agenda_bronnen.NietGevonden:
            pass
    c.execute("UPDATE takenbord SET agenda_koppeling = NULL, agenda_id = '', agenda_extern = '', agenda_spec = '' WHERE id = %s", (t["id"],))


def agenda_bijwerken(c, tid):
    """De afspraak bij een taak maken, vernieuwen of weghalen. Fouten komen bij de taak te staan (de taak zelf blijft)."""
    t = c.execute("SELECT * FROM takenbord WHERE id = %s", (tid,)).fetchone()
    if not t:
        return ""
    a = afspraak(t)
    doelen = [w for w, _l, _d in clara_web._reisdoelen(agenda_sync.koppelingen(c, alleen_actief=True))]  # pylint: disable=protected-access
    doel = t["doel"] if t["doel"] in doelen else (doelen[0] if doelen else "")
    spec = _spec(a, doel)
    if spec == t["agenda_spec"] and (t["agenda_extern"] or not a):
        return ""
    try:
        _weg_uit_agenda(c, t)
        if not a:
            c.execute("UPDATE takenbord SET agenda_fout = '' WHERE id = %s", (tid,))
            return ""
        if not doel:
            c.execute("UPDATE takenbord SET agenda_fout = %s WHERE id = %s",
                      ("Er is geen eigen agenda met schrijfrechten om de taak in te zetten.", tid))
            return ""
        oms = (f"{MARKER}{tid}] Taak uit Buurmanitor ({STATUS_NAMEN[t['status']]}, prioriteit {PRIO_NAMEN[t['prio']].lower()})."
               + (f"\n\n{t['beschrijving']}" if t["beschrijving"] else ""))
        kid, extern = clara_web._maak(c, doel, a["titel"], a["start"], a["eind"], oms, bijwerken=False,  # pylint: disable=protected-access
                                      heledag=a["heledag"])
        c.execute("""UPDATE takenbord SET agenda_koppeling = %s, agenda_id = %s, agenda_extern = %s, agenda_spec = %s, agenda_fout = '', doel = %s
                     WHERE id = %s""", (kid, doel.split("|", 1)[1], extern, spec, doel, tid))
        agenda_sync.synchroniseer(c, c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (kid,)).fetchone(), forceer=True)
        return "in de agenda gezet"
    except (agenda_bronnen.BronFout, kluis.KluisFout) as e:
        c.execute("UPDATE takenbord SET agenda_fout = %s WHERE id = %s", (str(e)[:300], tid))
        return f"maar de agenda lukte niet: {e}"


def uit_formulier(f):
    """De velden van een taak uit het formulier, gecontroleerd. Pure functie."""
    status = str(f.get("status") or "nieuw")
    prio = str(f.get("prio") or "middel")
    return {"titel": str(f.get("titel") or "").strip()[:160], "beschrijving": str(f.get("beschrijving") or "").strip()[:8000],
            "prio": prio if prio in PRIO_NAMEN else "middel", "status": status if status in STATUS_NAMEN else "nieuw",
            "wacht_op": str(f.get("wacht_op") or "").strip()[:200],
            "startdatum": _datum(f.get("startdatum")), "starttijd": _tijd(f.get("starttijd")),
            "einddatum": _datum(f.get("einddatum")), "eindtijd": _tijd(f.get("eindtijd")), "doel": str(f.get("doel") or "")}


def uit_bericht(b):
    """Een voorstel voor een taak uit een mail in Post. Pure functie."""
    van = b.get("van_naam") or b.get("van_adres") or "onbekend"
    regel = b.get("regel") or ""
    return {"titel": (b.get("onderwerp") or "Mail")[:160], "prio": "hoog" if b.get("prioriteit") == "hoog" else "middel",
            "beschrijving": f"Uit de mail van {van}" + (f" ({b['van_adres']})" if b.get("van_adres") and b.get("van_naam") else "")
                            + f": {b.get('onderwerp') or ''}" + (f"\n\nBram: {regel}" if regel else "")
                            + (f"\n\n{b['fragment']}" if b.get("fragment") else ""),
            "bericht_id": b["id"]}


def nieuw(c, velden, bericht_id=None):
    return c.execute("""INSERT INTO takenbord (titel, beschrijving, prio, status, wacht_op, startdatum, starttijd, einddatum, eindtijd, doel,
                                           bericht_id, afgerond)
                        VALUES (%(titel)s, %(beschrijving)s, %(prio)s, %(status)s, %(wacht_op)s, %(startdatum)s, %(starttijd)s,
                                %(einddatum)s, %(eindtijd)s, %(doel)s, %(bericht_id)s,
                                CASE WHEN %(status)s = 'afgerond' THEN now() END) RETURNING id""",
                     {"wacht_op": "", "startdatum": None, "starttijd": None, "einddatum": None, "eindtijd": None, "doel": "",
                      "status": "nieuw", **velden, "bericht_id": bericht_id}).fetchone()["id"]


# ---------------------------------------------------------------- schermen

def _terug_adres(f, standaard):
    """Terug naar Vandaag als de knop daar stond, anders naar het bord. Pure functie."""
    return "/#taken" if str(f.get("terug") or "") == "/" else standaard


def _paneel(c, g=None):
    """Het gesprek met Noor: het gekozen gesprek, anders het laatste, met de open en uitgevoerde voorstellen."""
    if g:
        gs = c.execute("SELECT * FROM gesprekken WHERE id = %s AND persona = 'noor'", (g,)).fetchone()
    elif g is None:
        gs = c.execute("SELECT * FROM gesprekken WHERE persona = 'noor' AND NOT gearchiveerd ORDER BY bijgewerkt DESC LIMIT 1").fetchone()
    else:
        gs = None
    uit = {"gesprek": gs, "berichten": [], "voorstellen": [], "klaar": [],
           "eerdere": c.execute("""SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'noor' AND NOT gearchiveerd
                                   ORDER BY bijgewerkt DESC LIMIT 10""").fetchall()}
    if gs:
        uit["berichten"] = c.execute("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gs["id"],)).fetchall()
        rijen = c.execute("""SELECT v.*, t.titel AS taak_titel, t.startdatum, t.starttijd, t.einddatum, t.eindtijd FROM taak_voorstellen v
                             LEFT JOIN takenbord t ON t.id = v.taak_id WHERE v.gesprek_id = %s ORDER BY v.id""", (gs["id"],)).fetchall()
        for v in rijen:
            if v["status"] == "open":
                taak = {"titel": v["taak_titel"], "startdatum": v["startdatum"], "starttijd": v["starttijd"],
                        "einddatum": v["einddatum"], "eindtijd": v["eindtijd"]}
                uit["voorstellen"].append(dict(v, tekst=noor.beschrijf(v["soort"], noor.gegevens_terug(v["gegevens"]), taak)))
            elif v["status"] == "uitgevoerd":
                uit["klaar"].append(v)
    return uit


@router.get("/taken", response_class=HTMLResponse)
def bord(request: Request, bewerk: int = 0, nieuw_: int = 0, bericht: int = 0, wacht: int = 0, alles: int = 0,
         melding: str = "", fout: str = "", praat: int = 0, g: int | None = None):
    nieuw_ = nieuw_ or int(request.query_params.get("nieuw") or 0)
    vandaag = dt.datetime.now(config.TZ).date()
    with db.pool.connection() as c:
        taken = c.execute("""SELECT t.*, b.onderwerp AS mail_onderwerp, b.van_naam AS mail_van FROM takenbord t
                             LEFT JOIN post_berichten b ON b.id = t.bericht_id
                             WHERE t.status <> 'afgerond' OR %s OR t.afgerond > now() - make_interval(days => %s)
                             ORDER BY CASE t.prio WHEN 'hoog' THEN 0 WHEN 'middel' THEN 1 ELSE 2 END,
                                      coalesce(t.einddatum, t.startdatum, '9999-12-31'::date), t.aangemaakt""",
                          (bool(alles), AFGEROND_DAGEN)).fetchall()
        doelen = clara_web._reisdoelen(agenda_sync.koppelingen(c, alleen_actief=True))  # pylint: disable=protected-access
        formulier = None
        if bewerk:
            formulier = c.execute("SELECT * FROM takenbord WHERE id = %s", (bewerk,)).fetchone()
            if not formulier:
                raise HTTPException(404)
        elif nieuw_:
            formulier = {"id": 0, "titel": "", "beschrijving": "", "prio": "middel", "status": "nieuw", "wacht_op": "", "startdatum": None,
                         "starttijd": None, "einddatum": None, "eindtijd": None, "doel": "", "bericht_id": None, "agenda_fout": ""}
            if bericht:
                b = c.execute("SELECT * FROM post_berichten WHERE id = %s", (bericht,)).fetchone()
                if b:
                    formulier |= uit_bericht(b)
        mail = c.execute("SELECT id, onderwerp, van_naam FROM post_berichten WHERE id = %s", (formulier["bericht_id"],)).fetchone() \
            if formulier and formulier.get("bericht_id") else None
        paneel = _paneel(c, g) if praat else None
    kolommen = [(s, naam, [t for t in taken if t["status"] == s]) for s, naam in STATUSSEN]
    return templates.TemplateResponse("taken.html", ctx(
        request, "taken", kolommen=kolommen, formulier=formulier, statussen=STATUSSEN, prios=PRIO, prio_namen=PRIO_NAMEN,
        doelen=doelen, vandaag=vandaag, DAGEN=DAGEN, melding=melding, fout=fout, wacht=wacht, mail=mail, alles=alles,
        afgerond_dagen=AFGEROND_DAGEN, praat=praat, paneel=paneel, sleutel_ok=bool(config.ANTHROPIC_API_KEY)))


@router.post("/taken/nieuw")
async def taak_nieuw(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    velden = uit_formulier(f)
    if not velden["titel"]:
        return _terug(_terug_adres(f, "/taken?nieuw=1"), fout="Geef de taak een korte titel.")
    bericht_id = int(f.get("bericht_id")) if str(f.get("bericht_id") or "").isdigit() else None

    def doe():
        with db.pool.connection() as c:
            tid = nieuw(c, velden, bericht_id)
            return tid, agenda_bijwerken(c, tid)
    tid, agenda = await asyncio.to_thread(doe)
    return _terug(_terug_adres(f, f"/taken#taak-{tid}"), melding=f"Taak '{velden['titel']}' staat bij {STATUS_NAMEN[velden['status']]}" + (f", {agenda}" if agenda else "") + ".")


@router.post("/taken/{tid:int}")
async def taak_opslaan(request: Request, tid: int):
    controleer_herkomst(request)
    f = await request.form()
    velden = uit_formulier(f)
    if not velden["titel"]:
        return _terug(f"/taken?bewerk={tid}", fout="Geef de taak een korte titel.")

    def doe():
        with db.pool.connection() as c:
            if not c.execute("SELECT 1 FROM takenbord WHERE id = %s", (tid,)).fetchone():
                raise HTTPException(404)
            c.execute("""UPDATE takenbord SET titel = %(titel)s, beschrijving = %(beschrijving)s, prio = %(prio)s, wacht_op = %(wacht_op)s,
                         startdatum = %(startdatum)s, starttijd = %(starttijd)s, einddatum = %(einddatum)s, eindtijd = %(eindtijd)s,
                         doel = %(doel)s, afgerond = CASE WHEN %(status)s = 'afgerond' THEN coalesce(afgerond, now()) END,
                         status = %(status)s, bijgewerkt = now() WHERE id = %(id)s""", velden | {"id": tid})
            return agenda_bijwerken(c, tid)
    agenda = await asyncio.to_thread(doe)
    return _terug(f"/taken#taak-{tid}", melding=f"Opgeslagen" + (f", {agenda}" if agenda else "") + ".")


@router.post("/taken/{tid:int}/status")
async def taak_status(request: Request, tid: int):
    """Naar een andere kolom (slepen op het bord, of de keuzelijst op de kaart)."""
    controleer_herkomst(request)
    f = await request.form()
    status = str(f.get("status", ""))
    if status not in STATUS_NAMEN:
        raise HTTPException(400)

    def doe():
        with db.pool.connection() as c:
            t = c.execute("""UPDATE takenbord SET status = %s, bijgewerkt = now(),
                             afgerond = CASE WHEN %s = 'afgerond' THEN coalesce(afgerond, now()) END
                             WHERE id = %s RETURNING *""", (status, status, tid)).fetchone()
            if not t:
                raise HTTPException(404)
            agenda_bijwerken(c, tid)
            return t
    t = await asyncio.to_thread(doe)
    if status == "wachtend" and not t["wacht_op"]:
        return _terug(f"/taken?bewerk={tid}&wacht=1#formulier", melding=f"'{t['titel']}' staat bij Wachtend op. Op wie wacht je?")
    return _terug(_terug_adres(f, f"/taken#taak-{tid}"), melding=f"'{t['titel']}' staat nu bij {STATUS_NAMEN[status]}.")


@router.post("/taken/{tid:int}/weg")
async def taak_weg(request: Request, tid: int):
    controleer_herkomst(request)

    def doe():
        with db.pool.connection() as c:
            t = c.execute("SELECT * FROM takenbord WHERE id = %s", (tid,)).fetchone()
            if not t:
                raise HTTPException(404)
            try:
                _weg_uit_agenda(c, t)
            except (agenda_bronnen.BronFout, kluis.KluisFout):
                pass
            c.execute("DELETE FROM takenbord WHERE id = %s", (tid,))
            return t
    t = await asyncio.to_thread(doe)
    return _terug("/taken", melding=f"Taak '{t['titel']}' verwijderd" + (" (ook uit de agenda)" if t["agenda_extern"] else "") + ".")


# ---------------------------------------------------------------- Noor

def _gesprek_adres(gid):
    return "/taken?praat=1" + (f"&g={gid}" if gid else "") + "#noor"


@router.post("/taken/gesprek/vraag")
async def noor_vraag(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    vraag = str(f.get("vraag") or "").strip()
    gid = int(f.get("gesprek") or 0) or None
    if not vraag:
        return _terug(_gesprek_adres(gid))
    with db.pool.connection() as c:
        if gid and not c.execute("SELECT 1 FROM gesprekken WHERE id = %s AND persona = 'noor'", (gid,)).fetchone():
            gid = None
        if not gid:
            gid = c.execute("INSERT INTO gesprekken (persona, titel, kader) VALUES ('noor', %s, 'taken') RETURNING id",
                            (" ".join(vraag.split())[:80],)).fetchone()["id"]
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (gid, vraag))
        c.execute("UPDATE gesprekken SET gearchiveerd = false WHERE id = %s", (gid,))

    def antwoorden():
        with db.pool.connection() as c:
            try:
                noor.antwoord(c, gid)
            except claude.ClaudeFout as e:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)",
                          (gid, f"(Ik kan nu niet antwoorden: {e})"))
    await asyncio.to_thread(antwoorden)
    return _terug(_gesprek_adres(gid))


@router.post("/taken/gesprek/nieuw")
async def noor_nieuw(request: Request):
    controleer_herkomst(request)
    return _terug("/taken?praat=1&g=0#noor")


def toepassen(c, v):
    """Een voorstel van Noor uitvoeren (na de klik). Geeft de uitslag in één zin."""
    g = noor.gegevens_terug(v["gegevens"])
    if v["soort"] == "nieuw":
        velden = {"titel": "", "beschrijving": "", "prio": "middel", "status": "nieuw", "wacht_op": "", "startdatum": None, "starttijd": None,
                  "einddatum": None, "eindtijd": None, "doel": ""} | g
        if velden["startdatum"] or velden["einddatum"]:
            velden["status"] = velden["status"] if velden["status"] != "nieuw" else "gepland"
        tid = nieuw(c, velden)
        agenda = agenda_bijwerken(c, tid)
        return tid, f"Taak '{velden['titel']}' staat bij {STATUS_NAMEN[velden['status']]}" + (f", {agenda}" if agenda else "") + "."
    t = c.execute("SELECT * FROM takenbord WHERE id = %s", (v["taak_id"],)).fetchone()
    if not t:
        return None, "Die taak bestaat niet meer."
    sets = ", ".join(f"{k} = %({k})s" for k in g)
    status = g.get("status", t["status"])
    c.execute(f"""UPDATE takenbord SET {sets + ', ' if sets else ''}bijgewerkt = now(),
                  afgerond = CASE WHEN %(_status)s = 'afgerond' THEN coalesce(afgerond, now()) END WHERE id = %(_id)s""",
              g | {"_status": status, "_id": t["id"]})
    agenda = agenda_bijwerken(c, t["id"])
    return t["id"], f"'{g.get('titel') or t['titel']}' bijgewerkt" + (f", {agenda}" if agenda else "") + "."


@router.post("/taken/voorstel/{vid:int}")
async def noor_voorstel(request: Request, vid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        v = c.execute("SELECT * FROM taak_voorstellen WHERE id = %s AND status = 'open'", (vid,)).fetchone()
        if not v:
            return _terug("/taken?praat=1#noor", fout="Dit voorstel is al afgehandeld.")
        if f.get("actie") == "weg":
            c.execute("UPDATE taak_voorstellen SET status = 'afgewezen' WHERE id = %s", (vid,))
            return _terug(_gesprek_adres(v["gesprek_id"]))

    def doe():
        with db.pool.connection() as c:
            tid, uitslag = toepassen(c, v)
            c.execute("UPDATE taak_voorstellen SET status = 'uitgevoerd', uitslag = %s, taak_id = coalesce(taak_id, %s) WHERE id = %s",
                      (uitslag[:500], tid, vid))
            c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (v["gesprek_id"], f"(Gedaan: {uitslag})"))
            c.execute("INSERT INTO logboek (persona, actie, details) VALUES ('noor', 'voorstel uitgevoerd', %s)",
                      (json.dumps({"soort": v["soort"], "taak": tid, "uitslag": uitslag[:200]}),))
            return uitslag
    uitslag = await asyncio.to_thread(doe)
    return _terug(_gesprek_adres(v["gesprek_id"]), melding=uitslag)
