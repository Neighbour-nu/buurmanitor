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
from . import bronnen, gesprek, sync

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


@router.get("/bram", response_class=HTMLResponse)
def postkamer(request: Request, account: int = 0, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        koppelingen = sync.koppelingen(c)
        berichten = c.execute("""SELECT b.*, k.naam AS koppeling, k.domein, k.soort FROM post_berichten b
                                 JOIN post_koppelingen k ON k.id = b.koppeling_id
                                 WHERE b.status = 'open' AND k.actief AND (%s = 0 OR k.id = %s)
                                 ORDER BY b.ontvangen DESC""", (account, account)).fetchall()
        labels, label_fout = {}, {}
        for k in koppelingen:
            if k["actief"] and not k["fout"] and (not account or k["id"] == account):
                try:
                    labels[k["id"]] = sync.labels(c, k)
                except Exception as e:  # noqa: BLE001  (zonder labels werkt de postkamer gewoon)
                    label_fout[k["id"]] = str(e)
        aantal = {r["koppeling_id"]: r["n"] for r in c.execute(
            "SELECT koppeling_id, count(*) AS n FROM post_berichten WHERE status = 'open' GROUP BY koppeling_id")}
    per_groep = {g[0]: [] for g in GROEPEN}
    for b in berichten:
        per_groep[groep(b)].append(b)
    namen = {k["id"]: dict(labels.get(k["id"], [])) for k in koppelingen}
    return templates.TemplateResponse("bram.html", ctx(
        request, "bram", p=personas.get("bram"), koppelingen=koppelingen, groepen=GROEPEN, per_groep=per_groep,
        labels=labels, label_namen=namen, account=account, aantal=aantal, melding=melding, fout=fout, tz=config.TZ,
        DAGEN=DAGEN, CATEGORIEEN=sync.CATEGORIEEN, nu=dt.datetime.now(config.TZ), kluis_ok=kluis.actief(),
        domeinnaam=dict(DOMEINEN)))


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

@router.get("/bram/bericht/{bid:int}", response_class=HTMLResponse)
def bericht_scherm(request: Request, bid: int, melding: str = "", fout: str = ""):
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
    return templates.TemplateResponse("bram_bericht.html", ctx(
        request, "bram", p=personas.get("bram"), b=b, k=k, volledig=volledig, groep_nu=groep(b), groepen=GROEPEN, lees_fout=lees_fout, berichten=berichten,
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
        terug = f"/bram/bericht/{v['bericht_id']}#gesprek"
        if f.get("actie") == "weg":
            c.execute("UPDATE post_voorstellen SET status = 'afgewezen' WHERE id = %s", (vid,))
            return _terug(terug)
    g = v["gegevens"]

    def doe():
        with db.pool.connection() as c:
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
            if b["gesprek_id"]:
                c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (b["gesprek_id"], f"(Gedaan: {uitslag})"))
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
    terug = "/bram" + (f"?account={int(f.get('account') or 0)}" if f.get("account") not in (None, "", "0") else "")
    return _terug(f"{terug}#{naar}", melding=tekst)


@router.post("/bram/afzenderregel/weg")
async def afzenderregel_weg(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    adres = str(f.get("van_adres", ""))
    with db.pool.connection() as c:
        c.execute("DELETE FROM post_afzenderregels WHERE van_adres = %s", (adres,))
    return _terug("/bram/instellingen#regels", melding=f"Regel voor {adres} weggehaald. Nieuwe mail van dit adres sorteert Bram weer zelf.")


@router.post("/bram/bericht/{bid:int}/{actie}")
async def bericht_actie(request: Request, bid: int, actie: str):
    """Archiveren, gelezen markeren of een label zetten, na Martijns klik."""
    controleer_herkomst(request)
    if actie not in ("archiveren", "gelezen", "label", "verwijderen", "uitschrijven"):
        raise HTTPException(404)
    f = await request.form()
    terug = "/bram" + (f"?account={int(f.get('account') or 0)}" if f.get("account") not in (None, "", "0") else "")
    weg = actie in ("archiveren", "verwijderen")
    vanuit_bericht = f.get("terug") == "bericht"

    def doe():
        with db.pool.connection() as c:
            b = _bericht(c, bid)
            return b, sync.uitvoeren(c, b, actie, str(f.get("label", "")))
    try:
        b, tekst = await asyncio.to_thread(doe)
    except (bronnen.BronFout, kluis.KluisFout) as e:
        return _terug(f"/bram/bericht/{bid}" if vanuit_bericht else terug + f"#b{bid}", fout=str(e)[:300])
    if vanuit_bericht and not weg:
        return _terug(f"/bram/bericht/{bid}", melding=f"{tekst[:1].upper()}{tekst[1:]}.")
    return _terug(terug + ("" if weg else f"#b{bid}"), melding=f"'{b['onderwerp'][:60]}': {tekst}.")


@router.post("/bram/archiveren")
async def archiveren(request: Request):
    """Meerdere berichten tegelijk archiveren of verwijderen (de aangevinkte uit Kan weg)."""
    controleer_herkomst(request)
    f = await request.form()
    actie = "verwijderen" if f.get("actie") == "verwijderen" else "archiveren"
    ids = [int(x) for x in f.getlist("bericht") if str(x).isdigit()]
    terug = "/bram" + (f"?account={int(f.get('account') or 0)}" if f.get("account") not in (None, "", "0") else "")
    if not ids:
        return _terug(terug, fout=f"Niets aangevinkt; er is niets {'verwijderd' if actie == 'verwijderen' else 'gearchiveerd'}.")

    def doe():
        gelukt, fouten = 0, []
        with db.pool.connection() as c:
            for bid in ids:
                b = c.execute("SELECT * FROM post_berichten WHERE id = %s AND status = 'open'", (bid,)).fetchone()
                if not b:
                    continue
                try:
                    sync.uitvoeren(c, b, actie)
                    gelukt += 1
                except (bronnen.BronFout, kluis.KluisFout) as e:
                    fouten.append(f"{b['onderwerp'][:40]}: {e}")
        return gelukt, fouten
    gelukt, fouten = await asyncio.to_thread(doe)
    tekst = f"{gelukt} bericht{'en' if gelukt != 1 else ''} {'verwijderd' if actie == 'verwijderen' else 'gearchiveerd'}."
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
