"""De schermen van Bram: de postkamer (/bram) en de mail-instellingen (/bram/instellingen).

Bram doet niets in je mailbox uit zichzelf: archiveren, labelen en gelezen markeren gebeurt alleen na jouw klik.
Koppelen loopt via dezelfde apps en hetzelfde terugkeeradres als Clara (/clara/koppel/.../terug); daar ziet de
portal aan het koppelverzoek dat het voor Bram is.
"""
import asyncio
import datetime as dt
import json
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import personas
from ..clara import kluis, web as clara_web
from ..clara.web import _nieuwe_staat  # koppelverzoeken delen de tabel oauth_staat met Clara
from ..kern import claude, config, db, planner
from ..kern.web import DAGEN, controleer_herkomst, ctx, templates
from . import bronnen, gesprek, mailhtml, sync

router = APIRouter()
DOMEINEN = config.DOMEINEN
GROEPEN = (("actie", "Actie nodig", "Hier wordt iets van je gevraagd."),
           ("lezen", "Lezen", "Persoonlijk, inhoudelijk of ter info."),
           ("weg", "Kan weg", "Nieuwsbrieven en automatische berichten. Vink uit wat je wilt houden."))
GROEP_NAMEN = {g[0]: g[1] for g in GROEPEN}


def _terug(pad, **melding):
    pad, _, anker = pad.partition("#")
    if melding:
        pad += ("&" if "?" in pad else "?") + urlencode(melding)
    return RedirectResponse(pad + (f"#{anker}" if anker else ""), status_code=303)


def groep(b):
    """In welke groep van de postkamer een bericht staat: zelf verplaatst gaat voor het oordeel van Bram. Pure functie."""
    if b.get("groep_handmatig") in GROEP_NAMEN:
        return b["groep_handmatig"]
    if b["categorie"] in ("actie", "antwoord") or b["prioriteit"] == "hoog":
        return "actie"
    if b["categorie"] in ("nieuwsbrief", "automatisch") or b["actie"] == "archiveren":
        return "weg"
    return "lezen"


MAPPEN = (("alles", "Alles", ""),) + GROEPEN
BULK = {"archiveren": "gearchiveerd", "verwijderen": "verwijderd", "gelezen": "als gelezen gemarkeerd",
        "ongelezen": "als ongelezen gemarkeerd", "uitschrijven": "uitgeschreven", "verplaats": "verplaatst", "map": "gearchiveerd in",
        "taak": "als taak in Taken gezet"}


def lijst_adres(map_="alles", account=0, praat=False):
    """Het adres van de lijst met de gekozen map, het gekozen account en of het gesprek open staat. Pure functie."""
    delen = ([f"map={map_}"] if map_ in GROEP_NAMEN else []) + ([f"account={int(account)}"] if account else []) + (["praat=1"] if praat else [])
    return "/bram" + ("?" + "&".join(delen) if delen else "")


def _paneel_adres(f, gid):
    """Terug naar de lijst (zelfde map en mailbox) met het gesprek open."""
    try:
        account = int(f.get("account") or 0)
    except ValueError:
        account = 0
    return lijst_adres(str(f.get("map") or "alles"), account, True) + ("" if gid in (None, "") else f"&g={gid}") + "#gesprek"


def _lijst_uit_formulier(f):
    try:
        account = int(f.get("account") or 0)
    except ValueError:
        account = 0
    return lijst_adres(str(f.get("map") or "alles"), account, f.get("praat") == "1")


