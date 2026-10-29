"""Promonitor: de politieke cockpit van PRO Helmond.

Opgezet zoals Doellijn (de ZEROHERO-cockpit): FastAPI, Jinja2 en htmx, met een eigen
PostgreSQL-database. Inloggen gebeurt vooraf door Authelia (via Caddy); de app vertrouwt
de header Remote-User die Caddy meestuurt en weigert verzoeken zonder die header.

Fase 0: huisstijl, alle schermen, instellingen (personen, vaste links, vergaderingen)
en de kennisbank met de kerndocumenten.
"""
import asyncio
import datetime as dt
import hmac
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import adviseur, agenda, begroting, dashboard, stemadvies, dossiers as dossierkaarten, fractie, kennisbank, notubiz, raadsinfo, schermen, schrijven, sjablonen, taken, whaller
from .schema import EENMALIG, MIGRATIES, STANDAARD_INSTELLINGEN, STANDAARD_PERSONEN, WHALLER_NODIG

DATABASE_URL = os.environ["POLITIEK_DATABASE_URL"]
HOST = os.environ.get("PORTAL_HOST", "buurmanitor.neighbour.nu")
AUTH_URL = os.environ.get("AUTH_URL", "https://auth.neighbour.nu")
OPEN_PADEN = ("/gezond", "/static/", "/favicon")  # zonder Remote-User bereikbaar (alleen intern of onschuldig)
# Geheim dat Caddy bij elk verzoek meestuurt. Is het ingesteld, dan accepteert de app alleen verzoeken die via Caddy
# komen; een andere container op het interne netwerk kan zich dan niet meer voordoen als ingelogde gebruiker.
PROXY_GEHEIM = os.environ.get("PROXY_GEHEIM", "")
# Adressen waarvandaan formulieren mogen komen: het eigen adres plus eventueel extra adressen (komma's).
TOEGESTANE_HOSTS = {HOST.lower()} | {h.strip().lower() for h in os.environ.get("TOEGESTANE_HOSTS", "").split(",") if h.strip()}
MAX_UPLOAD = 60 * 1024 * 1024  # gelijk aan request_body max_size in Caddy

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": True})


def zaai(c):
    """Vult lege tabellen één keer met de startgegevens."""
    if not c.execute("SELECT 1 FROM personen LIMIT 1").fetchone():
        for p in STANDAARD_PERSONEN:
            c.execute("""INSERT INTO personen (naam, whaller, rol, portefeuilles, volgorde)
                         VALUES (%s, %s, %s, %s, %s)""",
                      (p["naam"], p["whaller"], p["rol"], p["portefeuilles"], p["volgorde"]))
    for sleutel, (waarde, uitleg) in STANDAARD_INSTELLINGEN.items():
        c.execute("""INSERT INTO instellingen (sleutel, waarde, uitleg) VALUES (%s, %s, %s)
                     ON CONFLICT (sleutel) DO UPDATE SET uitleg = EXCLUDED.uitleg""", (sleutel, waarde, uitleg))
    for naam, opdrachten in EENMALIG:
        if c.execute("SELECT 1 FROM eenmalig WHERE naam = %s", (naam,)).fetchone():
            continue
        for opdracht in opdrachten:
            c.execute(*opdracht) if isinstance(opdracht, tuple) else c.execute(opdracht)
        c.execute("INSERT INTO eenmalig (naam) VALUES (%s)", (naam,))


@asynccontextmanager
async def lifespan(_app):
    pool.open(wait=True, timeout=30)
    with pool.connection() as c:
        for sql in MIGRATIES:
            c.execute(sql)
        zaai(c)
    planner = None
    if os.environ.get("POLITIEK_PLANNER", "0") != "0":
        planner = asyncio.create_task(taken.planner(pool))
    yield
    if planner:
        planner.cancel()
    pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
BASIS = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASIS, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASIS, "templates"))


@app.middleware("http")
async def alleen_via_authelia(request: Request, call_next):
    """Zonder Remote-User (dus niet via Caddy en Authelia) geen toegang, behalve tot een paar open paden."""
    pad = request.url.path
    if not pad.startswith(OPEN_PADEN):
        if PROXY_GEHEIM and not hmac.compare_digest(request.headers.get("X-Proxy-Geheim", ""), PROXY_GEHEIM):
            return JSONResponse({"fout": "niet via de proxy"}, status_code=401)
        if not request.headers.get("Remote-User"):
            return JSONResponse({"fout": "niet ingelogd"}, status_code=401)
    return await call_next(request)


# ---------- hulpfuncties ----------

def q(sql, params=None):
    with pool.connection() as c:
        return c.execute(sql, params or ()).fetchall()


def q1(sql, params=None):
    rows = q(sql, params)
    return rows[0] if rows else None


def uitvoeren(sql, params=None):
    with pool.connection() as c:
        c.execute(sql, params or ())


def nu():
    return dt.datetime.now(dt.timezone.utc)