def _gesprek_paneel(c, g=None):
    """Het gesprek over de postkamer: het gekozen gesprek, anders het laatste. Met open en uitgevoerde voorstellen."""
    if g:
        gs = c.execute("SELECT * FROM gesprekken WHERE id = %s AND persona = 'bram' AND kader = 'postkamer'", (g,)).fetchone()
    elif g is None:
        gs = c.execute("""SELECT * FROM gesprekken WHERE persona = 'bram' AND kader = 'postkamer' AND NOT gearchiveerd
                          ORDER BY bijgewerkt DESC LIMIT 1""").fetchone()
    else:
        gs = None
    uit = {"gesprek": gs, "berichten": [], "voorstellen": [], "klaar": [], "namen": {},
           "eerdere": c.execute("""SELECT id, titel, bijgewerkt FROM gesprekken WHERE persona = 'bram' AND kader = 'postkamer'
                                   AND NOT gearchiveerd AND id <> %s ORDER BY bijgewerkt DESC LIMIT 10""", (gs["id"] if gs else 0,)).fetchall(),
           "zonder_claude": [k["naam"] for k in c.execute("SELECT naam FROM post_koppelingen WHERE actief AND NOT claude ORDER BY id")]}
    if gs:
        uit["berichten"] = c.execute("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gs["id"],)).fetchall()
        uit["voorstellen"] = c.execute("SELECT * FROM post_voorstellen WHERE gesprek_id = %s AND status = 'open' ORDER BY id", (gs["id"],)).fetchall()
        uit["klaar"] = c.execute("SELECT * FROM post_voorstellen WHERE gesprek_id = %s AND status = 'uitgevoerd' ORDER BY id", (gs["id"],)).fetchall()
        ids = {i for v in uit["voorstellen"] for i in (v["gegevens"].get("berichten") or [])} | {v["bericht_id"] for v in uit["voorstellen"] if v["bericht_id"]}
        if ids:
            uit["namen"] = {r["id"]: r for r in c.execute("SELECT id, van_naam, onderwerp FROM post_berichten WHERE id = ANY(%s::int[])", (sorted(ids),))}
    return uit


@router.get("/bram", response_class=HTMLResponse)
def postkamer(request: Request, map: str = "alles", account: int = 0, praat: int = 0, g: int | None = None,  # pylint: disable=redefined-builtin
              melding: str = "", fout: str = ""):
    """Eén lijst voor alle mailboxen, zoals Gmail: links mappen en accounts, rechts de berichten (en het gesprek met Bram)."""
    if map not in GROEP_NAMEN:
        map = "alles"
    with db.pool.connection() as c:
        paneel = _gesprek_paneel(c, g) if praat else None
        agendas = clara_web.sync.koppelingen(c, alleen_actief=True) if praat else []
        koppelingen = sync.koppelingen(c)
        berichten = c.execute("""SELECT b.*, k.naam AS koppeling, k.domein, k.soort FROM post_berichten b
                                 JOIN post_koppelingen k ON k.id = b.koppeling_id
                                 WHERE b.status = 'open' AND k.actief ORDER BY b.ontvangen DESC""").fetchall()
        mapkeuze = []
        for k in koppelingen:
            if k["actief"] and not k["fout"] and (not account or k["id"] == account):
                try:
                    mapkeuze.append((k, sync.mappen(c, k)))
                except Exception:  # noqa: BLE001  (zonder mappen werkt de lijst gewoon)
                    continue
        recent = sync.recente_mappen(c, account=account)
    per_map = {m[0]: 0 for m in MAPPEN} | {"ongelezen_" + m[0]: 0 for m in MAPPEN}
    per_account = {}
    lijst = []
    for b in berichten:
        g = groep(b)
        if not account or b["koppeling_id"] == account:
            for m in ("alles", g):
                per_map[m] += 1
                per_map["ongelezen_" + m] += 0 if b["gelezen"] else 1
            if map in ("alles", g):
                lijst.append(b | {"groep": g})
        per_account[b["koppeling_id"]] = per_account.get(b["koppeling_id"], 0) + 1
    return templates.TemplateResponse("bram.html", ctx(
        request, "bram", p=personas.get("bram"), koppelingen=koppelingen, mappen=MAPPEN, groep_namen=GROEP_NAMEN,
        lijst=lijst, map=map, per_map=per_map, per_account=per_account, account=account, melding=melding, fout=fout,
        tz=config.TZ, DAGEN=DAGEN, CATEGORIEEN=sync.CATEGORIEEN, nu=dt.datetime.now(config.TZ), kluis_ok=kluis.actief(),
        domeinnaam=dict(DOMEINEN), groepen=GROEPEN, praat=praat, paneel=paneel, sleutel_ok=bool(config.ANTHROPIC_API_KEY),
        doelen=clara_web._schrijfdoelen(agendas), eigen=clara_web._reisdoelen(agendas),  # pylint: disable=protected-access
        datum=lambda iso: dt.datetime.fromisoformat(iso).astimezone(config.TZ), lijst_adres=lijst_adres,
        mapkeuze=[(k, m) for k, m in mapkeuze if m], recent=recent))


@router.post("/bram/gesprek/vraag")
async def postkamer_vraag(request: Request):
    """Een vraag aan Bram over de hele postkamer, eventueel over de aangevinkte berichten."""
    controleer_herkomst(request)
    f = await request.form()
    tekst = str(f.get("vraag", "")).strip()[:4000]
    gekozen = [int(x) for x in f.getlist("gekozen") if str(x).isdigit()][:5]
    try:
        gid = int(f.get("gesprek") or 0) or None
    except ValueError:
        gid = None
    if not tekst:
        return _terug(_paneel_adres(f, gid))
    with db.pool.connection() as c:
        if gid and not c.execute("SELECT 1 FROM gesprekken WHERE id = %s AND persona = 'bram' AND kader = 'postkamer'", (gid,)).fetchone():
            gid = None
        if not gid:
            gid = c.execute("INSERT INTO gesprekken (persona, titel, kader) VALUES ('bram', %s, 'postkamer') RETURNING id",
                            (" ".join(tekst.split())[:80],)).fetchone()["id"]
        vraag_tekst = tekst + (f"\n\n(Aangevinkt: {len(gekozen)} bericht{'en' if len(gekozen) != 1 else ''}.)" if gekozen else "")
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (gid, vraag_tekst))
        c.execute("UPDATE gesprekken SET gearchiveerd = false WHERE id = %s", (gid,))

    def antwoorden():
        with db.pool.connection() as c:
            try:
                gesprek.postkamer_antwoord(c, gid, gekozen, ook_zonder_claude=f.get("ook") == "1")
            except (claude.ClaudeFout, bronnen.BronFout, kluis.KluisFout) as e:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)",
                          (gid, f"(Ik kan nu niet antwoorden: {e})"))
    await asyncio.to_thread(antwoorden)
    return RedirectResponse(_paneel_adres(f, gid), status_code=303)


@router.post("/bram/gesprek/nieuw")
async def postkamer_nieuw(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    return _terug(_paneel_adres(f, 0))


@router.post("/bram/vernieuwen")
async def vernieuwen(request: Request):
    controleer_herkomst(request)
    uitslag = await asyncio.to_thread(planner.draai, db.pool, "post", True)
    return _terug("/bram", melding=(uitslag or "Bijgewerkt.")[:300])


def _bericht(c, bid):
    b = c.execute("SELECT * FROM post_berichten WHERE id = %s", (bid,)).fetchone()
    if not b:
        raise HTTPException(404)
    return b


# ---------------------------------------------------------------- één bericht: lezen en Bram vragen

BEELDEN_SLEUTEL = "bram_beelden_afzenders"  # afzenders van wie afbeeldingen van buiten altijd mogen laden


def _vertrouwd(c):
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = %s", (BEELDEN_SLEUTEL,)).fetchone()
    try:
        return set(json.loads(r["waarde"])) if r and r["waarde"] else set()
    except ValueError:
        return set()


def mailweergave(volledig, afzender, beelden=False, vertrouwd=()):
    """Wat het scherm van de mail laat zien: een veilig HTML-document voor het kader, of platte tekst met links. Pure functie."""
    if not volledig:
        return None
    if volledig.get("html"):
        aan = beelden or (afzender or "").lower() in vertrouwd
        opgeschoond, tegengehouden = mailhtml.schoon_met_telling(volledig["html"], volledig.get("cid"), beelden=aan)
        extern = bool(tegengehouden) or (aan and mailhtml.heeft_externe_beelden(opgeschoond))
        return {"html": mailhtml.document(opgeschoond, beelden=aan), "extern": extern, "beelden": aan,
                "vertrouwd": (afzender or "").lower() in vertrouwd, "tekst": mailhtml.linkify(volledig.get("tekst") or "")}
    return {"html": "", "tekst": mailhtml.linkify(volledig.get("tekst") or ""), "extern": False, "beelden": False, "vertrouwd": False}


@router.post("/bram/bericht/{bid:int}/beelden")
async def beelden_afzender(request: Request, bid: int):
    """Afbeeldingen van buiten altijd (of niet meer) laden voor de afzender van dit bericht."""
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        b = _bericht(c, bid)
        lijst = _vertrouwd(c)
        adres = (b["van_adres"] or "").lower()
        if adres:
            (lijst.add if f.get("aan", "1") != "0" else lijst.discard)(adres)
            c.execute("""INSERT INTO instellingen (sleutel, waarde) VALUES (%s, %s)
                         ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()""",
                      (BEELDEN_SLEUTEL, json.dumps(sorted(lijst))))
    return RedirectResponse(f"/bram/bericht/{bid}#mail", status_code=303)


@router.get("/bram/bericht/{bid:int}", response_class=HTMLResponse)
def bericht_scherm(request: Request, bid: int, melding: str = "", fout: str = "", beelden: int = 0):
    with db.pool.connection() as c:
        b = _bericht(c, bid)
        k = c.execute("SELECT * FROM post_koppelingen WHERE id = %s", (b["koppeling_id"],)).fetchone()
        berichten = c.execute("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (b["gesprek_id"],)).fetchall() if b["gesprek_id"] else []
        voorstellen = c.execute("SELECT * FROM post_voorstellen WHERE bericht_id = %s AND status = 'open' ORDER BY id", (bid,)).fetchall()
        klaar = c.execute("SELECT * FROM post_voorstellen WHERE bericht_id = %s AND status = 'uitgevoerd' ORDER BY id", (bid,)).fetchall()
        agendas = clara_web.sync.koppelingen(c, alleen_actief=True)
        try:
            _, volledig = gesprek.volledig_ophalen(c, b)
            lees_fout = ""
        except Exception as e:  # noqa: BLE001  (dan toont het scherm wat Bram al weet)
            volledig, lees_fout = None, str(e)
        try:
            labels = sync.labels(c, k)
        except Exception:  # noqa: BLE001  (zonder labels werkt het scherm gewoon)
            labels = []
        try:
            mappen_ = sync.mappen(c, k)
        except Exception:  # noqa: BLE001
            mappen_ = []
        recent = sync.recente_mappen(c, account=k["id"])
        weergave = mailweergave(volledig, b["van_adres"], bool(beelden), _vertrouwd(c))
    return templates.TemplateResponse("bram_bericht.html", ctx(
        request, "bram", p=personas.get("bram"), b=b, k=k, volledig=volledig, weergave=weergave, groep_nu=groep(b), groepen=GROEPEN, labels=labels,
        label_namen=dict(labels), lees_fout=lees_fout, berichten=berichten, mappen=mappen_, recent=recent,
        voorstellen=voorstellen, klaar=klaar, doelen=clara_web._schrijfdoelen(agendas),  # pylint: disable=protected-access
        eigen=clara_web._reisdoelen(agendas), melding=melding, fout=fout, tz=config.TZ,  # pylint: disable=protected-access
        DAGEN=DAGEN, CATEGORIEEN=sync.CATEGORIEEN, sleutel_ok=bool(config.ANTHROPIC_API_KEY),
        datum=lambda iso: dt.datetime.fromisoformat(iso).astimezone(config.TZ)))


@router.post("/bram/bericht/{bid:int}/vraag")
async def vraag(request: Request, bid: int):
    """Een vraag aan Bram over dit bericht. Mag Claude de mail van dit account niet lezen, dan alleen met 'deze keer'."""
    controleer_herkomst(request)
    f = await request.form()
    tekst = str(f.get("vraag", "")).strip()[:4000]
    if not tekst:
        return _terug(f"/bram/bericht/{bid}#gesprek")
    with db.pool.connection() as c:
        b = _bericht(c, bid)
        k = c.execute("SELECT * FROM post_koppelingen WHERE id = %s", (b["koppeling_id"],)).fetchone()
        if not k["claude"] and f.get("toestaan") != "1":
            return _terug(f"/bram/bericht/{bid}#gesprek", fout="Claude mag de mail van dit account niet lezen. Vink 'deze mail mag Claude "
                                                              "lezen' aan, of zet het vinkje bij Post › Instellingen.")
        gid = b["gesprek_id"]
        if not gid:
            gid = c.execute("INSERT INTO gesprekken (persona, titel) VALUES ('bram', %s) RETURNING id", (b["onderwerp"][:80],)).fetchone()["id"]
            c.execute("UPDATE post_berichten SET gesprek_id = %s WHERE id = %s", (gid, bid))
        c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (gid, tekst))

    def antwoorden():
        with db.pool.connection() as c:
            try:
                gesprek.antwoord(c, _bericht(c, bid), gid)
            except (claude.ClaudeFout, bronnen.BronFout, kluis.KluisFout) as e:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)",
                          (gid, f"(Ik kan nu niet antwoorden: {e})"))
    await asyncio.to_thread(antwoorden)
    return RedirectResponse(f"/bram/bericht/{bid}#gesprek", status_code=303)