def geleden(t):
    if not t:
        return ""
    s = (nu() - t).total_seconds()
    if s < 3600:
        return f"{max(1, int(s // 60))} min geleden"
    if s < 86400:
        return f"{int(s // 3600)} uur geleden"
    d = int(s // 86400)
    return "gisteren" if d == 1 else f"{d} dagen geleden"


def datum_nl(d):
    """6-10-2026: zonder voorloopnullen, zoals in de Whaller-berichten."""
    return f"{d.day}-{d.month}-{d.year}" if d else ""


def instelling(sleutel, standaard=""):
    r = q1("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel,))
    return (r["waarde"] if r and r["waarde"] is not None else standaard)


def ctx(request, pagina, **kw):
    kw.update(request=request, pagina=pagina, menu=schermen.MENU, auth_url=AUTH_URL,
              gebruiker=request.headers.get("Remote-Name") or request.headers.get("Remote-User") or "",
              geleden=geleden, datum_nl=datum_nl, portal_host=HOST)
    return kw


def herkomst_toegestaan(origin, referer, toegestaan):
    """Komt een formulier van een van onze eigen adressen? Vergelijkt het adres exact, niet als stukje tekst."""
    herkomst = origin or referer or ""
    if not herkomst or herkomst == "null":
        return False
    adres = urlsplit(herkomst)
    return adres.scheme == "https" and (adres.hostname or "").lower() in toegestaan


def controleer_herkomst(request: Request):
    """Bescherming tegen verzoeken vanaf andere sites: Origin (of anders Referer) moet exact een eigen adres zijn."""
    if not herkomst_toegestaan(request.headers.get("origin"), request.headers.get("referer"), TOEGESTANE_HOSTS):
        raise HTTPException(403, "Onbekende herkomst")


def tellers_kennisbank():
    r = q1("""SELECT count(*) AS documenten,
                     count(*) FILTER (WHERE soort = 'bron') AS bronnen,
                     count(*) FILTER (WHERE soort = 'bijdrage') AS bijdragen,
                     coalesce(sum(woorden), 0) AS woorden
              FROM documenten""")
    return r or {"documenten": 0, "bronnen": 0, "bijdragen": 0, "woorden": 0}


# ---------- schermen ----------

@app.get("/", response_class=HTMLResponse)
def vandaag(request: Request):
    tel = tellers_kennisbank()
    personen = q1("SELECT count(*) AS n FROM personen WHERE actief")["n"]
    links_leeg = q1("""SELECT count(*) AS n FROM instellingen
                       WHERE sleutel = ANY(%s) AND coalesce(waarde, '') = ''""", (WHALLER_NODIG,))["n"]
    inbox = q1("""SELECT count(*) FILTER (WHERE NOT gezien) AS nieuw,
                         count(*) FILTER (WHERE NOT gezien AND pro) AS pro
                  FROM stukken WHERE soort NOT IN ('bijlage', 'nieuws', 'besloten') AND status <> 'genegeerd'
                    AND NOT (soort = 'vraag' AND pro)""")
    # Vergaderingen (ook die alleen voor raadsleden) staan bij 'Komende twee weken', niet hier.
    # Eigen raadsvragen van PRO staan bij Raadsvragen.
    recent = q("""SELECT * FROM stukken WHERE NOT gezien AND bron = 'notubiz' AND soort NOT IN ('bijlage', 'besloten')
                    AND NOT (soort = 'vraag' AND pro)
                  ORDER BY pro DESC, datum DESC NULLS LAST, id DESC LIMIT 8""")
    nieuws_top = q("""SELECT * FROM stukken WHERE bron = 'nieuws' AND score >= 60 AND status <> 'genegeerd'
                        AND toegevoegd > now() - interval '24 hours' ORDER BY score DESC, id DESC LIMIT 6""")
    vragen = vragen_tellers()
    deadlines = []
    try:
        verg, _ = agenda_overzicht(3)
        deadlines = agenda.mijn_deadlines(verg, instelling("agenda_ik"), dagen=7)
    except Exception:  # noqa: BLE001 - Vandaag moet altijd laden
        pass
    voornaam = _mijn_voornaam(_instellingen())
    mijn_acties = q("""SELECT * FROM fractie_acties WHERE status <> 'afgerond' AND %s <> '' AND wie ILIKE %s
                        ORDER BY id DESC LIMIT 8""", (voornaam, f"%{voornaam}%"))
    komend = []
    try:
        vandaag_d = dt.date.today()
        komend = markeer_besloten(notubiz.evenementen(vandaag_d, vandaag_d + dt.timedelta(days=14)))
    except Exception:  # noqa: BLE001 - Vandaag moet altijd laden
        pass
    return templates.TemplateResponse("vandaag.html", ctx(
        request, "vandaag", tel=tel, personen=personen, links_leeg=links_leeg, fasen=schermen.FASEN,
        inbox=inbox, recent=recent, komend=komend, nieuws_top=nieuws_top, vragen=vragen, deadlines=deadlines, mijn_acties=mijn_acties, soorten=raadsinfo.SOORTEN, lang=whaller.lang))


@app.get("/dossiers", response_class=HTMLResponse)
def dossiers(request: Request, zoek: str = "", soort: str = "", archief: int = 0):
    resultaten = kennisbank.zoeken(q, zoek, soort) if zoek.strip() else []
    lijst = q("""SELECT id, titel, soort, pad, woorden, toegevoegd, vertrouwelijk FROM documenten
                 WHERE (%s = '' OR soort = %s) ORDER BY soort, pad NULLS LAST, titel""", (soort, soort))
    kaarten = q("""SELECT id, naam, zoektermen, woordvoerder, status, inzicht->>'samenvatting' AS samenvatting,
                          analyse_tijd, bijgewerkt FROM dossiers
                   WHERE status = %s ORDER BY naam""", ("archief" if archief else "actief",))
    with pool.connection() as c:
        voorstel = dossierkaarten.voorstellen(c, [k["naam"] for k in q("SELECT naam FROM dossiers")])
    return templates.TemplateResponse("dossiers.html", ctx(
        request, "dossiers", zoek=zoek, soort=soort, resultaten=resultaten, lijst=lijst, kaarten=kaarten,
        voorstel=voorstel, archief=archief, personen=_personen_whaller(),
        tel=tellers_kennisbank(), soorten=kennisbank.SOORTEN, melding=request.query_params.get("melding", "")))


@app.post("/dossiers/kaart")
async def dossier_nieuw(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    naam = re.sub(r"\s+", " ", str(form.get("naam", ""))).strip()[:120]
    if not naam:
        return RedirectResponse("/dossiers", status_code=303)
    bestaand = q1("SELECT id FROM dossiers WHERE lower(naam) = lower(%s)", (naam,))
    if bestaand:
        return RedirectResponse(f"/dossiers/kaart/{bestaand['id']}", status_code=303)
    r = q1("INSERT INTO dossiers (naam, zoektermen, woordvoerder) VALUES (%s, %s, %s) RETURNING id",
           (naam, str(form.get("zoektermen", "")).strip() or naam, str(form.get("woordvoerder", "")).strip()))
    return RedirectResponse(f"/dossiers/kaart/{r['id']}", status_code=303)


@app.get("/dossiers/kaart/{did}", response_class=HTMLResponse)
def dossier_kaart(request: Request, did: int):
    d = q1("SELECT * FROM dossiers WHERE id = %s", (did,))
    if not d:
        raise HTTPException(404)
    with pool.connection() as c:
        v = dossierkaarten.verzamel(c, d)
    return templates.TemplateResponse("dossier.html", ctx(
        request, "dossiers", d=d, a=d["inzicht"] or {}, v=v, personen=_personen_whaller(),
        statussen=fractie.STATUS_NAAM, soorten=kennisbank.SOORTEN))


@app.post("/dossiers/kaart/{did}/wijzig")
async def dossier_wijzig(request: Request, did: int):
    controleer_herkomst(request)
    form = await request.form()
    naam = re.sub(r"\s+", " ", str(form.get("naam", ""))).strip()[:120]
    if not naam:
        raise HTTPException(400)
    uitvoeren("""UPDATE dossiers SET naam = %s, zoektermen = %s, woordvoerder = %s, standpunt = %s, notitie = %s,
                   bijgewerkt = now() WHERE id = %s""",
              (naam, str(form.get("zoektermen", "")).strip(), str(form.get("woordvoerder", "")).strip(),
               str(form.get("standpunt", "")).strip(), str(form.get("notitie", "")).strip(), did))
    return RedirectResponse(f"/dossiers/kaart/{did}", status_code=303)


@app.post("/dossiers/kaart/{did}/analyse")
async def dossier_analyse(request: Request, did: int):
    controleer_herkomst(request)
    form = await request.form()
    uitvoeren("UPDATE dossiers SET analyse_status = 'bezig', analyse_fout = NULL WHERE id = %s", (did,))
    dossierkaarten.start(pool, did, form.get("vertrouwelijk") == "1")
    return RedirectResponse(f"/dossiers/kaart/{did}", status_code=303)


@app.post("/dossiers/kaart/{did}/{wat}")
def dossier_status(request: Request, did: int, wat: str):
    controleer_herkomst(request)
    if wat == "archief":
        uitvoeren("UPDATE dossiers SET status = 'archief', bijgewerkt = now() WHERE id = %s", (did,))
    elif wat == "actief":
        uitvoeren("UPDATE dossiers SET status = 'actief', bijgewerkt = now() WHERE id = %s", (did,))
    elif wat == "weg":
        uitvoeren("DELETE FROM dossiers WHERE id = %s", (did,))
        return RedirectResponse("/dossiers?" + urlencode({"melding": "Dossier verwijderd. De stukken zelf blijven bewaard."}),
                                status_code=303)
    else:
        raise HTTPException(400)
    return RedirectResponse(f"/dossiers/kaart/{did}", status_code=303)


@app.get("/dossiers/document/{did}", response_class=HTMLResponse)
def document(request: Request, did: int, zoek: str = ""):
    d = q1("SELECT * FROM documenten WHERE id = %s", (did,))
    if not d:
        raise HTTPException(404)
    return templates.TemplateResponse("document.html", ctx(request, "dossiers", d=d, zoek=zoek))


@app.post("/dossiers/upload")
async def upload(request: Request, bestanden: list[UploadFile] = File(...), soort: str = Form("bron"),
                 vertrouwelijk: str = Form("")):
    controleer_herkomst(request)
    if soort not in kennisbank.SOORTEN:
        raise HTTPException(400, "Onbekende soort")
    toegevoegd, vertrouwelijk_n, overgeslagen = 0, 0, []
    for b in bestanden:
        inhoud = await b.read()
        if len(inhoud) > MAX_UPLOAD:
            overgeslagen.append(f"{b.filename} (te groot)")
            continue
        stukken, fouten = kennisbank.lees_upload(b.filename or "bestand", inhoud, soort, bool(vertrouwelijk))
        overgeslagen += fouten
        for stuk in stukken:
            if not stuk["tekst"].strip():
                overgeslagen.append(f"{os.path.basename(stuk['pad'])} (geen tekst)")
                continue
            uitvoeren("""INSERT INTO documenten (titel, soort, pad, tekst, woorden, bron_bestand, vertrouwelijk)
                         VALUES (%s, %s, %s, %s, %s, %s, %s)
                         ON CONFLICT (pad) DO UPDATE SET titel = EXCLUDED.titel, soort = EXCLUDED.soort,
                           tekst = EXCLUDED.tekst, woorden = EXCLUDED.woorden, bron_bestand = EXCLUDED.bron_bestand,
                           vertrouwelijk = documenten.vertrouwelijk OR EXCLUDED.vertrouwelijk,
                           bijgewerkt = now()""",
                      (stuk["titel"], stuk["soort"], stuk["pad"], stuk["tekst"], stuk["woorden"],
                       b.filename, stuk["vertrouwelijk"]))
            toegevoegd += 1
            vertrouwelijk_n += stuk["vertrouwelijk"]
    melding = f"{toegevoegd} document(en) toegevoegd of bijgewerkt"
    melding += f", waarvan {vertrouwelijk_n} vertrouwelijk." if vertrouwelijk_n else "."
    if overgeslagen:
        melding += f" Overgeslagen ({len(overgeslagen)}): " + ", ".join(overgeslagen[:10])
        melding += " en meer." if len(overgeslagen) > 10 else "."
    return RedirectResponse("/dossiers?" + urlencode({"melding": melding}), status_code=303)


@app.post("/dossiers/document/{did}/weg")
def document_weg(request: Request, did: int):
    controleer_herkomst(request)
    uitvoeren("DELETE FROM documenten WHERE id = %s", (did,))
    return RedirectResponse("/dossiers?" + urlencode({"melding": "Document verwijderd."}), status_code=303)


@app.get("/instellingen", response_class=HTMLResponse)
def instellingen(request: Request):
    personen = q("SELECT * FROM personen ORDER BY actief DESC, volgorde, naam")
    inst = q("SELECT * FROM instellingen ORDER BY sleutel")
    groepen = schermen.groepeer_instellingen(inst, list(STANDAARD_INSTELLINGEN))
    return templates.TemplateResponse("instellingen.html", ctx(
        request, "instellingen", personen=personen, groepen=groepen, nodig=WHALLER_NODIG,
        taken=q("SELECT * FROM taken ORDER BY naam"),
        melding=request.query_params.get("melding", "")))


@app.post("/instellingen")
async def instellingen_opslaan(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    for sleutel, waarde in form.items():
        if sleutel.startswith("inst_"):
            uitvoeren("UPDATE instellingen SET waarde = %s, bijgewerkt = now() WHERE sleutel = %s",
                      (str(waarde).strip(), sleutel[5:]))
    return RedirectResponse("/instellingen?" + urlencode({"melding": "Instellingen opgeslagen."}), status_code=303)


@app.post("/instellingen/persoon")
def persoon_opslaan(request: Request, pid: int = Form(0), naam: str = Form(...), whaller: str = Form(""),
                    rol: str = Form(""), portefeuilles: str = Form(""), volgorde: int = Form(50),
                    actief: str = Form("")):
    controleer_herkomst(request)
    whaller = whaller.strip().lstrip("@")
    if pid:
        uitvoeren("""UPDATE personen SET naam=%s, whaller=%s, rol=%s, portefeuilles=%s, volgorde=%s, actief=%s
                     WHERE id=%s""", (naam.strip(), whaller, rol.strip(), portefeuilles.strip(), volgorde, bool(actief), pid))
    else:
        uitvoeren("""INSERT INTO personen (naam, whaller, rol, portefeuilles, volgorde, actief)
                     VALUES (%s, %s, %s, %s, %s, true)""", (naam.strip(), whaller, rol.strip(), portefeuilles.strip(), volgorde))
    return RedirectResponse("/instellingen?" + urlencode({"melding": f"{naam.strip()} opgeslagen."}) + "#personen",
                            status_code=303)


@app.get("/gezond")
def gezond():
    q1("SELECT 1 AS ok")
    return {"ok": True}


# ---------- sjablonen (formats van de griffie) ----------

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _sjabloon(soort):
    """De geüploade versie als die er is, anders None (dan geldt de standaard)."""
    r = q1("SELECT inhoud FROM sjablonen WHERE soort = %s", (soort,))
    return bytes(r["inhoud"]) if r else None


def _word(inhoud, naam):
    return Response(inhoud, media_type=DOCX,
                    headers={"Content-Disposition": f'attachment; filename="{naam}"'})


def _soort_of_404(soort):
    if soort not in sjablonen.SOORTEN:
        raise HTTPException(404)


@app.get("/sjablonen", response_class=HTMLResponse)
def sjablonen_lijst(request: Request):
    eigen = {r["soort"]: r for r in q("SELECT soort, bestandsnaam, bijgewerkt FROM sjablonen")}
    return templates.TemplateResponse("sjablonen.html", ctx(
        request, "bijdragen", soorten=sjablonen.SOORTEN, eigen=eigen,
        melding=request.query_params.get("melding", "")))


@app.get("/sjablonen/{soort}", response_class=HTMLResponse)
def sjabloon_formulier(request: Request, soort: str):
    _soort_of_404(soort)
    return templates.TemplateResponse("sjabloon.html", ctx(
        request, "bijdragen", soort=soort, naam=sjablonen.SOORTEN[soort], velden=sjablonen.VELDEN[soort],
        voorbeeld=sjablonen.VOORBEELD[soort] if request.query_params.get("voorbeeld") else {}))


@app.post("/sjablonen/{soort}")
async def sjabloon_invullen(request: Request, soort: str):
    _soort_of_404(soort)
    controleer_herkomst(request)
    form = await request.form()
    gegevens = {}
    for naam, _label, aard in sjablonen.VELDEN[soort]:
        waarde = str(form.get(naam, "")).replace("\r", "")
        gegevens[naam] = [r for r in waarde.split("\n") if r.strip()] if aard == "regels" else waarde.strip()
    inhoud = sjablonen.vul(soort, gegevens, _sjabloon(soort))
    return _word(inhoud, sjablonen.bestandsnaam(soort, gegevens))


@app.get("/sjablonen/{soort}/leeg")
def sjabloon_leeg(soort: str):
    _soort_of_404(soort)
    return _word(_sjabloon(soort) or sjablonen.standaard(soort), f"Format-{soort}.docx")


@app.get("/sjablonen/{soort}/voorbeeld")
def sjabloon_voorbeeld(soort: str):
    _soort_of_404(soort)
    return _word(sjablonen.vul(soort, sjablonen.VOORBEELD[soort], _sjabloon(soort)), f"Voorbeeld-{soort}.docx")


@app.post("/sjablonen/{soort}/vervang")
async def sjabloon_vervang(request: Request, soort: str, bestand: UploadFile = File(...)):
    _soort_of_404(soort)
    controleer_herkomst(request)
    inhoud = await bestand.read()
    if not bestand.filename.lower().endswith(".docx") or len(inhoud) > 10 * 1024 * 1024:
        melding = "Alleen een Word-bestand (.docx) tot 10 MB."
    elif fout := sjablonen.controleer(soort, inhoud):
        melding = (f"Dit bestand kan Promonitor niet invullen ({fout}). De vaste teksten en invulvelden "
                   "van de griffie moeten erin blijven staan. Er is niets veranderd.")
    else:
        uitvoeren("""INSERT INTO sjablonen (soort, bestandsnaam, inhoud) VALUES (%s, %s, %s)
                     ON CONFLICT (soort) DO UPDATE SET bestandsnaam = EXCLUDED.bestandsnaam,
                     inhoud = EXCLUDED.inhoud, bijgewerkt = now()""", (soort, bestand.filename, inhoud))
        melding = f"Nieuwe versie van '{sjablonen.SOORTEN[soort]}' opgeslagen."
    return RedirectResponse("/sjablonen?" + urlencode({"melding": melding}), status_code=303)


@app.post("/sjablonen/{soort}/standaard")
def sjabloon_standaard(request: Request, soort: str):
    _soort_of_404(soort)
    controleer_herkomst(request)
    uitvoeren("DELETE FROM sjablonen WHERE soort = %s", (soort,))
    return RedirectResponse("/sjablonen?" + urlencode({"melding": "Terug naar de standaardversie."}), status_code=303)


def markeer_besloten(evenementen):
    """Zet alleen_leden ook aan bij bijeenkomsten waarvoor Notubiz eerder 'niet ingelogd' gaf."""
    bekend = {r["extern_id"] for r in q("SELECT extern_id FROM stukken WHERE soort = 'besloten'")}
    for e in evenementen:
        e["alleen_leden"] = e.get("alleen_leden") or f"leden-{e['id']}" in bekend
    return evenementen


# ---------- Inbox (M1, M2) ----------

INBOX_STATUS = {"nieuw": "Nieuw", "bewaard": "Bewaard", "genegeerd": "Genegeerd", "alles": "Alles"}
INBOX_PERIODE = {"": "Elke datum", "week": "Afgelopen week", "maand": "Afgelopen maand", "kwartaal": "Afgelopen drie maanden",
                 "jaar": "Afgelopen jaar", "komend": "Vanaf vandaag (komende vergaderingen)"}
INBOX_DREMPEL = {"0": "Alle nieuws", "40": "Nieuws vanaf score 40", "60": "Nieuws vanaf score 60", "80": "Nieuws vanaf score 80"}
INBOX_SORTERING = {
    "slim": ("PRO en belangrijk bovenaan", "pro DESC, coalesce(bijgewerkt, toegevoegd)::date DESC, (coalesce(score, 60) >= 70) DESC, "
                                            "coalesce(score, 60) DESC, datum DESC NULLS LAST, id DESC"),
    "score": ("Hoogste nieuwsscore eerst", "coalesce(score, -2) DESC, datum DESC NULLS LAST, id DESC"),
    "datum": ("Datum, nieuwste eerst", "datum DESC NULLS LAST, id DESC"),
    "datum_oud": ("Datum, oudste eerst", "datum ASC NULLS LAST, id"),
    "binnen": ("Laatst binnengekomen", "coalesce(bijgewerkt, toegevoegd) DESC, id DESC"),
    "titel": ("Titel A-Z", "lower(titel), id"),
    "soort": ("Soort, dan datum", "soort, datum DESC NULLS LAST, id DESC"),
}


def _inbox_filter(soort="", status="nieuw", pro="", periode="", fractie="", zoek="", drempel="40", zonder=()):
    """WHERE-deel en parameters; 'zonder' laat filters weg (voor de aantallen per soort)."""
    voorwaarden, params = ["true"], []
    if drempel and drempel != "0":
        # Nieuws onder de drempel niet tonen; nog niet gewogen nieuws wel.
        voorwaarden.append("(bron <> 'nieuws' OR score IS NULL OR score >= %s)")
        params.append(int(drempel) if str(drempel).isdigit() else 40)
    if "soort" not in zonder:
        if soort:
            voorwaarden.append("soort = %s")
            params.append(soort)
        else:
            voorwaarden.append("soort <> 'bijlage'")
    if status == "nieuw":
        voorwaarden.append("NOT gezien AND status <> 'genegeerd'")
    elif status in ("bewaard", "genegeerd"):
        voorwaarden.append("status = %s")
        params.append(status)
    if pro:
        voorwaarden.append("pro")
    dagen = {"week": 7, "maand": 31, "kwartaal": 92, "jaar": 366}.get(periode)
    if dagen:
        voorwaarden.append("datum >= current_date - %s")
        params.append(dagen)
    elif periode == "komend":
        voorwaarden.append("datum >= current_date")
    if fractie:
        voorwaarden.append("fracties ILIKE %s")
        params.append(f"%{fractie}%")
    if zoek.strip():
        voorwaarden.append("(titel ILIKE %s OR context ILIKE %s OR nummer ILIKE %s OR fracties ILIKE %s)")
        params += [f"%{zoek.strip()}%"] * 4
    return " AND ".join(voorwaarden), params


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, soort: str = "", status: str = "nieuw", pro: str = "", periode: str = "",
          fractie: str = "", sorteer: str = "slim", zoek: str = "", drempel: str = "40", alles: str = "", pagina_nr: int = 1):
    if alles:  # oude links
        status = "alles"
    status = status if status in INBOX_STATUS else "nieuw"
    sorteer = sorteer if sorteer in INBOX_SORTERING else "slim"
    drempel = drempel if drempel in INBOX_DREMPEL else "40"
    filters = dict(soort=soort, status=status, pro=pro, periode=periode, fractie=fractie, zoek=zoek, drempel=drempel)
    waar, params = _inbox_filter(**filters)
    totaal = q1(f"SELECT count(*) AS n FROM stukken WHERE {waar}", params)["n"]
    rijen = q(f"""SELECT * FROM stukken WHERE {waar} ORDER BY {INBOX_SORTERING[sorteer][1]}
                  LIMIT 50 OFFSET %s""", params + [50 * (max(pagina_nr, 1) - 1)])
    # Aantallen per soort bij de huidige overige filters, voor de snelknoppen.
    waar_s, params_s = _inbox_filter(**filters, zonder=("soort",))
    per_soort = {r["soort"]: r["n"] for r in q(f"SELECT soort, count(*) AS n FROM stukken WHERE {waar_s} GROUP BY soort", params_s)}
    fracties = sorted({f.strip() for r in q("SELECT DISTINCT fracties FROM stukken WHERE fracties <> ''")
                       for f in r["fracties"].split(",") if f.strip()}, key=str.lower)
    taak = q1("SELECT * FROM taken WHERE naam = 'raadsinformatie'")
    taak_nieuws = q1("SELECT * FROM taken WHERE naam = 'nieuws'")
    basis = {k: v for k, v in dict(filters, sorteer=sorteer).items() if v and not (k == "status" and v == "nieuw")
             and not (k == "sorteer" and v == "slim") and not (k == "drempel" and v == "40")}
    return templates.TemplateResponse("inbox.html", ctx(
        request, "inbox", rijen=rijen, totaal=totaal, pagina_nr=pagina_nr, f=dict(filters, sorteer=sorteer),
        soorten=raadsinfo.SOORTEN, per_soort=per_soort, fracties=fracties, statussen=INBOX_STATUS,
        alle_soorten=sum(n for k, n in per_soort.items() if k != "bijlage"),
        periodes=INBOX_PERIODE, drempels=INBOX_DREMPEL, sorteringen={k: v[0] for k, v in INBOX_SORTERING.items()},
        link=lambda **wijzig: "/inbox?" + urlencode({k: v for k, v in dict(basis, **wijzig).items() if v}),
        taak=taak, taak_nieuws=taak_nieuws, melding=request.query_params.get("melding", "")))


@app.post("/inbox/{sid}/{actie}")
async def inbox_actie(request: Request, sid: int, actie: str):
    controleer_herkomst(request)
    form = await request.form()
    if actie == "gezien":
        uitvoeren("UPDATE stukken SET gezien = true WHERE id = %s", (sid,))
    elif actie == "bewaar":
        uitvoeren("UPDATE stukken SET status = 'bewaard', gezien = true WHERE id = %s", (sid,))
    elif actie == "negeer":
        reden = str(form.get("reden", "")).strip()[:300]
        if reden:  # met reden: die gaat mee bij het wegen (bijgewerkt bepaalt welke redenen het nieuwst zijn)
            uitvoeren("""UPDATE stukken SET status = 'genegeerd', gezien = true, negeer_reden = %s, bijgewerkt = now()
                         WHERE id = %s""", (reden, sid))
        else:
            uitvoeren("UPDATE stukken SET status = 'genegeerd', gezien = true WHERE id = %s", (sid,))
    else:
        raise HTTPException(400)
    if request.headers.get("HX-Request"):
        return HTMLResponse("")
    return RedirectResponse(request.headers.get("referer") or "/inbox", status_code=303)


@app.post("/inbox/alles-gezien")
async def inbox_alles_gezien(request: Request):
    """Markeert alles wat met de huidige filters in de lijst 'Nieuw' staat als gezien."""
    controleer_herkomst(request)
    form = await request.form()
    filters = {k: str(form.get(k, "")) for k in ("soort", "pro", "periode", "fractie", "zoek", "drempel")}
    waar, params = _inbox_filter(status="nieuw", **filters)
    uitvoeren(f"UPDATE stukken SET gezien = true WHERE {waar}", params)
    terug = {k: v for k, v in filters.items() if v}
    terug["melding"] = "Als gezien gemarkeerd."
    return RedirectResponse("/inbox?" + urlencode(terug), status_code=303)


@app.post("/inbox/vernieuw")
async def inbox_vernieuw(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    taak = "nieuws" if form.get("wat") == "nieuws" else "raadsinformatie"
    resultaat, fout = await asyncio.to_thread(taken.voer_uit, pool, taak)
    melding = f"Opgehaald: {resultaat}." if not fout else "Ophalen mislukt; zie Instellingen, onderaan."
    return RedirectResponse("/inbox?" + urlencode({"melding": melding}), status_code=303)


# ---------- Filter en bronnen (M1) en de briefing ----------

@app.get("/filter", response_class=HTMLResponse)
def filter_scherm(request: Request):
    from . import nieuws as nw  # pylint: disable=import-outside-toplevel
    bronnen = q("SELECT * FROM bronnen ORDER BY soort <> 'feed', soort, naam")
    redenen = q("""SELECT id, titel, negeer_reden, bijgewerkt FROM stukken WHERE coalesce(negeer_reden, '') <> ''
                   ORDER BY bijgewerkt DESC NULLS LAST, id DESC LIMIT 60""")
    kosten = q1("""SELECT coalesce(sum(kosten_eur) FILTER (WHERE tijd::date = current_date), 0) AS vandaag,
                          coalesce(sum(kosten_eur) FILTER (WHERE tijd > date_trunc('month', now())), 0) AS maand,
                          count(*) FILTER (WHERE tijd::date = current_date) AS aanroepen FROM claude_gebruik""")
    verdeling = q("""SELECT CASE WHEN score IS NULL THEN 'nog niet gewogen' WHEN score < 0 THEN 'te oud'
                                 WHEN score < 40 THEN '0-39' WHEN score < 60 THEN '40-59' WHEN score < 80 THEN '60-79'
                                 ELSE '80-100' END AS groep, count(*) AS n
                     FROM stukken WHERE bron = 'nieuws' AND toegevoegd > now() - interval '7 days' GROUP BY 1 ORDER BY 1""")
    return templates.TemplateResponse("filter.html", ctx(
        request, "filter", bronnen=bronnen, redenen=redenen, kosten=kosten, verdeling=verdeling,
        profiel=instelling("nieuws_profiel") or nw.STANDAARD_PROFIEL, regels=instelling("filter_regels"),
        plafond=instelling("claude_dagplafond_eur", "1.00"), taak=q1("SELECT * FROM taken WHERE naam = 'nieuws'"),
        melding=request.query_params.get("melding", "")))


def _zet_instelling(sleutel, waarde, uitleg):
    uitvoeren("""INSERT INTO instellingen (sleutel, waarde, uitleg) VALUES (%s, %s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()""", (sleutel, waarde, uitleg))


@app.post("/filter/teksten")
async def filter_teksten(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    _zet_instelling("nieuws_profiel", str(form.get("profiel", "")).strip(), "Profiel voor het wegen van nieuws")
    _zet_instelling("filter_regels", str(form.get("regels", "")).strip(), "Eigen regels voor het nieuwsfilter")
    return RedirectResponse("/filter?melding=Opgeslagen.+Dit+geldt+voor+het+nieuws+dat+vanaf+nu+binnenkomt.", status_code=303)


@app.post("/filter/reden/{sid}/weg")
def filter_reden_weg(request: Request, sid: int):
    controleer_herkomst(request)
    uitvoeren("UPDATE stukken SET negeer_reden = NULL WHERE id = %s", (sid,))
    return RedirectResponse("/filter#redenen", status_code=303)


@app.post("/filter/bron")
async def filter_bron(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    actie = form.get("actie")
    if actie == "nieuw":
        naam, url, soort = (str(form.get(k, "")).strip() for k in ("naam", "url", "soort"))
        if naam and url and soort in ("feed", "site", "google"):
            uitvoeren("INSERT INTO bronnen (naam, url, soort) VALUES (%s, %s, %s) ON CONFLICT (url) DO NOTHING",
                      (naam, url, soort))
    elif actie in ("aan", "uit"):
        uitvoeren("UPDATE bronnen SET actief = %s WHERE id = %s", (actie == "aan", int(form.get("id"))))
    elif actie == "weg":
        uitvoeren("DELETE FROM bronnen WHERE id = %s", (int(form.get("id")),))
    return RedirectResponse("/filter#bronnen", status_code=303)


@app.get("/briefing", response_class=HTMLResponse)
def briefing_voorbeeld(request: Request):
    from . import briefing as br  # pylint: disable=import-outside-toplevel
    with pool.connection() as c:
        g = br.gegevens(c)
        onderwerp, html_, _ = br.mail(g)
        tg = br.telegram(g)
    return templates.TemplateResponse("briefing.html", ctx(
        request, "vandaag", onderwerp=onderwerp, mail_html=html_, telegram=tg,
        taak=q1("SELECT * FROM taken WHERE naam = 'briefing'"), tijd=instelling("briefing_tijd", "08.00"),
        melding=request.query_params.get("melding", "")))


@app.post("/briefing/stuur")
async def briefing_stuur(request: Request):
    controleer_herkomst(request)
    from . import briefing as br  # pylint: disable=import-outside-toplevel

    def doe():
        with pool.connection() as c:
            return br.versturen(c)
    uitslag = await asyncio.to_thread(doe)
    return RedirectResponse("/briefing?" + urlencode({"melding": f"Verstuurd: {uitslag}"}), status_code=303)


# ---------- Agenda (M3) ----------

def _personen_whaller():
    return q("""SELECT naam, whaller, rol, portefeuilles FROM personen
                WHERE actief AND coalesce(whaller, '') <> '' ORDER BY volgorde, naam""")


def agenda_overzicht(weken=6):
    vandaag_d = dt.date.today()
    with pool.connection() as c:
        return agenda.overzicht(c, vandaag_d, vandaag_d + dt.timedelta(days=7 * weken), _instellingen(), _personen_whaller())


@app.get("/agenda", response_class=HTMLResponse)
def agenda_scherm(request: Request, wie: str = "", weken: int = 6, vernieuw: int = 0):
    weken = max(1, min(weken, 12))
    if vernieuw:
        agenda.leeg_cache()
    inst = _instellingen()
    ik = inst.get("agenda_ik", "")
    wie = wie or ik or "alle"
    fout = ""
    try:
        vergaderingen, fouten = agenda_overzicht(weken)
    except Exception as e:  # noqa: BLE001
        vergaderingen, fouten = [], []
        fout = f"Notubiz is nu niet bereikbaar ({e}). Probeer het straks opnieuw."
    mijn = sorted([p for v in vergaderingen for p in v["punten"] if agenda.van_mij(p, wie)],
                  key=lambda p: (p["datum"], p.get("tijd") or "")) if wie != "alle" else []
    personen = _personen_whaller()
    return templates.TemplateResponse("agenda.html", ctx(
        request, "agenda", vergaderingen=vergaderingen, fouten=fouten, fout=fout, mijn=mijn, wie=wie, ik=ik,
        weken=weken, personen=personen, raadsleden=[p for p in personen if (p["rol"] or "").startswith("raadslid")], stap_naam=agenda.STAP_NAAM, lang=whaller.lang,
        deadlines=agenda.mijn_deadlines(vergaderingen, wie, dagen=7) if wie != "alle" else [],
        vandaag=dt.date.today(), melding=request.query_params.get("melding", "")))


@app.post("/agenda/toewijzingen")
async def agenda_toewijzingen(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    soort, datum, n = _bewaar_toewijzingen(form)
    melding = (f"Woordvoering opgeslagen ({n} gewijzigd). Het Whaller-bericht is bijgewerkt."
               if n else "Er was niets gewijzigd.")
    if n and _invoer(f"{soort}:{datum.isoformat()}").get("gewijzigd_na_plaatsen"):
        melding += " Let op: dat bericht stond al op Whaller; pas het daar aan."
    terug = f"/agenda?wie={form.get('wie', '')}&weken={form.get('weken', 6)}&" + urlencode({"melding": melding})
    return RedirectResponse(terug + f"#v-{soort}-{datum.isoformat()}", status_code=303)


@app.post("/agenda/stap")
async def agenda_stap(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    onderwerp = str(form.get("onderwerp", "")).strip()
    stap, actie = form.get("stap"), form.get("actie")
    if not onderwerp or stap not in agenda.STAP_NAAM or actie not in ("gedaan", "nvt", "open"):
        raise HTTPException(400)
    sl = whaller.sleutel(onderwerp)
    r = q1("SELECT * FROM agenda_voortgang WHERE sleutel = %s", (sl,)) or {"gedaan": [], "nvt": []}
    gedaan = [x for x in r["gedaan"] if x != stap] + ([stap] if actie == "gedaan" else [])
    nvt = [x for x in r["nvt"] if x != stap] + ([stap] if actie == "nvt" else [])
    uitvoeren("""INSERT INTO agenda_voortgang (sleutel, onderwerp, gedaan, nvt) VALUES (%s, %s, %s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET gedaan = EXCLUDED.gedaan, nvt = EXCLUDED.nvt, bijgewerkt = now()""",
              (sl, onderwerp, Jsonb(gedaan), Jsonb(nvt)))
    return RedirectResponse(f"/agenda?wie={form.get('wie', '')}&weken={form.get('weken', 6)}#p-{sl.replace(' ', '-')[:60]}",
                            status_code=303)


@app.post("/agenda/notitie")
async def agenda_notitie(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    onderwerp = str(form.get("onderwerp", "")).strip()
    if not onderwerp:
        raise HTTPException(400)
    sl = whaller.sleutel(onderwerp)
    uitvoeren("""INSERT INTO agenda_voortgang (sleutel, onderwerp, notitie) VALUES (%s, %s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET notitie = EXCLUDED.notitie, bijgewerkt = now()""",
              (sl, onderwerp, str(form.get("notitie", "")).strip()))
    return RedirectResponse(f"/agenda?wie={form.get('wie', '')}&weken={form.get('weken', 6)}#p-{sl.replace(' ', '-')[:60]}",
                            status_code=303)

# ---------- Fractievergadering (M5) ----------

def _volgende_fractie(inst):
    """Datum van de eerstvolgende fractievergadering (vandaag telt mee)."""
    vandaag_d = dt.date.today()
    dag = (inst.get("fractie_dag") or "maandag").lower()
    doel = whaller.DAGEN.index(dag) if dag in whaller.DAGEN else 0
    return vandaag_d + dt.timedelta(days=(doel - vandaag_d.weekday()) % 7)


def _vorige_fractie(inst):
    d = _volgende_fractie(inst)
    return d if d == dt.date.today() else d - dt.timedelta(days=7)


def _mijn_voornaam(inst):
    r = q1("SELECT naam FROM personen WHERE whaller = %s", (inst.get("agenda_ik", ""),))
    return (r["naam"].split()[0] if r else "")


@app.get("/fractie", response_class=HTMLResponse)
def fractie_scherm(request: Request):
    inst = _instellingen()
    personen = q("SELECT naam, rol FROM personen WHERE actief ORDER BY volgorde, naam")
    per_status = {r["status"]: r["n"] for r in q("SELECT status, count(*) AS n FROM fractie_acties GROUP BY status")}
    notulen = q("""SELECT id, datum, status, bron_naam, aangemaakt, bijgewerkt,
                          (SELECT count(*) FROM fractie_acties a WHERE a.notulen_id = n.id AND a.status <> 'afgerond') AS open
                   FROM notulen n ORDER BY datum DESC, id DESC LIMIT 30""")
    return templates.TemplateResponse("fractie.html", ctx(
        request, "fractie", notulen=notulen, personen=personen, per_status=per_status, statussen=fractie.STATUSSEN,
        volgende=_volgende_fractie(inst), vorige=_vorige_fractie(inst), lang=whaller.lang,
        melding=request.query_params.get("melding", ""), bestandtypes=", ".join(fractie.SOORTEN_BESTAND)))


@app.post("/fractie/notulen")
async def fractie_notulen_maak(request: Request, bestand: UploadFile = File(None)):
    controleer_herkomst(request)
    form = await request.form()
    try:
        datum = dt.date.fromisoformat(str(form.get("datum")))
    except ValueError:
        raise HTTPException(400) from None
    tekst, bron = str(form.get("tekst", "")).strip(), "geplakte tekst"
    if bestand is not None and bestand.filename:
        inhoud = await bestand.read()
        try:
            tekst = fractie.tekst_uit_bestand(bestand.filename, inhoud).strip()
        except Exception as e:  # noqa: BLE001
            return RedirectResponse("/fractie?" + urlencode({"melding": f"Het bestand kon niet worden gelezen: {e}"}), status_code=303)
        bron = bestand.filename
    if len(tekst) < 200:
        return RedirectResponse("/fractie?" + urlencode({"melding": "Voeg het transcript toe (bestand of geplakte tekst)."}),
                                status_code=303)
    alle = [p["naam"] for p in q("SELECT naam FROM personen WHERE actief ORDER BY volgorde, naam")]
    aanwezig = [x for x in form.getlist("aanwezig") if x in alle]
    afwezig = [x for x in alle if x not in aanwezig]
    inst = _instellingen()
    vergaderingen = []
    try:
        maandag = datum - dt.timedelta(days=datum.weekday())
        for ev in notubiz.evenementen(maandag, maandag + dt.timedelta(days=6)):
            if ev["soort"] in ("commissie", "raad"):
                vergaderingen.append((f"{ev['naam']} {whaller.d(ev['start'].date())}", "", []))
    except Exception:  # noqa: BLE001 - de agenda is een hulp, geen voorwaarde
        pass
    agenda_tekst = fractie.agenda_tekst(datum, inst, _invoer(f"fractie:{datum.isoformat()}"), vergaderingen)
    r = q1("""INSERT INTO notulen (datum, plaats, aanwezig, afwezig, voorzitter, notulist, bron_naam, transcript, agenda, aanwijzingen)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
           (datum, str(form.get("plaats", "")).strip(), Jsonb(aanwezig), Jsonb(afwezig),
            str(form.get("voorzitter", "")).strip(), str(form.get("notulist", "")).strip(), bron,
            tekst[:fractie.MAX_TRANSCRIPT], agenda_tekst, str(form.get("aanwijzingen", "")).strip()))
    fractie.start(pool, r["id"])
    return RedirectResponse(f"/fractie/notulen/{r['id']}", status_code=303)


@app.get("/fractie/notulen/{nid}", response_class=HTMLResponse)
def fractie_notulen(request: Request, nid: int):
    n = q1("SELECT * FROM notulen WHERE id = %s", (nid,))
    if not n:
        raise HTTPException(404)
    acties = q("SELECT * FROM fractie_acties WHERE notulen_id = %s ORDER BY id", (nid,))
    return templates.TemplateResponse("notulen.html", ctx(
        request, "fractie", n=n, i=n["inhoud"] or {}, acties=acties, lang=whaller.lang, statussen=fractie.STATUSSEN,
        melding=request.query_params.get("melding", ""),
        transcript_lengte=len(n["transcript"] or "")))


@app.get("/fractie/notulen/{nid}/word")
def fractie_notulen_word(nid: int):
    n = q1("SELECT * FROM notulen WHERE id = %s", (nid,))
    if not n or not n["inhoud"]:
        raise HTTPException(404)
    acties = q("SELECT * FROM fractie_acties WHERE notulen_id = %s ORDER BY id", (nid,))
    return _word(fractie.word(n, acties), fractie.bestandsnaam(n))


@app.post("/fractie/notulen/{nid}/opnieuw")
async def fractie_notulen_opnieuw(request: Request, nid: int):
    controleer_herkomst(request)
    form = await request.form()
    n = q1("SELECT status, transcript FROM notulen WHERE id = %s", (nid,))
    if not n or not n["transcript"]:
        raise HTTPException(400, "Het transcript is er niet meer (de notulen zijn vastgesteld).")
    uitvoeren("""UPDATE notulen SET status = 'bezig', fout = NULL, bijgewerkt = now(),
                   aanwijzingen = coalesce(nullif(%s, ''), aanwijzingen) WHERE id = %s""",
              (str(form.get("aanwijzingen", "")).strip(), nid))
    fractie.start(pool, nid)
    return RedirectResponse(f"/fractie/notulen/{nid}", status_code=303)


@app.post("/fractie/notulen/{nid}/woordvoering")
def fractie_woordvoering(request: Request, nid: int):
    """Zet de verdeling van de woordvoering uit de notulen door naar de Agenda (en daarmee naar Whaller)."""
    controleer_herkomst(request)
    n = q1("SELECT inhoud FROM notulen WHERE id = %s", (nid,))
    rijen = ((n or {}).get("inhoud") or {}).get("woordvoering") or []
    personen = q("SELECT naam, whaller, rol FROM personen WHERE actief AND coalesce(whaller, '') <> ''")
    try:
        vergaderingen, _ = agenda_overzicht(6)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/fractie/notulen/{nid}?" + urlencode({"melding": f"Notubiz is nu niet bereikbaar ({e})."}),
                                status_code=303)
    gelukt, niet = [], []
    for r in rijen:
        wie = agenda.persoon_bij_naam(r.get("woordvoerder"), personen)
        punt = agenda.zoek_punt(r.get("agendapunt"), r.get("vergadering"), vergaderingen)
        if not wie or not punt:
            niet.append(f"{r.get('agendapunt')} ({'naam onbekend' if not wie else 'niet op de agenda gevonden'})")
            continue
        if punt["soort"] == "raad" and wie["whaller"] not in {p["whaller"] for p in personen if (p["rol"] or "").startswith("raadslid")}:
            niet.append(f"{r.get('agendapunt')} ({wie['naam']} is geen raadslid)")
            continue
        oud = q1("SELECT zit_erbij FROM toewijzingen WHERE soort = %s AND datum = %s AND sleutel = %s",
                 (punt["soort"], punt["datum"], whaller.sleutel(punt["onderwerp"])))
        uitvoeren("""INSERT INTO toewijzingen (soort, datum, sleutel, onderwerp, woordvoerder, zit_erbij)
                     VALUES (%s, %s, %s, %s, %s, %s)
                     ON CONFLICT (soort, datum, sleutel) DO UPDATE SET woordvoerder = EXCLUDED.woordvoerder,
                       bijgewerkt = now()""",
                  (punt["soort"], punt["datum"], whaller.sleutel(punt["onderwerp"]), punt["onderwerp"], wie["whaller"],
                   (oud or {}).get("zit_erbij") or ""))
        _markeer_whaller(punt["soort"], punt["datum"])
        gelukt.append(f"{punt['onderwerp']}: {wie['naam']}")
    melding = f"{len(gelukt)} doorgezet naar Agenda en Whaller" + (": " + "; ".join(gelukt) if gelukt else "") + "."
    if niet:
        melding += " Niet gelukt: " + "; ".join(niet) + "."
    return RedirectResponse(f"/fractie/notulen/{nid}?" + urlencode({"melding": melding}), status_code=303)


@app.post("/fractie/notulen/{nid}/vastgesteld")
def fractie_notulen_vastgesteld(request: Request, nid: int):
    controleer_herkomst(request)
    uitvoeren("UPDATE notulen SET status = 'vastgesteld', transcript = NULL, bijgewerkt = now() WHERE id = %s AND status = 'klaar'",
              (nid,))
    return RedirectResponse(f"/fractie/notulen/{nid}", status_code=303)


@app.post("/fractie/notulen/{nid}/weg")
def fractie_notulen_weg(request: Request, nid: int):
    controleer_herkomst(request)
    uitvoeren("DELETE FROM fractie_acties WHERE notulen_id = %s", (nid,))
    uitvoeren("DELETE FROM notulen WHERE id = %s", (nid,))
    return RedirectResponse("/fractie?" + urlencode({"melding": "Notulen en hun acties verwijderd."}), status_code=303)


@app.post("/fractie/actie")
async def fractie_actie_nieuw(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    actie = str(form.get("actie", "")).strip()
    if actie:
        uitvoeren("INSERT INTO fractie_acties (actie, wie, wanneer) VALUES (%s, %s, %s)",
                  (actie, str(form.get("wie", "")).strip(), str(form.get("wanneer", "")).strip()))
    return RedirectResponse(str(form.get("terug") or "/acties"), status_code=303)


@app.post("/fractie/actie/{aid}/{wat}")
async def fractie_actie_wijzig(request: Request, aid: int, wat: str):
    controleer_herkomst(request)
    form = await request.form()
    if wat == "status":
        status = form.get("status")
        if status not in fractie.STATUS_NAAM:
            raise HTTPException(400)
        uitvoeren("""UPDATE fractie_acties SET status = %s, gewijzigd = now(),
                       afgerond = CASE WHEN %s = 'afgerond' THEN coalesce(afgerond, now()) END,
                       wacht_op = CASE WHEN %s = 'wachten' THEN coalesce(nullif(%s, ''), wacht_op) ELSE wacht_op END
                     WHERE id = %s""", (status, status, status, str(form.get("wacht_op", "")).strip(), aid))
    elif wat == "weg":
        uitvoeren("DELETE FROM fractie_acties WHERE id = %s", (aid,))
    elif wat == "wijzig":
        uitvoeren("""UPDATE fractie_acties SET actie = %s, wie = %s, wanneer = %s, wacht_op = %s, bewerkt = true,
                       gewijzigd = now() WHERE id = %s""",
                  (str(form.get("actie", "")).strip(), str(form.get("wie", "")).strip(),
                   str(form.get("wanneer", "")).strip(), str(form.get("wacht_op", "")).strip() or None, aid))
    else:
        raise HTTPException(400)
    if request.headers.get("x-promonitor") == "bord":
        return JSONResponse({"ok": True})
    return RedirectResponse(str(form.get("terug") or "/acties"), status_code=303)


# ---------- Acties en besluiten ----------

def _besluiten(zoek=""):
    """Alle besluiten uit de notulen, nieuwste vergadering eerst."""
    uit = []
    for n in q("SELECT id, datum, status, inhoud FROM notulen WHERE inhoud IS NOT NULL ORDER BY datum DESC, id DESC"):
        for p in (n["inhoud"] or {}).get("punten", []):
            for b in p.get("besluiten") or []:
                if zoek and zoek.lower() not in (b + " " + (p.get("titel") or "")).lower():
                    continue
                uit.append({"datum": n["datum"], "notulen_id": n["id"], "vastgesteld": n["status"] == "vastgesteld",
                            "punt": p.get("titel") or "", "besluit": b})
    return uit


@app.get("/acties", response_class=HTMLResponse)
def acties_scherm(request: Request, wie: str = "", zoek: str = "", afgerond: str = "recent"):
    inst = _instellingen()
    voornaam = _mijn_voornaam(inst)
    filter_wie = voornaam if wie == "ik" else ("" if wie in ("", "alle") else wie)
    rijen = q("""SELECT a.*, n.datum FROM fractie_acties a LEFT JOIN notulen n ON n.id = a.notulen_id
                 WHERE (%s = '' OR a.wie ILIKE %s)
                   AND (%s = '' OR a.actie ILIKE %s OR coalesce(a.punt, '') ILIKE %s OR coalesce(a.wacht_op, '') ILIKE %s)
                   AND (a.status <> 'afgerond' OR %s = 'alles' OR coalesce(a.afgerond, a.gewijzigd) > now() - interval '30 days')
                 ORDER BY a.gewijzigd DESC, a.id DESC""",
              (filter_wie, f"%{filter_wie}%", zoek, f"%{zoek}%", f"%{zoek}%", f"%{zoek}%", afgerond))
    kolommen = [(k, naam, [r for r in rijen if r["status"] == k]) for k, naam in fractie.STATUSSEN]
    personen = q("SELECT naam FROM personen WHERE actief ORDER BY volgorde, naam")
    return templates.TemplateResponse("acties.html", ctx(
        request, "acties", kolommen=kolommen, statussen=fractie.STATUSSEN, besluiten=_besluiten(zoek), wie=wie or "alle",
        zoek=zoek, afgerond=afgerond, voornaam=voornaam, personen=personen, terug=str(request.url.path) + (
            "?" + request.url.query if request.url.query else "")))

# ---------- Stemadvies ----------

@app.get("/stemadvies", response_class=HTMLResponse)
def stemadvies_scherm(request: Request):
    fout, raden = "", []
    try:
        vergaderingen, _ = agenda_overzicht(8)
        raden = [v for v in vergaderingen if v["soort"] == "raad" and v["type"] == "meeting" and not v["alleen_leden"]]
    except Exception as e:  # noqa: BLE001
        fout = f"Notubiz is nu niet bereikbaar ({e})."
    bestaand = q("SELECT id, notubiz_id, datum, titel, status, bijgewerkt FROM stemadviezen ORDER BY datum DESC LIMIT 30")
    per_id = {b["notubiz_id"]: b for b in bestaand}
    return templates.TemplateResponse("stemadviezen.html", ctx(
        request, "agenda", raden=raden, bestaand=bestaand, per_id=per_id, fout=fout, lang=whaller.lang,
        melding=request.query_params.get("melding", "")))


@app.post("/stemadvies/maak")
async def stemadvies_maak(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    try:
        nid = int(form.get("notubiz_id"))
    except (TypeError, ValueError):
        raise HTTPException(400) from None
    personen = q("SELECT naam, whaller, rol FROM personen WHERE actief AND coalesce(whaller, '') <> ''")
    try:
        with pool.connection() as c:
            sid = stemadvies.nieuw(c, nid, personen)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse("/stemadvies?" + urlencode({"melding": f"De agenda kon niet worden gelezen: {e}"}), status_code=303)
    if form.get("claude"):
        uitvoeren("UPDATE stemadviezen SET status = 'bezig', fout = NULL WHERE id = %s", (sid,))
        stemadvies.start(pool, sid)
    return RedirectResponse(f"/stemadvies/{sid}", status_code=303)


@app.get("/stemadvies/{sid}", response_class=HTMLResponse)
def stemadvies_detail(request: Request, sid: int):
    s = q1("SELECT * FROM stemadviezen WHERE id = %s", (sid,))
    if not s:
        raise HTTPException(404)
    return templates.TemplateResponse("stemadvies.html", ctx(
        request, "agenda", s=s, adviezen=stemadvies.ADVIEZEN, advies_naam=stemadvies.ADVIES_NAAM, lang=whaller.lang,
        melding=request.query_params.get("melding", "")))


@app.post("/stemadvies/{sid}/opslaan")
async def stemadvies_opslaan(request: Request, sid: int):
    controleer_herkomst(request)
    form = await request.form()
    s = q1("SELECT items FROM stemadviezen WHERE id = %s", (sid,))
    if not s:
        raise HTTPException(404)
    items = s["items"] or []
    for p in items:
        i = str(p["punt_id"])
        if form.get(f"advies_{i}") in stemadvies.ADVIES_NAAM:
            p["advies"] = form.get(f"advies_{i}")
        p["motivering"] = str(form.get(f"motivering_{i}", p.get("motivering", ""))).strip()
        p["stemverklaring"] = str(form.get(f"stemverklaring_{i}", p.get("stemverklaring", ""))).strip()
    status = "vastgesteld" if form.get("vastgesteld") else None
    uitvoeren("""UPDATE stemadviezen SET items = %s, status = coalesce(%s, CASE WHEN status = 'leeg' THEN 'concept' ELSE status END),
                   bijgewerkt = now() WHERE id = %s""", (Jsonb(items), status, sid))
    return RedirectResponse(f"/stemadvies/{sid}?" + urlencode({"melding": "Opgeslagen."}), status_code=303)


@app.post("/stemadvies/{sid}/{wat}")
def stemadvies_actie(request: Request, sid: int, wat: str):
    controleer_herkomst(request)
    s = q1("SELECT notubiz_id FROM stemadviezen WHERE id = %s", (sid,))
    if not s:
        raise HTTPException(404)
    if wat == "claude":
        uitvoeren("UPDATE stemadviezen SET status = 'bezig', fout = NULL WHERE id = %s", (sid,))
        stemadvies.start(pool, sid)
    elif wat == "vernieuw":
        personen = q("SELECT naam, whaller, rol FROM personen WHERE actief AND coalesce(whaller, '') <> ''")
        with pool.connection() as c:
            stemadvies.nieuw(c, s["notubiz_id"], personen)
    elif wat == "concept":
        uitvoeren("UPDATE stemadviezen SET status = 'concept' WHERE id = %s", (sid,))
    elif wat == "weg":
        uitvoeren("DELETE FROM stemadviezen WHERE id = %s", (sid,))
        return RedirectResponse("/stemadvies", status_code=303)
    else:
        raise HTTPException(400)
    return RedirectResponse(f"/stemadvies/{sid}", status_code=303)


@app.get("/stemadvies/{sid}/word")
def stemadvies_word(sid: int):
    s = q1("SELECT * FROM stemadviezen WHERE id = %s", (sid,))
    if not s:
        raise HTTPException(404)
    return _word(stemadvies.word(s), stemadvies.bestandsnaam(s))

# ---------- Begrotingshulp ----------

BEGROTING_WOORDEN = ("begroting", "jaarrekening", "voorjaarsnota", "najaarsnota", "investering", "kadernota",
                     "bestuursrapportage", "burap", "meerjaren", "financ")


@app.get("/begroting", response_class=HTMLResponse)
def begroting_scherm(request: Request):
    docs = q("""SELECT id, titel, soort, pad, woorden FROM documenten
                WHERE soort IN ('bron', 'raadsstuk') AND NOT vertrouwelijk ORDER BY titel""")
    for d in docs:
        d["financieel"] = any(w in (d["titel"] + " " + (d["pad"] or "")).lower() for w in BEGROTING_WOORDEN)
    docs.sort(key=lambda d: (not d["financieel"], d["titel"]))
    analyses = q("SELECT id, titel, thema, status, bijgewerkt FROM begrotingsanalyses ORDER BY id DESC LIMIT 30")
    return templates.TemplateResponse("begroting.html", ctx(
        request, "agenda", docs=docs, analyses=analyses, melding=request.query_params.get("melding", "")))


@app.post("/begroting")
async def begroting_maak(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    thema = str(form.get("thema", "")).strip()
    ids = [int(x) for x in form.getlist("doc") if str(x).isdigit()]
    if not thema or not ids:
        return RedirectResponse("/begroting?" + urlencode({"melding": "Geef een thema en kies minstens één document."}),
                                status_code=303)
    r = q1("INSERT INTO begrotingsanalyses (titel, thema, vraag, documenten) VALUES (%s, %s, %s, %s) RETURNING id",
           (thema[:120], thema, str(form.get("vraag", "")).strip(), Jsonb(ids)))
    begroting.start(pool, r["id"])
    return RedirectResponse(f"/begroting/{r['id']}", status_code=303)


@app.get("/begroting/{bid}", response_class=HTMLResponse)
def begroting_detail(request: Request, bid: int):
    b = q1("SELECT * FROM begrotingsanalyses WHERE id = %s", (bid,))
    if not b:
        raise HTTPException(404)
    docs = q("SELECT id, titel FROM documenten WHERE id = ANY(%s)", ([int(x) for x in b["documenten"]] or [0],))
    return templates.TemplateResponse("begrotingsanalyse.html", ctx(
        request, "agenda", b=b, i=b["inzicht"] or {}, docs=docs, euro=begroting.euro))


@app.get("/begroting/{bid}/word")
def begroting_word(bid: int):
    b = q1("SELECT * FROM begrotingsanalyses WHERE id = %s", (bid,))
    if not b or not b["inzicht"]:
        raise HTTPException(404)
    return _word(begroting.word(b), begroting.bestandsnaam(b))


@app.post("/begroting/{bid}/{wat}")
def begroting_actie(request: Request, bid: int, wat: str):
    controleer_herkomst(request)
    b = q1("SELECT * FROM begrotingsanalyses WHERE id = %s", (bid,))
    if not b:
        raise HTTPException(404)
    if wat == "opnieuw":
        uitvoeren("UPDATE begrotingsanalyses SET status = 'bezig', fout = NULL WHERE id = %s", (bid,))
        begroting.start(pool, bid)
    elif wat == "vragen":
        invoer = {"aanwijzingen": "Maak technische vragen over de begroting op basis van de vragen en opvallende punten hieronder. "
                                  "Noem bij elke vraag het document en de pagina.",
                  "tekst": begroting.vragen_tekst(b)}
        r = q1("INSERT INTO concepten (soort, titel, bron, invoer) VALUES ('technische_vraag', %s, %s, %s) RETURNING id",
               (f"Technische vragen: {b['titel']}"[:200], Jsonb({"type": "idee", "onderwerp": b["thema"]}), Jsonb(invoer)))
        schrijven.start(pool, r["id"])
        return RedirectResponse(f"/concepten/{r['id']}", status_code=303)
    elif wat == "weg":
        uitvoeren("DELETE FROM begrotingsanalyses WHERE id = %s", (bid,))
        return RedirectResponse("/begroting", status_code=303)
    else:
        raise HTTPException(400)
    return RedirectResponse(f"/begroting/{bid}", status_code=303)

# ---------- Dashboard (M13) ----------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_scherm(request: Request, maand: str = ""):
    vandaag_d = dt.date.today()
    try:
        jaar, nr = (int(x) for x in maand.split("-")) if maand else (vandaag_d.year, vandaag_d.month)
        dt.date(jaar, nr, 1)
    except ValueError:
        jaar, nr = vandaag_d.year, vandaag_d.month
    with pool.connection() as c:
        w = dashboard.week(c)
        m = dashboard.maand(c, jaar, nr)
    vorige = (dt.date(jaar, nr, 1) - dt.timedelta(days=1))
    volgende = dt.date(jaar + (nr == 12), nr % 12 + 1, 1)
    taken_rijen = {r["naam"]: r for r in q("SELECT * FROM taken WHERE naam IN ('weekvooruitblik', 'maandoverzicht')")}
    return templates.TemplateResponse("dashboard.html", ctx(
        request, "dashboard", w=w, m=m, lang=whaller.lang, dagen=whaller.DAGEN, euro=dashboard.euro,
        statussen=fractie.STATUS_NAAM, vorige=f"{vorige.year}-{vorige.month:02d}",
        volgende=f"{volgende.year}-{volgende.month:02d}" if volgende <= vandaag_d else "",
        taken=taken_rijen, melding=request.query_params.get("melding", "")))


@app.post("/dashboard/stuur/{wat}")
def dashboard_stuur(request: Request, wat: str):
    controleer_herkomst(request)
    if wat not in ("weekvooruitblik", "maandoverzicht"):
        raise HTTPException(400)
    with pool.connection() as c:
        uitslag = dashboard.stuur_week(c) if wat == "weekvooruitblik" else dashboard.stuur_maand(c)
    return RedirectResponse("/dashboard?" + urlencode({"melding": f"{wat.capitalize()}: {uitslag}."}), status_code=303)

# ---------- Raadsvragen van PRO ----------

VRAGEN_FILTER = {"open": "Nog geen antwoord", "laat": "Over de termijn", "beantwoord": "Beantwoord", "alles": "Alles"}


def _vraag_status(r):
    """open, laat of beantwoord, met de verwachte datum."""
    x = r["extra"] or {}
    beantwoord = x.get("beantwoord") if x else "beantwoord" in (r["context"] or "")
    verwacht = x.get("verwacht") or (re.search(r"antwoord verwacht (\d{4}-\d{2}-\d{2})", r["context"] or "") or [None, ""])[1]
    if beantwoord:
        return "beantwoord", verwacht
    if verwacht and verwacht < dt.date.today().isoformat():
        return "laat", verwacht
    return "open", verwacht


def pro_vragen():
    rijen = q("""SELECT * FROM stukken WHERE bron = 'notubiz' AND soort = 'vraag' AND pro
                 ORDER BY datum DESC NULLS LAST, nummer DESC, id DESC""")
    for r in rijen:
        r["vstatus"], r["verwacht"] = _vraag_status(r)
        x = r["extra"] or {}
        r["vraag_url"] = x.get("vraag_url") or (r["document_url"] if r["vstatus"] != "beantwoord" else "")
        r["antwoord_url"] = x.get("antwoord_url") or (r["document_url"] if r["vstatus"] == "beantwoord" else "")
        r["datum_antwoord"] = x.get("datum_antwoord", "")
        r["categorie"] = x.get("categorie", "")
        r["verwacht_nl"], r["antwoord_nl"] = _iso_nl(r["verwacht"]), _iso_nl(r["datum_antwoord"])
    return rijen


def _iso_nl(t):
    try:
        return datum_nl(dt.date.fromisoformat(t[:10])) if t else ""
    except ValueError:
        return t


def vragen_tellers():
    rijen = pro_vragen()
    return {"open": sum(r["vstatus"] in ("open", "laat") for r in rijen), "laat": sum(r["vstatus"] == "laat" for r in rijen)}


@app.get("/vragen", response_class=HTMLResponse)
def vragen_scherm(request: Request, toon: str = "open", jaar: str = ""):
    alle = pro_vragen()
    jaren = sorted({str(r["datum"].year) for r in alle if r["datum"]}, reverse=True)
    rijen = [r for r in alle if (toon == "alles" or r["vstatus"] == toon or (toon == "open" and r["vstatus"] == "laat"))
             and (not jaar or (r["datum"] and str(r["datum"].year) == jaar))]
    if toon in ("open", "laat"):
        rijen.sort(key=lambda r: r["verwacht"] or "9999")
    tellers = {k: sum(1 for r in alle if r["vstatus"] == k) for k in ("open", "laat", "beantwoord")}
    tellers["open"] += tellers["laat"]
    tellers["alles"] = len(alle)
    return templates.TemplateResponse("vragen.html", ctx(
        request, "vragen", rijen=rijen, toon=toon if toon in VRAGEN_FILTER else "open", filters=VRAGEN_FILTER,
        tellers=tellers, jaren=jaren, jaar=jaar, vandaag=dt.date.today().isoformat()))

# ---------- Bijdragenatelier en concepten (M4, M8) ----------

_AGENDA_CACHE = {"tijd": 0.0, "punten": []}


def _agendapunten():
    """Agendapunten van de komende weken (tien minuten bewaard, Notubiz is niet snel)."""
    import time  # pylint: disable=import-outside-toplevel
    if time.time() - _AGENDA_CACHE["tijd"] > 600:
        _AGENDA_CACHE["punten"] = schrijven.agendapunten()
        _AGENDA_CACHE["tijd"] = time.time()
    return _AGENDA_CACHE["punten"]


@app.get("/bijdragen", response_class=HTMLResponse)
def bijdragen_scherm(request: Request, stuk: int = 0, punt: int = 0, soort: str = ""):
    fout, punten = "", []
    try:
        punten = _agendapunten()
    except Exception as e:  # noqa: BLE001
        fout = f"De agenda's uit Notubiz konden niet worden geladen ({e}). Je kunt wel met een eigen tekst of idee beginnen."
    gekozen_stuk = q1("SELECT * FROM stukken WHERE id = %s", (stuk,)) if stuk else None
    recent = q("SELECT id, soort, titel, status, aangemaakt FROM concepten ORDER BY id DESC LIMIT 8")
    return templates.TemplateResponse("bijdragen.html", ctx(
        request, "bijdragen", soorten=schrijven.SOORTEN, punten=punten, gekozen_stuk=gekozen_stuk, punt=punt,
        recent=recent, fout=fout, sjabloon_soorten=schrijven.MET_SJABLOON, soorten_stuk=raadsinfo.SOORTEN,
        voorkeur_soort=soort if soort in schrijven.SOORTEN else ""))


@app.post("/bijdragen")
async def bijdragen_maak(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    soort = form.get("soort")
    if soort not in schrijven.SOORTEN:
        raise HTTPException(400)
    bron_type = form.get("bron", "idee")
    bron, titel = {"type": bron_type}, str(form.get("titel", "")).strip()
    if bron_type == "agendapunt":
        try:
            p = next(x for x in _agendapunten() if str(x["punt_id"]) == str(form.get("punt_id")))
        except StopIteration:
            return RedirectResponse("/bijdragen?" + urlencode({"melding": "Kies een agendapunt."}), status_code=303)
        bron.update(p)
        titel = titel or p["onderwerp"]
    elif bron_type == "stuk":
        s = q1("SELECT id, titel FROM stukken WHERE id = %s", (int(form.get("stuk_id") or 0),))
        if not s:
            raise HTTPException(400)
        bron["stuk_id"] = s["id"]
        titel = titel or s["titel"]
    invoer = {"aanwijzingen": str(form.get("aanwijzingen", "")).strip(), "tekst": str(form.get("tekst", "")).strip()}
    origineel, origineel_naam = None, None
    bestand = form.get("bestand")
    if bestand is not None and getattr(bestand, "filename", ""):
        inhoud = await bestand.read()
        if len(inhoud) > MAX_UPLOAD:
            return RedirectResponse("/bijdragen?" + urlencode({"melding": "Het bestand is te groot."}), status_code=303)
        try:
            uit_bestand = fractie.tekst_uit_bestand(bestand.filename, inhoud).strip()
        except Exception as e:  # noqa: BLE001
            return RedirectResponse("/bijdragen?" + urlencode({"melding": f"Het bestand kon niet worden gelezen: {e}"}),
                                    status_code=303)
        invoer["tekst"] = (invoer["tekst"] + "\n\n" + uit_bestand).strip() if invoer["tekst"] else uit_bestand
        invoer["bestand"] = bestand.filename
        origineel_naam = bestand.filename
        if bestand.filename.lower().endswith(".docx"):
            origineel = inhoud
    if not (bron_type in ("agendapunt", "stuk") or invoer["aanwijzingen"] or invoer["tekst"]):
        return RedirectResponse("/bijdragen?" + urlencode({"melding": "Geef aanwijzingen of plak een tekst."}), status_code=303)
    r = q1("""INSERT INTO concepten (soort, titel, bron, invoer, origineel, origineel_naam)
              VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
           (soort, titel or schrijven.SOORTEN[soort], Jsonb(bron), Jsonb(invoer), origineel, origineel_naam))
    schrijven.start(pool, r["id"])
    return RedirectResponse(f"/concepten/{r['id']}", status_code=303)


@app.get("/concepten", response_class=HTMLResponse)
def concepten_lijst(request: Request, soort: str = ""):
    rijen = q("""SELECT k.id, k.soort, k.titel, k.status, k.aangemaakt, k.bijgewerkt, k.ouder_id,
                        k.bron->>'vergadering' AS vergadering
                 FROM concepten k WHERE (%s = '' OR k.soort = %s) ORDER BY k.id DESC LIMIT 200""", (soort, soort))
    return templates.TemplateResponse("concepten.html", ctx(
        request, "concepten", rijen=rijen, soorten=schrijven.SOORTEN, soort=soort))


@app.get("/concepten/{cid}", response_class=HTMLResponse)
def concept_scherm(request: Request, cid: int):
    k = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not k:
        raise HTTPException(404)
    versies = q("""WITH RECURSIVE keten AS (SELECT id, ouder_id FROM concepten WHERE id = %s
                     UNION SELECT c.id, c.ouder_id FROM concepten c JOIN keten ON c.id = keten.ouder_id)
                   SELECT id FROM keten ORDER BY id""", (cid,))
    kinderen = q("SELECT id FROM concepten WHERE ouder_id = %s ORDER BY id", (cid,))
    return templates.TemplateResponse("concept.html", ctx(
        request, "concepten", k=k, meta=k["meta"] or {}, soorten=schrijven.SOORTEN, md=schrijven.md_html,
        weergave=schrijven.velden_als_tekst(k["soort"], k["velden"]) if k["velden"] else k["tekst"],
        versies=[v["id"] for v in versies], kinderen=[v["id"] for v in kinderen]))


@app.get("/concepten/{cid}/word")
def concept_word(cid: int):
    k = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not k or k["status"] == "bezig":
        raise HTTPException(404)
    with pool.connection() as c:
        inhoud = schrijven.word(c, k)
    return _word(inhoud, schrijven.bestandsnaam(k))


@app.get("/concepten/{cid}/opmerkingen")
def concept_opmerkingen(cid: int):
    """Feedback: de tekst van de collega met de opmerkingen in de kantlijn."""
    k = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not k or k["soort"] != "feedback" or k["status"] != "klaar":
        raise HTTPException(404)
    inhoud, _ = schrijven.word_met_opmerkingen(k, bytes(k["origineel"]) if k["origineel"] else None)
    basis = re.sub(r"\.docx$", "", k["origineel_naam"] or "tekst", flags=re.I)
    return _word(inhoud, f"{basis}-met-opmerkingen.docx")


@app.post("/concepten/{cid}/herschrijf")
async def concept_herschrijf(request: Request, cid: int):
    controleer_herkomst(request)
    form = await request.form()
    k = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not k:
        raise HTTPException(404)
    invoer = dict(k["invoer"] or {})
    invoer["aanwijzingen"] = str(form.get("aanwijzingen", "")).strip()
    invoer["vorige"] = k["tekst"] or ""
    r = q1("""INSERT INTO concepten (soort, titel, bron, invoer, ouder_id, origineel, origineel_naam)
              VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
           (k["soort"], k["titel"], Jsonb(k["bron"] or {}), Jsonb(invoer), cid, k["origineel"], k["origineel_naam"]))
    schrijven.start(pool, r["id"])
    return RedirectResponse(f"/concepten/{r['id']}", status_code=303)


@app.post("/concepten/{cid}/opnieuw")
def concept_opnieuw(request: Request, cid: int):
    controleer_herkomst(request)
    uitvoeren("UPDATE concepten SET status = 'bezig', fout = NULL, bijgewerkt = now() WHERE id = %s", (cid,))
    schrijven.start(pool, cid)
    return RedirectResponse(f"/concepten/{cid}", status_code=303)


@app.post("/concepten/{cid}/weg")
def concept_weg(request: Request, cid: int):
    controleer_herkomst(request)
    uitvoeren("UPDATE concepten SET ouder_id = NULL WHERE ouder_id = %s", (cid,))
    uitvoeren("DELETE FROM concepten WHERE id = %s", (cid,))
    return RedirectResponse("/concepten", status_code=303)


# ---------- Adviseur (M7) ----------

@app.get("/adviseur", response_class=HTMLResponse)
def adviseur_scherm(request: Request):
    gesprekken = q("""SELECT g.*, (SELECT count(*) FROM gesprek_berichten b WHERE b.gesprek_id = g.id) AS aantal
                      FROM gesprekken g ORDER BY bijgewerkt DESC LIMIT 50""")
    return templates.TemplateResponse("adviseur.html", ctx(request, "adviseur", gesprekken=gesprekken, rollen=adviseur.ROLLEN))


@app.post("/adviseur")
async def adviseur_nieuw(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    vraag, rol = str(form.get("vraag", "")).strip(), form.get("rol", "strateeg")
    vertrouwelijk = form.get("vertrouwelijk") == "1"
    if not vraag:
        return RedirectResponse("/adviseur", status_code=303)
    titel = re.sub(r"\s+", " ", vraag)[:90]
    g = q1("INSERT INTO gesprekken (titel, rol, vertrouwelijk) VALUES (%s, %s, %s) RETURNING id",
           (titel, rol if rol in adviseur.ROLLEN else "strateeg", vertrouwelijk))
    uitvoeren("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (g["id"], vraag))
    adviseur.start(pool, g["id"])
    return RedirectResponse(f"/adviseur/{g['id']}", status_code=303)


@app.get("/adviseur/{gid}", response_class=HTMLResponse)
def adviseur_gesprek(request: Request, gid: int):
    g = q1("SELECT * FROM gesprekken WHERE id = %s", (gid,))
    if not g:
        raise HTTPException(404)
    berichten = q("SELECT * FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gid,))
    return templates.TemplateResponse("gesprek.html", ctx(
        request, "adviseur", g=g, berichten=berichten, rollen=adviseur.ROLLEN, md=schrijven.md_html))


@app.post("/adviseur/{gid}")
async def adviseur_vervolg(request: Request, gid: int):
    controleer_herkomst(request)
    form = await request.form()
    vraag = str(form.get("vraag", "")).strip()
    g = q1("SELECT status FROM gesprekken WHERE id = %s", (gid,))
    if not g:
        raise HTTPException(404)
    if vraag and g["status"] != "bezig":
        uitvoeren("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'user', %s)", (gid, vraag))
        if form.get("rol") in adviseur.ROLLEN:
            uitvoeren("UPDATE gesprekken SET rol = %s WHERE id = %s", (form.get("rol"), gid))
        uitvoeren("UPDATE gesprekken SET status = 'bezig', fout = NULL, bijgewerkt = now() WHERE id = %s", (gid,))
        adviseur.start(pool, gid)
    return RedirectResponse(f"/adviseur/{gid}#laatste", status_code=303)


@app.post("/adviseur/{gid}/opnieuw")
def adviseur_opnieuw(request: Request, gid: int):
    controleer_herkomst(request)
    uitvoeren("UPDATE gesprekken SET status = 'bezig', fout = NULL WHERE id = %s", (gid,))
    adviseur.start(pool, gid)
    return RedirectResponse(f"/adviseur/{gid}", status_code=303)


@app.post("/adviseur/{gid}/weg")
def adviseur_weg(request: Request, gid: int):
    controleer_herkomst(request)
    uitvoeren("DELETE FROM gesprek_berichten WHERE gesprek_id = %s", (gid,))
    uitvoeren("DELETE FROM gesprekken WHERE id = %s", (gid,))
    return RedirectResponse("/adviseur", status_code=303)


# ---------- Whaller-berichten (fase 1, M6) ----------

def _week(param):
    """Maandag van de gevraagde week; standaard de week na deze (berichten staan op woensdag klaar)."""
    try:
        d0 = dt.date.fromisoformat(param) if param else None
    except ValueError:
        d0 = None
    vandaag = dt.date.today()
    basis = d0 or (vandaag + dt.timedelta(days=7))
    return basis - dt.timedelta(days=basis.weekday())


def _invoer(sleutel):
    r = q1("SELECT gegevens FROM whaller_invoer WHERE sleutel = %s", (sleutel,))
    return dict(r["gegevens"]) if r else {}


def _instellingen():
    return {r["sleutel"]: r["waarde"] or "" for r in q("SELECT sleutel, waarde FROM instellingen")}


def _regels(tekst):
    return [r.strip() for r in str(tekst or "").replace("\r", "").split("\n") if r.strip()]


@app.get("/whaller", response_class=HTMLResponse)
def whaller_scherm(request: Request, week: str = ""):
    maandag = _week(week)
    zondag = maandag + dt.timedelta(days=6)
    inst = _instellingen()
    personen = q("SELECT naam, whaller, rol FROM personen WHERE actief AND coalesce(whaller, '') <> '' ORDER BY volgorde, naam")
    raadsleden = {p["whaller"] for p in personen if (p["rol"] or "").startswith("raadslid")}
    toew = q("SELECT * FROM toewijzingen")
    berichten, fout, overig = [], "", []
    try:
        evenementen = notubiz.evenementen(maandag, zondag)
    except Exception as e:  # noqa: BLE001 - netwerkfout naar het scherm
        evenementen, fout = [], f"Notubiz is nu niet bereikbaar ({e}). Probeer het straks opnieuw."
    for ev in evenementen:
        try:
            if ev["soort"] == "commissie" and ev["type"] == "assembly":
                avond = notubiz.commissieavond(ev["id"])
                datum = avond["start"].date()
                sl = f"commissie:{datum.isoformat()}"
                inv = _invoer(sl)
                punten = whaller.commissie_punten(avond, toew)
                berichten.append({"soort": "commissie", "sleutel": sl, "datum": datum,
                                  "titel": f"Commissievergadering {whaller.d(datum)}", "url": avond["url"],
                                  "punten": punten, "invoer": inv,
                                  "html": whaller.commissie_bericht(avond, punten, inst, inv.get("map_url", "")),
                                  "checklist": whaller.checklist("commissie", datum, inst)})
            elif ev["soort"] == "raad" and ev["type"] == "meeting":
                verg = notubiz.vergadering(ev["id"])
                datum = verg["start"].date()
                sl = f"raad:{datum.isoformat()}"
                inv = _invoer(sl)
                indeling = whaller.raad_indeling(verg, toew, raadsleden)
                berichten.append({"soort": "raad", "sleutel": sl, "datum": datum,
                                  "titel": f"{verg['naam']} {whaller.d(datum)}", "url": verg["url"],
                                  "indeling": indeling, "invoer": inv,
                                  "html": whaller.raad_bericht(verg, indeling, inst, inv,
                                                               whaller.raad_vooraf(evenementen, verg), inv.get("map_url", "")),
                                  "checklist": whaller.checklist("raad", datum, inst)})
            else:
                overig.append(ev)
        except Exception as e:  # noqa: BLE001
            fout = f"De agenda van {ev['naam']} kon niet worden gelezen ({e})."
    fdatum = whaller.fractie_datum(maandag, inst)
    fsl = f"fractie:{fdatum.isoformat()}"
    finv = _invoer(fsl)
    punt4 = [(b["titel"], b["invoer"].get("whaller_url", ""), []) for b in berichten]
    fractie = {"soort": "fractie", "sleutel": fsl, "datum": fdatum, "titel": f"#fractievergadering {whaller.d(fdatum)}",
               "invoer": finv, "html": whaller.fractie_bericht(fdatum, inst, finv, punt4),
               "checklist": whaller.checklist("fractie", fdatum, inst, finv)}
    return templates.TemplateResponse("whaller.html", ctx(
        request, "whaller", maandag=maandag, zondag=zondag, berichten=[fractie] + berichten, overig=markeer_besloten(overig),
        fout=fout, personen=personen, raadsleden=raadsleden, inst=inst, lang=whaller.lang,
        vorige=(maandag - dt.timedelta(days=7)).isoformat(), volgende=(maandag + dt.timedelta(days=7)).isoformat(),
        melding=request.query_params.get("melding", "")))


def _bewaar_toewijzingen(form):
    """Slaat de woordvoering van één vergadering op (vanaf Agenda of Whaller). Staat het Whaller-bericht van die
    vergadering al online, dan krijgt het een waarschuwing dat het moet worden aangepast. Geeft (soort, datum, aantal)."""
    soort, datum = form.get("soort"), dt.date.fromisoformat(str(form.get("datum")))
    if soort not in ("commissie", "raad"):
        raise HTTPException(400)
    gewijzigd = 0
    for k in form.keys():
        if not k.startswith("onderwerp_"):
            continue
        i = k.split("_", 1)[1]
        onderwerp = form.get(k)
        wv, erbij = form.get(f"wv_{i}", ""), form.get(f"erbij_{i}", "")
        oud = q1("SELECT woordvoerder, zit_erbij FROM toewijzingen WHERE soort = %s AND datum = %s AND sleutel = %s",
                 (soort, datum, whaller.sleutel(onderwerp)))
        if oud and (oud["woordvoerder"] or "") == wv and (oud["zit_erbij"] or "") == erbij:
            continue
        uitvoeren("""INSERT INTO toewijzingen (soort, datum, sleutel, onderwerp, woordvoerder, zit_erbij)
                     VALUES (%s, %s, %s, %s, %s, %s)
                     ON CONFLICT (soort, datum, sleutel) DO UPDATE SET woordvoerder = EXCLUDED.woordvoerder,
                       zit_erbij = EXCLUDED.zit_erbij, onderwerp = EXCLUDED.onderwerp, bijgewerkt = now()""",
                  (soort, datum, whaller.sleutel(onderwerp), onderwerp, wv, erbij))
        gewijzigd += 1
    if gewijzigd:
        _markeer_whaller(soort, datum)
    return soort, datum, gewijzigd


def _markeer_whaller(soort, datum):
    """Een al geplaatst Whaller-bericht moet worden bijgewerkt na een nieuwe woordvoering."""
    sl = f"{soort}:{datum.isoformat()}"
    inv = _invoer(sl)
    if inv.get("whaller_url"):
        inv["gewijzigd_na_plaatsen"] = dt.datetime.now().strftime("%d-%m %H.%M")
        uitvoeren("""INSERT INTO whaller_invoer (sleutel, gegevens) VALUES (%s, %s)
                     ON CONFLICT (sleutel) DO UPDATE SET gegevens = EXCLUDED.gegevens, bijgewerkt = now()""",
                  (sl, Jsonb(inv)))


@app.post("/whaller/toewijzingen")
async def whaller_toewijzingen(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    soort, datum, _ = _bewaar_toewijzingen(form)
    terug = f"/whaller?week={form.get('week', '')}&melding=Woordvoerders+opgeslagen.#{soort}-{datum.isoformat()}"
    return RedirectResponse(terug, status_code=303)


@app.post("/whaller/invoer")
async def whaller_invoer(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    sl = form.get("sleutel", "")
    if not re.match(r"^(fractie|commissie|raad):\d{4}-\d{2}-\d{2}$", sl):
        raise HTTPException(400)
    gegevens = _invoer(sl)
    for veld in ("mededelingen", "ingebracht", "overig", "organisatorisch", "bijzonder", "moties"):
        if veld in form:
            gegevens[veld] = _regels(form.get(veld))
    for veld in ("tijd", "map_url", "whaller_url"):
        if veld in form:
            gegevens[veld] = str(form.get(veld)).strip()
    if form.get("aangepast"):
        gegevens.pop("gewijzigd_na_plaatsen", None)
    uitvoeren("""INSERT INTO whaller_invoer (sleutel, gegevens) VALUES (%s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET gegevens = EXCLUDED.gegevens, bijgewerkt = now()""",
              (sl, Jsonb(gegevens)))
    terug = f"/whaller?week={form.get('week', '')}&melding=Opgeslagen.#{sl.replace(':', '-')}"
    return RedirectResponse(terug, status_code=303)


# Schermen die in een latere fase worden gebouwd: één generieke pagina met uitleg.
def _maak_scherm(sleutel):
    def scherm(request: Request):
        s = schermen.SCHERMEN[sleutel]
        return templates.TemplateResponse("scherm.html", ctx(request, sleutel, s=s))
    return scherm


for _sleutel, _s in schermen.SCHERMEN.items():
    app.add_api_route(_s["pad"], _maak_scherm(_sleutel), methods=["GET"], response_class=HTMLResponse)


@app.exception_handler(404)
def niet_gevonden(request: Request, _exc):
    return templates.TemplateResponse("404.html", ctx(request, ""), status_code=404)