@router.post("/bram/voorstel/{vid:int}")
async def voorstel(request: Request, vid: int):
    """Een voorstel van Bram uitvoeren (na Martijns klik): antwoord versturen, afspraak of herinnering in de agenda."""
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        v = c.execute("SELECT * FROM post_voorstellen WHERE id = %s AND status = 'open'", (vid,)).fetchone()
        if not v:
            return _terug("/bram", fout="Dit voorstel is al afgehandeld.")
        kader = c.execute("SELECT kader FROM gesprekken WHERE id = %s", (v["gesprek_id"],)).fetchone() if v["gesprek_id"] else None
        if kader and kader["kader"] == "postkamer":
            terug = _paneel_adres(f, v["gesprek_id"])
        else:
            terug = f"/bram/bericht/{v['bericht_id']}#gesprek"
        if f.get("actie") == "weg":
            c.execute("UPDATE post_voorstellen SET status = 'afgewezen' WHERE id = %s", (vid,))
            return _terug(terug)
    g = v["gegevens"]

    def doe():
        with db.pool.connection() as c:
            if v["soort"] == "bulk":
                gelukt, fouten = 0, []
                for bid in g["berichten"]:
                    b = c.execute("SELECT * FROM post_berichten WHERE id = %s AND status = 'open'", (bid,)).fetchone()
                    if not b:
                        continue
                    try:
                        if g["actie"] == "verplaats":
                            c.execute("UPDATE post_berichten SET groep_handmatig = %s WHERE id = %s", (g["groep"], bid))
                        else:
                            sync.uitvoeren(c, b, g["actie"])
                        gelukt += 1
                    except (bronnen.BronFout, kluis.KluisFout) as e:
                        fouten.append(f"{b['onderwerp'][:40]}: {e}")
                uitslag = (f"{gelukt} bericht{'en' if gelukt != 1 else ''} {BULK[g['actie']]}"
                           + (f" naar {GROEP_NAMEN[g['groep']]}" if g["actie"] == "verplaats" else "") + "."
                           + (" Niet gelukt: " + "; ".join(fouten[:3]) if fouten else ""))
                c.execute("UPDATE post_voorstellen SET status = 'uitgevoerd', uitslag = %s WHERE id = %s", (uitslag[:500], vid))
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (v["gesprek_id"], f"(Gedaan: {uitslag})"))
                return uitslag
            b = _bericht(c, v["bericht_id"])
            if v["soort"] == "antwoord":
                tekst = str(f.get("tekst") or g.get("tekst") or "").strip()
                if not tekst:
                    raise bronnen.BronFout("Het antwoord is leeg; er is niets verstuurd.")
                aan = gesprek.versturen(c, b, tekst)
                uitslag = f"Antwoord verstuurd aan {aan}."
                c.execute("UPDATE post_voorstellen SET gegevens = gegevens || %s::jsonb WHERE id = %s", (json.dumps({"tekst": tekst}), vid))
            else:
                start = dt.datetime.fromisoformat(g["start"])
                eind = dt.datetime.fromisoformat(g["eind"])
                titel = str(f.get("titel") or g["titel"]).strip()[:200] or g["titel"]
                if v["soort"] == "herinnering":
                    clara_web._maak(c, str(f.get("doel", "")), "🔔 " + titel.removeprefix("🔔").strip(), start, eind,  # pylint: disable=protected-access
                                    f"Herinnering van Bram (Buurmanitor) bij de mail '{b['onderwerp']}'.", vrij=True)
                    uitslag = f"Herinnering '{titel}' staat in je agenda op {start.astimezone(config.TZ):%d-%m om %H.%M}."
                else:
                    omschrijving = (g.get("omschrijving") or "") + f"\n\nIngepland door Bram (Buurmanitor) uit de mail '{b['onderwerp']}' van {b['van_naam']}."
                    clara_web._maak(c, str(f.get("doel", "")), titel, start, eind, omschrijving.strip(), locatie=g.get("locatie", ""))  # pylint: disable=protected-access
                    uitslag = f"'{titel}' staat in de agenda op {start.astimezone(config.TZ):%d-%m om %H.%M}."
            c.execute("UPDATE post_voorstellen SET status = 'uitgevoerd', uitslag = %s WHERE id = %s", (uitslag, vid))
            if v["gesprek_id"]:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (v["gesprek_id"], f"(Gedaan: {uitslag})"))
            return uitslag
    try:
        uitslag = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout, clara_web.bronnen.BronFout) as e:
        return _terug(terug, fout=str(e)[:300])
    return _terug(terug, melding=uitslag)


@router.post("/bram/bericht/{bid:int}/verplaats")
async def verplaats(request: Request, bid: int):
    """Een bericht naar een andere groep zetten (Actie nodig, Lezen, Kan weg); met 'altijd' ook voortaan voor die afzender."""
    controleer_herkomst(request)
    f = await request.form()
    keuze = str(f.get("groep", ""))
    naar, altijd = keuze.removesuffix("|altijd"), keuze.endswith("|altijd")
    if naar not in GROEP_NAMEN:
        raise HTTPException(400)
    with db.pool.connection() as c:
        b = _bericht(c, bid)
        c.execute("UPDATE post_berichten SET groep_handmatig = %s WHERE id = %s", (naar, bid))
        tekst = f"'{b['onderwerp'][:60]}' staat nu bij {GROEP_NAMEN[naar]}."
        if altijd and b["van_adres"]:
            c.execute("""INSERT INTO post_afzenderregels (van_adres, groep) VALUES (%s, %s)
                         ON CONFLICT (van_adres) DO UPDATE SET groep = EXCLUDED.groep, aangemaakt = now()""", (b["van_adres"], naar))
            n = c.execute("UPDATE post_berichten SET groep_handmatig = %s WHERE van_adres = %s AND status = 'open' RETURNING id",
                          (naar, b["van_adres"])).fetchall()
            tekst = f"Mail van {b['van_adres']} staat voortaan bij {GROEP_NAMEN[naar]} ({len(n)} nu)."
    if f.get("terug") == "bericht":
        return _terug(f"/bram/bericht/{bid}", melding=tekst)
    return _terug(_lijst_uit_formulier(f), melding=tekst)


@router.post("/bram/afzenderregel/weg")
async def afzenderregel_weg(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    adres = str(f.get("van_adres", ""))
    with db.pool.connection() as c:
        c.execute("DELETE FROM post_afzenderregels WHERE van_adres = %s", (adres,))
    return _terug("/bram/instellingen#regels", melding=f"Regel voor {adres} weggehaald. Nieuwe mail van dit adres sorteert Bram weer zelf.")


@router.post("/bram/bericht/{bid:int}/map")
async def bericht_in_map(request: Request, bid: int):
    """Eén bericht (met de rest van de conversatie) archiveren in een map van Gmail of Outlook."""
    controleer_herkomst(request)
    f = await request.form()
    map_id = str(f.get("map_doel") or f.get("map") or "").split("|", 1)[-1]
    terug = _lijst_uit_formulier(f)

    def doe():
        with db.pool.connection() as c:
            b = _bericht(c, bid)
            return b, sync.archiveer_in(c, b, map_id)
    try:
        b, tekst = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/bram/bericht/{bid}", fout=str(e)[:300])
    return _terug(terug, melding=f"'{b['onderwerp'][:60]}': {tekst}.")


@router.post("/bram/bericht/{bid:int}/{actie}")
async def bericht_actie(request: Request, bid: int, actie: str):
    """Archiveren, gelezen markeren of een label zetten, na Martijns klik."""
    controleer_herkomst(request)
    if actie not in ("archiveren", "gelezen", "ongelezen", "label", "verwijderen", "uitschrijven"):
        raise HTTPException(404)
    f = await request.form()
    terug = _lijst_uit_formulier(f)
    weg = actie in ("archiveren", "verwijderen")
    vanuit_bericht = f.get("terug") == "bericht"

    def doe():
        with db.pool.connection() as c:
            b = _bericht(c, bid)
            return b, sync.uitvoeren(c, b, actie, str(f.get("label", "")))
    try:
        b, tekst = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/bram/bericht/{bid}" if vanuit_bericht else terug, fout=str(e)[:300])
    if vanuit_bericht and not weg:
        return _terug(f"/bram/bericht/{bid}", melding=f"{tekst[:1].upper()}{tekst[1:]}.")
    return _terug(terug, melding=f"'{b['onderwerp'][:60]}': {tekst}.")


@router.post("/bram/bulk")
@router.post("/bram/archiveren")
async def bulk(request: Request):
    """Een actie op de aangevinkte berichten: archiveren, verwijderen, (on)gelezen, uitschrijven of verplaatsen."""
    controleer_herkomst(request)
    f = await request.form()
    actie = str(f.get("actie") or "archiveren")
    naar = str(f.get("groep", ""))
    if actie.startswith("verplaats:"):  # één knop per map: meteen daarheen
        actie, naar = "verplaats", actie.split(":", 1)[1]
    doel_map = ""
    if actie.startswith("map:") or actie == "map":  # archiveren in een map van Gmail of Outlook
        doel_map, actie = (actie[4:] if actie.startswith("map:") else str(f.get("map_doel", ""))), "map"
        try:
            map_koppeling, map_id = int(doel_map.split("|", 1)[0]), doel_map.split("|", 1)[1]
        except (ValueError, IndexError):
            return _terug(_lijst_uit_formulier(f), fout="Kies een map.")
    if actie not in BULK:
        raise HTTPException(400)
    if actie == "verplaats" and naar not in GROEP_NAMEN:
        return _terug(_lijst_uit_formulier(f), fout="Kies waar de berichten heen moeten.")
    ids = [int(x) for x in f.getlist("bericht") if str(x).isdigit()]
    terug = _lijst_uit_formulier(f)
    if not ids:
        return _terug(terug, fout="Niets aangevinkt; er is niets gedaan.")

    overgeslagen = []

    def doe():
        gelukt, fouten = 0, []
        with db.pool.connection() as c:
            for bid in ids:
                b = c.execute("SELECT * FROM post_berichten WHERE id = %s AND status = 'open'", (bid,)).fetchone()
                if not b:
                    continue
                if actie == "map" and b["koppeling_id"] != map_koppeling:
                    overgeslagen.append(b["id"])  # die map hoort bij een andere mailbox
                    continue
                try:
                    if actie == "taak":
                        from ..taken import web as taken_web  # pylint: disable=import-outside-toplevel
                        voorstel = taken_web.uit_bericht(b)
                        taken_web.nieuw(c, {k: v for k, v in voorstel.items() if k != "bericht_id"}, b["id"])
                    elif actie == "map":
                        laatste = sync.archiveer_in(c, b, map_id)
                        doe.map_naam = laatste.split(" (met ")[0].removeprefix("gearchiveerd in ")
                    elif actie == "verplaats":
                        c.execute("UPDATE post_berichten SET groep_handmatig = %s WHERE id = %s", (naar, bid))
                    else:
                        sync.uitvoeren(c, b, actie)
                    gelukt += 1
                except (bronnen.BronFout, kluis.KluisFout) as e:
                    fouten.append(f"{b['onderwerp'][:40]}: {e}")
        return gelukt, fouten
    doe.map_naam = ""
    gelukt, fouten = await asyncio.to_thread(doe)
    tekst = (f"{gelukt} bericht{'en' if gelukt != 1 else ''} {BULK[actie]}" + (f" naar {GROEP_NAMEN[naar]}" if actie == "verplaats" else "")
             + (f" {doe.map_naam}" if actie == "map" and doe.map_naam else "") + ".")
    if overgeslagen:
        tekst += f" {len(overgeslagen)} uit een andere mailbox overgeslagen: die map hoort bij één mailbox."
    if fouten:
        return _terug(terug, melding=tekst, fout="Niet gelukt: " + "; ".join(fouten[:3])[:300])
    return _terug(terug, melding=tekst)


# ---------------------------------------------------------------- instellingen

@router.get("/bram/instellingen", response_class=HTMLResponse)
def instellingen(request: Request, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        koppelingen = sync.koppelingen(c)
        aantal = {r["koppeling_id"]: r["n"] for r in c.execute(
            "SELECT koppeling_id, count(*) AS n FROM post_berichten WHERE status = 'open' GROUP BY koppeling_id")}
        regels = c.execute("SELECT * FROM post_afzenderregels ORDER BY van_adres").fetchall()
    return templates.TemplateResponse("bram_instellingen.html", ctx(
        request, "bram", koppelingen=koppelingen, aantal=aantal, regels=regels, groep_namen=GROEP_NAMEN, melding=melding, fout=fout, kluis_ok=kluis.actief(),
        google_ok=bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
        ms_ok=bool(config.MS_CLIENT_ID and config.MS_CLIENT_SECRET), sleutel_ok=bool(config.ANTHROPIC_API_KEY),
        domeinnaam=dict(DOMEINEN)))


@router.post("/bram/koppel/{soort}")
async def koppel_start(request: Request, soort: str):
    controleer_herkomst(request)
    if soort not in ("google", "microsoft"):
        raise HTTPException(404)
    f = await request.form()
    domein = str(f.get("domein", "prive"))
    opties = {"voor": "bram", "domein": domein if domein in dict(DOMEINEN) else "prive",
              "naam": str(f.get("naam", "")).strip()[:80], "claude": f.get("claude") == "1"}
    if not kluis.actief():
        return _terug("/bram/instellingen", fout="Er is nog geen kluissleutel. Draai op de server: bash ~/buurmanitor/app/deploy/clara.sh")
    if soort == "google" and not (config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET):
        return _terug("/bram/instellingen", fout="De Google-app is nog niet ingesteld (clara.sh).")
    if soort == "microsoft" and not (config.MS_CLIENT_ID and config.MS_CLIENT_SECRET):
        return _terug("/bram/instellingen", fout="De Microsoft-app is nog niet ingesteld (clara.sh).")
    with db.pool.connection() as c:
        staat = _nieuwe_staat(c, soort, opties)
    return RedirectResponse(bronnen.google_url(staat) if soort == "google" else bronnen.ms_url(staat), status_code=303)


async def koppeling_klaar(soort, code, opties):
    """Het tweede deel van koppelen (vanuit /clara/koppel/{soort}/terug): token bewaren en de inbox voor het eerst ophalen."""
    try:
        gegevens = await asyncio.to_thread(bronnen.google_code if soort == "google" else bronnen.ms_code, code)
    except bronnen.BronFout as e:
        return _terug("/bram/instellingen", fout=str(e))
    with db.pool.connection() as c:
        bestaand = c.execute("SELECT * FROM post_koppelingen WHERE soort = %s AND lower(account) = lower(%s) AND account <> ''",
                             (soort, gegevens["account"])).fetchone()
        if bestaand:
            c.execute("UPDATE post_koppelingen SET geheim = %s, tenant = %s, fout = NULL, actief = true WHERE id = %s",
                      (kluis.sluit(gegevens["refresh"]), gegevens.get("tenant", ""), bestaand["id"]))
            sync.vergeet_toegang(bestaand["id"])
            kid = bestaand["id"]
        else:
            kid = c.execute("""INSERT INTO post_koppelingen (soort, naam, account, tenant, domein, geheim, claude)
                               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                            (soort, opties.get("naam") or gegevens["account"] or soort.capitalize(), gegevens["account"],
                             gegevens.get("tenant", ""), opties.get("domein", "prive"), kluis.sluit(gegevens["refresh"]),
                             bool(opties.get("claude")))).fetchone()["id"]

    def eerste_keer():
        with db.pool.connection() as c:
            return sync.synchroniseer(c, c.execute("SELECT * FROM post_koppelingen WHERE id = %s", (kid,)).fetchone())
    uitslag = await asyncio.to_thread(eerste_keer)
    return _terug("/bram/instellingen", melding=f"{gegevens['account'] or soort} {'opnieuw gekoppeld' if bestaand else 'gekoppeld'}. {uitslag}")


@router.post("/bram/koppeling/{kid:int}")
async def koppeling_bijwerken(request: Request, kid: int):
    controleer_herkomst(request)
    f = await request.form()
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM post_koppelingen WHERE id = %s", (kid,)).fetchone()
        if not k:
            raise HTTPException(404)
        if f.get("actie") == "weg":
            c.execute("DELETE FROM post_koppelingen WHERE id = %s", (kid,))
            sync.vergeet_toegang(kid)
            return _terug("/bram/instellingen", melding=f"'{k['naam']}' ontkoppeld. Je mail in Gmail of Outlook blijft gewoon staan.")
        domein = str(f.get("domein", k["domein"]))
        c.execute("UPDATE post_koppelingen SET naam = %s, domein = %s, actief = %s, claude = %s WHERE id = %s",
                  (str(f.get("naam", "")).strip()[:80] or k["naam"], domein if domein in dict(DOMEINEN) else k["domein"],
                   f.get("actief") == "1", f.get("claude") == "1", kid))
    return _terug("/bram/instellingen", melding="Opgeslagen.")
