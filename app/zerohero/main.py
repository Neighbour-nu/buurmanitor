"""Doellijn: de ZEROHERO-cockpit.

Leest de database doellijn (gevuld door de n8n-werkstromen) en toont de schermen
Vandaag, Inbox en Congres. Inloggen gebeurt vooraf door Authelia (via Caddy);
de app vertrouwt de header Remote-User die Caddy meestuurt.
"""
import datetime as dt
import hmac
import html
import json
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from . import omgeving, wp
from .beeld import maak_beeld
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["ZEROHERO_DATABASE_URL"]
CONGRES_DATUM = dt.date.fromisoformat(os.environ.get("ZEROHERO_CONGRES_DATUM", "2027-02-03"))
CONGRES_EVENT_ID = os.environ.get("ZEROHERO_CONGRES_EVENT_ID", "")  # optioneel: alleen dit evenement tellen
# Adressen waarvandaan formulieren mogen komen: de cockpit plus eventueel extra adressen (komma's), zoals de portal.
TOEGESTANE_HOSTS = {omgeving.PORTAL_HOST} | {h.strip().lower() for h in os.environ.get("TOEGESTANE_HOSTS", "").split(",") if h.strip()}
# Geheim dat Caddy bij elk verzoek meestuurt. Is het ingesteld, dan komen alleen verzoeken via Caddy (en dus via
# Authelia) binnen; een andere container op het interne netwerk kan zich niet meer voordoen als ingelogde gebruiker.
PROXY_GEHEIM = os.environ.get("PROXY_GEHEIM", "")
# Zonder proxygeheim bereikbaar: gezondheid, statische bestanden en de /intern-adressen voor n8n (die hebben een eigen controle).
OPEN_PADEN = ("/gezond", "/static/", "/favicon", "/intern/")
N8N_CONCEPT_URL = os.environ.get("ZEROHERO_N8N_CONCEPT_URL", "http://n8n:5678/webhook/doellijn-concept")
N8N_CONTROLE_URL = os.environ.get("ZEROHERO_N8N_CONTROLE_URL", "http://n8n:5678/webhook/doellijn-controle")
CONCEPT_SECRET = os.environ.get("ZEROHERO_CONCEPT_SECRET", "")

STATUS_OPEN = ("nieuw", "bewaard", "geselecteerd")
STATUS_KEUZE = ("nieuw", "bewaard", "geselecteerd", "genegeerd")
WEGGEFILTERD = ("onleesbaar", "geen_inhoud", "zoeken_mislukt", "geleerd_weg")

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": True})

MIGRATIES = [
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS status_op timestamptz",
    "ALTER TABLE tt_orders ADD COLUMN IF NOT EXISTS evenement text",
    "CREATE INDEX IF NOT EXISTS items_keepers_gin ON items USING gin (keepers)",
    "CREATE INDEX IF NOT EXISTS items_sport ON items (sport)",
    """CREATE TABLE IF NOT EXISTS concepten (
        id serial PRIMARY KEY,
        item_ids bigint[] NOT NULL,
        status text NOT NULL DEFAULT 'bezig',
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        klaar_op timestamptz,
        blog_titel text, blog_html text, linkedin text, instagram text,
        verificatie jsonb, wp_post_id int, wp_link text, fout text,
        model text, tokens_in int, tokens_out int)""",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS kernzin text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS grafiek jsonb",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS foto_zoek jsonb",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS sport text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS beeld_media_id int",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS beeld_url text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS beeld_bron text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS controle jsonb",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS controle_status text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS controle_op timestamptz",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS controle_fout text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS instructie text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS gepubliceerd_op timestamptz",
]


@asynccontextmanager
async def lifespan(_app):
    pool.open(wait=True, timeout=30)
    with pool.connection() as c:
        for sql in MIGRATIES:
            c.execute(sql)
    if omgeving.ACHTERGROND:
        start_achtergrond()
    yield
    pool.close()


def start_achtergrond():
    """Alle achtergronddraden bij het opstarten (in de oude Doellijn pas bij het eerste bezoek aan dashboard of adviseur)."""
    from . import adviseur, gdata, mailerlite, meta, rekening, threads, tickettailor, woo, youtube  # pylint: disable=import-outside-toplevel
    for m in (mailerlite, woo, gdata, youtube, meta, tickettailor, threads, rekening):
        m.start_achtergrond(q)
    adviseur.start_achtergrond()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
BASIS = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASIS, "static")), name="static")


@app.middleware("http")
async def alleen_via_caddy(request: Request, call_next):
    """Met PROXY_GEHEIM: alleen verzoeken die via Caddy en Authelia komen, behalve de open paden."""
    if PROXY_GEHEIM and not request.url.path.startswith(OPEN_PADEN):
        if not hmac.compare_digest(request.headers.get("X-Proxy-Geheim", ""), PROXY_GEHEIM):
            return JSONResponse({"fout": "niet via de proxy"}, status_code=401)
        if not request.headers.get("Remote-User"):
            return JSONResponse({"fout": "niet ingelogd"}, status_code=401)
    return await call_next(request)
templates = Jinja2Templates(directory=os.path.join(BASIS, "templates"))


# ---------- hulpfuncties ----------

def q(sql, params=None):
    with pool.connection() as c:
        return c.execute(sql, params or ()).fetchall()


def q1(sql, params=None):
    rows = q(sql, params)
    return rows[0] if rows else None


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


def euro(cent):
    return "€ " + f"{(cent or 0) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def advies_van(x):
    a = x.get("advies") or {}
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            a = {}
    return a if isinstance(a, dict) else {}


def alleen_kop(x):
    return str(x.get("samenvatting") or "").strip().lower().startswith("volgens de kop")


def keepers_van(x):
    return {str(k).strip().lower() for k in (x.get("keepers") or []) if k}


def zelfde(a, b):
    if a.get("verhaal") and a.get("verhaal") == b.get("verhaal"):
        return True
    return bool(keepers_van(a) & keepers_van(b))


def clusteren(items):
    """Voegt berichten over dezelfde keeper of hetzelfde voorval samen. Items gesorteerd op score."""
    clusters = []
    for x in items:
        for c in clusters:
            if any(zelfde(x, y) for y in c):
                c.append(x)
                break
        else:
            clusters.append([x])
    verhalen = []
    for c in clusters:
        top = c[0]
        rijk = next((y for y in c if not alleen_kop(y)), top)
        v = dict(rijk)
        v["score"] = top["score"]
        v["leden"] = c
        v["ids"] = [y["id"] for y in c]
        v["gemeld"] = any(y.get("gemeld") for y in c)
        v["bronnen"] = sorted({y["bron"] for y in c if y.get("bron")})
        statussen = {y["status"] for y in c}
        v["status"] = next((s for s in ("geselecteerd", "bewaard", "genegeerd") if s in statussen), "nieuw")
        verhalen.append(v)
    verhalen.sort(key=lambda v: v["score"] or 0, reverse=True)
    return verhalen


def ctx(request, **kw):
    kw.update(request=request, gebruiker=request.headers.get("Remote-Name") or request.headers.get("Remote-User") or "",
              geleden=geleden, euro=euro, advies_van=advies_van, alleen_kop=alleen_kop,
              congres_dagen=(CONGRES_DATUM - dt.date.today()).days, portal_host=omgeving.PORTAL_HOST,
              auth_url=omgeving.AUTH_URL)
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


def congres_event():
    """Het evenement dat als 'het congres' telt: gekozen op de pagina Congres, anders CONGRES_EVENT_ID uit .env."""
    try:
        r = q1("SELECT waarde FROM instellingen WHERE sleutel = 'congres_event_id'")
    except Exception:  # noqa: BLE001
        r = None
    if r is not None:
        return r["waarde"] or ""
    return CONGRES_EVENT_ID


def kaartverkoop():
    filt, params = "", []
    if congres_event():
        filt, params = " AND event_id = %s", [congres_event()]
    totaal = q1(f"""SELECT coalesce(sum(aantal),0) AS kaarten, coalesce(sum(totaal_cent),0) AS omzet
                    FROM tt_orders WHERE status = 'completed'{filt}""", params)
    dag = q1(f"""SELECT coalesce(sum(aantal),0) AS kaarten, coalesce(sum(totaal_cent),0) AS omzet
                 FROM tt_orders WHERE status = 'completed' AND besteld > now() - interval '24 hours'{filt}""", params)
    return totaal, dag


# ---------- schermen ----------

# ---------- favicon (dezelfde als zerohero.one) ----------

FAVICON_BRON = "https://zerohero.one/wp-content/uploads/2024/01/cropped-ZeroHero-Favicon-{n}x{n}.png"
_favicons = {}


@app.get("/favicon-{n}.png")
def favicon(n: int):
    """Haalt het site-icoon van zerohero.one eenmalig op en houdt het daarna in het geheugen."""
    if n not in (32, 180, 192):
        raise HTTPException(404)
    if n not in _favicons:
        for maat in (n, 270):  # 270x270 staat zeker op de site; browsers schalen zelf
            try:
                req = urllib.request.Request(FAVICON_BRON.format(n=maat), headers={"User-Agent": "Doellijn/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    _favicons[n] = r.read()
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            raise HTTPException(404)
    return Response(_favicons[n], media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/favicon.ico")
def favicon_ico():
    return RedirectResponse("/favicon-32.png", status_code=301)


SORTERINGEN = {"score": "Score, hoog naar laag", "nieuw": "Binnengekomen, nieuwste eerst", "oud": "Binnengekomen, oudste eerst"}


@app.get("/", response_class=HTMLResponse)
def vandaag(request: Request, sport: str = "", sorteer: str = "score"):
    leren_toepassen()
    if sorteer not in SORTERINGEN:
        sorteer = "score"
    waar, params = ["binnen > now() - interval '24 hours'", "score >= 50", "status = ANY(%s)"], [list(STATUS_OPEN)]
    if sport:
        waar.append("sport = %s")
        params.append(sport)
    items = q(f"SELECT * FROM items WHERE {' AND '.join(waar)} ORDER BY score DESC LIMIT 300", params)
    verhalen = clusteren(items)
    for v in verhalen:
        v["binnen"] = max((y["binnen"] for y in v["leden"] if y.get("binnen")), default=v.get("binnen"))
    if sorteer == "nieuw":
        verhalen.sort(key=lambda v: v["binnen"] or nu(), reverse=True)
    elif sorteer == "oud":
        verhalen.sort(key=lambda v: v["binnen"] or nu())
    sporten = q("""SELECT sport, count(*) AS n FROM items
                   WHERE binnen > now() - interval '24 hours' AND score >= 50 AND status = ANY(%s) AND sport IS NOT NULL
                   GROUP BY sport ORDER BY n DESC, sport""", (list(STATUS_OPEN),))
    stats = q1("""SELECT count(*) AS gewogen,
                         count(*) FILTER (WHERE score >= 80) AS top,
                         count(*) FILTER (WHERE score >= 60) AS boven60,
                         count(*) FILTER (WHERE status = ANY(%s)) AS weggefilterd
                  FROM items WHERE binnen > now() - interval '24 hours'""", (list(WEGGEFILTERD),))
    geselecteerd = q1("SELECT count(*) AS n FROM items WHERE status = 'geselecteerd'")["n"]
    totaal, dag = kaartverkoop()
    return templates.TemplateResponse("vandaag.html", ctx(
        request, pagina="vandaag", verhalen=verhalen[:10], meer=max(0, len(verhalen) - 10),
        stats=stats, geselecteerd=geselecteerd, kaarten=totaal, kaarten_dag=dag,
        sporten=sporten, f={"sport": sport, "sorteer": sorteer}, sorteringen=SORTERINGEN))


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, sport: str = "", score: int = 0, status: str = "open", zoek: str = "",
          dagen: int = 3, pagina: int = 1):
    waar, params = ["binnen > now() - make_interval(days => %s)"], [max(1, min(dagen, 90))]
    if status == "open":
        waar.append("status = ANY(%s)"); params.append(["nieuw", "bewaard"])
    elif status == "weggefilterd":
        waar.append("status = ANY(%s)"); params.append(list(WEGGEFILTERD))
    elif status in STATUS_KEUZE:
        waar.append("status = %s"); params.append(status)
    if sport:
        waar.append("sport = %s"); params.append(sport)
    if score:
        waar.append("score >= %s"); params.append(score)
    if zoek.strip():
        waar.append("(titel ILIKE %s OR array_to_string(keepers, ' ') ILIKE %s OR bron ILIKE %s)")
        params += [f"%{zoek.strip()}%"] * 3
    where = " AND ".join(waar)
    per = 40
    pagina = max(1, pagina)
    totaal = q1(f"SELECT count(*) AS n FROM items WHERE {where}", params)["n"]
    rijen = q(f"""SELECT * FROM items WHERE {where}
                  ORDER BY score DESC NULLS LAST, binnen DESC LIMIT {per} OFFSET %s""",
              params + [(pagina - 1) * per])
    sporten = [r["sport"] for r in q("SELECT DISTINCT sport FROM items WHERE sport IS NOT NULL ORDER BY sport")]
    filters = dict(sport=sport, score=score, status=status, zoek=zoek, dagen=dagen)

    def link(p):
        return "/inbox?" + urlencode({**filters, "pagina": p})

    return templates.TemplateResponse("inbox.html", ctx(
        request, pagina="inbox", items=rijen, totaal=totaal, sporten=sporten, f=filters,
        huidige=pagina, vorige=link(pagina - 1) if pagina > 1 else None,
        volgende=link(pagina + 1) if pagina * per < totaal else None))


@app.get("/congres", response_class=HTMLResponse)
def congres(request: Request):
    from . import tickettailor
    tickettailor.start_achtergrond(q)
    totaal, dag = kaartverkoop()
    evenementen = q("""SELECT event_id, max(evenement) AS naam,
                              coalesce(sum(aantal) FILTER (WHERE status='completed'),0) AS kaarten,
                              coalesce(sum(totaal_cent) FILTER (WHERE status='completed'),0) AS omzet,
                              count(*) FILTER (WHERE status='cancelled') AS geannuleerd,
                              max(besteld) AS laatste
                       FROM tt_orders GROUP BY event_id ORDER BY kaarten DESC""")
    filt, params = "", []
    gekozen = congres_event()
    if gekozen:
        filt, params = " AND event_id = %s", [gekozen]
    per_dag = q(f"""SELECT d::date AS dag, coalesce(sum(o.aantal),0) AS kaarten
                    FROM generate_series(current_date - 13, current_date, interval '1 day') d
                    LEFT JOIN tt_orders o ON o.besteld::date = d::date AND o.status = 'completed'{filt.replace('event_id', 'o.event_id')}
                    GROUP BY 1 ORDER BY 1""", params)
    types = q(f"""SELECT t.key AS type, sum((t.value)::int) AS aantal
                  FROM tt_orders o, jsonb_each_text(CASE WHEN jsonb_typeof(o.tickettypes) = 'object' THEN o.tickettypes ELSE '{{}}'::jsonb END) t
                  WHERE o.status = 'completed'{filt.replace('event_id', 'o.event_id')}
                  GROUP BY 1 ORDER BY 2 DESC""", params)
    hoogste = max([r["kaarten"] for r in per_dag] + [1])
    return templates.TemplateResponse("congres.html", ctx(
        request, pagina="congres", kaarten=totaal, kaarten_dag=dag, evenementen=evenementen,
        per_dag=per_dag, hoogste=hoogste, types=types, filter_actief=bool(gekozen), gekozen=gekozen,
        tt_actief=tickettailor.actief(), tt_status=tickettailor.status(q) if tickettailor.actief() else None))


@app.post("/congres/kies")
def congres_kies(request: Request, event_id: str = Form("")):
    controleer_herkomst(request)
    q("""INSERT INTO instellingen (sleutel, waarde, bijgewerkt) VALUES ('congres_event_id', %s, now())
         ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now() RETURNING 1""", (event_id.strip()[:60],))
    return RedirectResponse("/congres", status_code=303)


@app.post("/congres/ververs")
def congres_ververs(request: Request):
    from urllib.parse import quote
    from . import tickettailor
    controleer_herkomst(request)
    ok, melding = tickettailor.ververs(q)
    return RedirectResponse("/congres?" + ("ok=" if ok else "fout=") + quote(melding or ""), status_code=303)


# ---------- acties ----------

@app.post("/items/status", response_class=HTMLResponse)
def zet_status(request: Request, ids: str = Form(...), status: str = Form(...), weergave: str = Form("item")):
    controleer_herkomst(request)
    if status not in STATUS_KEUZE:
        raise HTTPException(400, "Onbekende status")
    id_lijst = [int(i) for i in ids.split(",") if i.strip().isdigit()][:50]
    if not id_lijst:
        raise HTTPException(400, "Geen berichten")
    if status == "nieuw":
        from .leren import terugzetten
        terugzetten(id_lijst)  # wist ook de negeerreden en eventuele 'geen keeper'-namen van deze berichten
    else:
        q("UPDATE items SET status = %s, status_op = now() WHERE id = ANY(%s) RETURNING id", (status, id_lijst))
    rijen = q("SELECT * FROM items WHERE id = ANY(%s) ORDER BY score DESC NULLS LAST", (id_lijst,))
    if weergave == "verhaal":
        v = clusteren(rijen)[0]
        return templates.TemplateResponse("_verhaal.html", ctx(request, v=v, nr=None))
    return templates.TemplateResponse("_item.html", ctx(request, x=rijen[0]))


# ---------- concepten (fase 5) ----------

def start_concept(concept_id, url=None, instructie=""):
    """Vraagt n8n om het concept te schrijven (of te controleren). n8n antwoordt direct; het werk gaat op de achtergrond."""
    req = urllib.request.Request(url or N8N_CONCEPT_URL, data=json.dumps({"concept_id": concept_id, "instructie": instructie or ""}).encode(),
                                 headers={"Content-Type": "application/json", "X-Doellijn-Secret": CONCEPT_SECRET},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


@app.post("/concepten", response_class=HTMLResponse)
def maak_concept(request: Request, ids: str = Form(...), instructie: str = Form("")):
    controleer_herkomst(request)
    id_lijst = [int(i) for i in ids.split(",") if i.strip().isdigit()][:20]
    if not id_lijst:
        raise HTTPException(400, "Geen berichten")
    instructie = instructie.strip()[:1000]
    # Dubbele aanvraag (twee keer geklikt, of een tweede klik terwijl de eerste nog liep)? Dan het concept van zojuist tonen.
    eerder = q1("""SELECT * FROM concepten WHERE item_ids = %s::bigint[] AND aangemaakt > now() - interval '10 minutes'
                   AND status <> 'fout' ORDER BY id DESC LIMIT 1""", (sorted(id_lijst),))
    if eerder is None:
        eerder = q1("""SELECT * FROM concepten WHERE item_ids = %s::bigint[] AND aangemaakt > now() - interval '10 minutes'
                       AND status <> 'fout' ORDER BY id DESC LIMIT 1""", (id_lijst,))
    if eerder:
        return templates.TemplateResponse("_concept_status.html", ctx(request, c=eerder))
    c = q1("INSERT INTO concepten (item_ids, instructie) VALUES (%s, %s) RETURNING *", (id_lijst, instructie or None))
    ok, fout = start_concept(c["id"], instructie=instructie)
    if not ok:
        c = q1("UPDATE concepten SET status = 'fout', fout = %s, klaar_op = now() WHERE id = %s RETURNING *",
               ("n8n niet bereikbaar: " + fout, c["id"]))
    return templates.TemplateResponse("_concept_status.html", ctx(request, c=c))


@app.get("/concepten/{cid}/controle", response_class=HTMLResponse)
def concept_controle(request: Request, cid: int):
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404)
    start = c.get("controle_op") or c.get("klaar_op")
    if c.get("controle_status") == "bezig" and start and (nu() - start).total_seconds() > 900:
        c = q1("UPDATE concepten SET controle_status = 'fout', controle_fout = 'Geen antwoord binnen 15 minuten' WHERE id = %s RETURNING *", (cid,))
    return templates.TemplateResponse("_controle.html", ctx(request, c=_verrijk([c])[0]))


@app.post("/concepten/{cid}/controleer", response_class=HTMLResponse)
def concept_controleer(request: Request, cid: int):
    """Start de feitencontrole op verzoek (kost tokens, dus alleen als Martijn erom vraagt)."""
    controleer_herkomst(request)
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404)
    if c.get("status") != "klaar":
        raise HTTPException(400, "Concept is nog niet klaar")
    if c.get("controle_status") != "bezig":
        c = q1("""UPDATE concepten SET controle_status = 'bezig', controle = NULL, controle_fout = NULL, controle_op = now()
                  WHERE id = %s RETURNING *""", (cid,))
        ok, fout = start_concept(cid, N8N_CONTROLE_URL)
        if not ok:
            c = q1("UPDATE concepten SET controle_status = 'fout', controle_fout = %s WHERE id = %s RETURNING *",
                   ("n8n niet bereikbaar: " + fout, cid))
    return templates.TemplateResponse("_controle.html", ctx(request, c=_verrijk([c])[0]))


@app.get("/concepten/{cid}/status", response_class=HTMLResponse)
def concept_status(request: Request, cid: int):
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404)
    if c["status"] == "bezig" and (nu() - c["aangemaakt"]).total_seconds() > 900:
        c = q1("UPDATE concepten SET status = 'fout', fout = 'Geen antwoord binnen 15 minuten' WHERE id = %s RETURNING *", (cid,))
    return templates.TemplateResponse("_concept_status.html", ctx(request, c=c))


def _verrijk(concepten_rijen):
    """Voegt per concept de verificatielijst en de bronberichten toe."""
    ids = sorted({i for c in concepten_rijen for i in (c["item_ids"] or [])})
    berichten = {r["id"]: r for r in q("SELECT id, titel, url, bron, score FROM items WHERE id = ANY(%s)", (ids,))} if ids else {}
    uit = []
    for c in concepten_rijen:
        c = dict(c)
        v = c.get("verificatie") or []
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = [v]
        c["verificatie_lijst"] = v
        ctl = c.get("controle") or []
        if isinstance(ctl, str):
            try:
                ctl = json.loads(ctl)
            except ValueError:
                ctl = []
        volgorde = {"afwijkend": 0, "niet_gevonden": 1, "bevestigd": 2}
        c["controle_lijst"] = sorted(ctl, key=lambda p: volgorde.get(p.get("oordeel"), 1))
        c["controle_tel"] = {k: sum(1 for p in ctl if p.get("oordeel") == k) for k in volgorde}
        c["berichten"] = [berichten[i] for i in (c["item_ids"] or []) if i in berichten]
        uit.append(c)
    return uit


@app.get("/concepten", response_class=HTMLResponse)
def concepten(request: Request):
    rijen = q("""SELECT c.*, (SELECT titel FROM items WHERE id = c.item_ids[1]) AS bron_titel
                 FROM concepten c ORDER BY aangemaakt DESC LIMIT 100""")
    return templates.TemplateResponse("concepten.html", ctx(request, pagina="concepten", concepten=_verrijk(rijen)))


@app.get("/concepten/{cid}", response_class=HTMLResponse)
def concept(request: Request, cid: int):
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c:
        return RedirectResponse("/concepten")
    return templates.TemplateResponse("concept.html", ctx(request, pagina="concepten", c=_verrijk([c])[0]))


@app.post("/concepten/{cid}/weg", response_class=HTMLResponse)
def concept_weg(request: Request, cid: int):
    controleer_herkomst(request)
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c:
        return HTMLResponse("")
    if c.get("wp_post_id") and not c.get("bestaand_bericht"):  # een bijgewerkt bestaand bericht blijft altijd staan
        fout = ""
        if not wp.wp_actief():
            fout = "De cockpit heeft geen WordPress-gegevens, dus het concept in WordPress kon niet weg."
        else:
            try:
                wp.naar_prullenbak_als_concept(c["wp_post_id"])
            except urllib.error.HTTPError as e:
                if e.code not in (404, 410):  # 404/410: bericht bestaat al niet meer, dan is weggooien prima
                    fout = f"WordPress gaf een foutmelding ({e.code})."
            except Exception as e:  # noqa: BLE001
                fout = "WordPress was niet bereikbaar (" + str(e)[:120] + ")."
        if fout:
            melding = ('<p class="melding fout">⚠️ Niet weggegooid. ' + html.escape(fout) +
                       ' Het concept blijft staan; probeer het later opnieuw of gooi het zelf weg in WordPress.</p>')
            return HTMLResponse(melding, headers={"HX-Retarget": f"#weg-melding-{cid}", "HX-Reswap": "innerHTML"})
    q("DELETE FROM concepten WHERE id = %s RETURNING id", (cid,))
    huidige = request.headers.get("HX-Current-URL", "")
    headers = {"HX-Redirect": "/concepten"} if f"/concepten/{cid}" in huidige else {}
    return HTMLResponse("", headers=headers)


# ---------- beelden bij concepten ----------

def _json(v, standaard):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return standaard
    return v if v is not None else standaard


def _huisstijl_png(c):
    return maak_beeld(c.get("kernzin") or c.get("blog_titel") or "", _json(c.get("grafiek"), None), c.get("sport") or "")


def _zet_beeld(c, inhoud, mime, bestandsnaam, alt, bijschrift, bron):
    media_id, _ = wp.upload(inhoud, bestandsnaam, alt=alt, bijschrift=bijschrift, mime=mime)
    if c.get("wp_post_id"):
        wp.zet_uitgelicht(c["wp_post_id"], media_id)
        try:
            wp.beeld_in_artikel(c["wp_post_id"])
        except Exception:  # noqa: BLE001  (het uitgelichte beeld staat er in elk geval)
            pass
    url = wp.media_url(media_id)
    return q1("UPDATE concepten SET beeld_media_id = %s, beeld_url = %s, beeld_bron = %s WHERE id = %s RETURNING *",
              (media_id, url, bron, c["id"]))


@app.post("/intern/concepten/{cid}/beeld")
def intern_beeld(request: Request, cid: int):
    """Wordt door n8n aangeroepen zodra een concept klaar is: zet het huisstijlbeeld als uitgelicht beeld."""
    if not CONCEPT_SECRET or request.headers.get("X-Doellijn-Secret") != CONCEPT_SECRET:
        raise HTTPException(403)
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c or not wp.wp_actief() or c.get("status") != "klaar" or not c.get("wp_post_id"):
        return {"ok": False, "reden": "geen afgerond concept in WordPress of geen WordPress-gegevens"}
    slug = (c.get("blog_titel") or f"concept-{cid}").lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug).strip("-")[:60] or f"concept-{cid}"
    c = _zet_beeld(c, _huisstijl_png(c), "image/png", f"{slug}.png", c.get("kernzin") or c.get("blog_titel") or "",
                   "", "huisstijl")
    return {"ok": True, "media_id": c["beeld_media_id"]}


@app.get("/concepten/{cid}/huisstijl.png")
def huisstijl_voorbeeld(cid: int):
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404)
    return Response(_huisstijl_png(c), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/concepten/{cid}/fotos", response_class=HTMLResponse)
def foto_suggesties(request: Request, cid: int):
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404)
    termen = [t for t in _json(c.get("foto_zoek"), []) if t][:3]
    if not termen:
        termen = [k for (k,) in [(r["k"],) for r in q("SELECT DISTINCT unnest(keepers) AS k FROM items WHERE id = ANY(%s)", (c["item_ids"],))]][:2]
    eigen, commons, fouten = [], [], []
    for t in termen:
        if wp.wp_actief():
            try:
                eigen += wp.zoek_mediabibliotheek(t, 4)
            except Exception as e:  # noqa: BLE001
                fouten.append(f"Mediabibliotheek: {str(e)[:120]}")
        try:
            commons += wp.zoek_commons(t, 4)
        except Exception as e:  # noqa: BLE001
            fouten.append(f"Commons: {str(e)[:120]}")
    gezien, eigen_u, commons_u = set(), [], []
    for lijst, doel in ((eigen, eigen_u), (commons, commons_u)):
        for f in lijst:
            if f["id"] not in gezien:
                gezien.add(f["id"])
                doel.append(f)
    return templates.TemplateResponse("_fotos.html", ctx(request, c=c, termen=termen, eigen=eigen_u[:6],
                                                         commons=commons_u[:6], fouten=fouten,
                                                         wp_actief=wp.wp_actief()))


@app.post("/concepten/{cid}/uitgelicht", response_class=HTMLResponse)
def kies_uitgelicht(request: Request, cid: int, bron: str = Form(...), sleutel: str = Form("")):
    controleer_herkomst(request)
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c or not wp.wp_actief():
        raise HTTPException(400, "Geen concept of geen WordPress-gegevens")
    fout = ""
    try:
        if bron == "media":
            media_id = int(sleutel)
            if c.get("wp_post_id"):
                wp.zet_uitgelicht(c["wp_post_id"], media_id)
            c = q1("UPDATE concepten SET beeld_media_id = %s, beeld_url = %s, beeld_bron = 'mediabibliotheek' WHERE id = %s RETURNING *",
                   (media_id, wp.media_url(media_id), cid))
        elif bron == "commons":
            inhoud, mime, info = wp.haal_commons(sleutel)
            ext = "png" if "png" in mime else "jpg"
            naam = "".join(ch if ch.isalnum() else "-" for ch in info["titel"].lower()).strip("-")[:60]
            c = _zet_beeld(c, inhoud, mime, f"{naam}.{ext}", info["titel"], info["credit"], "commons")
        elif bron == "huisstijl":
            c = _zet_beeld(c, _huisstijl_png(c), "image/png", f"concept-{cid}.png", c.get("kernzin") or "", "", "huisstijl")
        else:
            raise HTTPException(400, "Onbekende bron")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        fout = str(e)[:300]
    return templates.TemplateResponse("_uitgelicht.html", ctx(request, c=c, fout=fout))


@app.post("/concepten/{cid}/upload", response_class=HTMLResponse)
async def upload_beeld(request: Request, cid: int, bestand: UploadFile = File(...), alt: str = Form(""),
                       bijschrift: str = Form("")):
    """Eigen beeld uploaden: wordt opgeschoond (geen EXIF/GPS), zo nodig verkleind en als uitgelicht beeld gezet."""
    controleer_herkomst(request)
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c or not wp.wp_actief():
        raise HTTPException(400, "Geen concept of geen WordPress-gegevens")
    fout = ""
    try:
        inhoud = await bestand.read(25 * 1024 * 1024 + 1)
        if len(inhoud) > 25 * 1024 * 1024:
            raise ValueError("Het bestand is groter dan 25 MB.")
        import io
        from PIL import Image, ImageOps
        try:
            img = Image.open(io.BytesIO(inhoud))
            img.load()
        except Exception:  # noqa: BLE001
            raise ValueError("Dit is geen afbeelding die ik kan lezen. Gebruik JPG, PNG of WebP.")
        img = ImageOps.exif_transpose(img)
        if img.width > 2400:
            img = img.resize((2400, round(img.height * 2400 / img.width)), Image.LANCZOS)
        uit = io.BytesIO()
        if img.mode in ("RGBA", "LA", "P") and (img.format == "PNG" or "transparency" in img.info or img.mode == "RGBA"):
            img.save(uit, "PNG", optimize=True)
            mime, ext = "image/png", "png"
        else:
            img.convert("RGB").save(uit, "JPEG", quality=86, optimize=True, progressive=True)
            mime, ext = "image/jpeg", "jpg"
        basis = (bestand.filename or f"concept-{cid}").rsplit(".", 1)[0].lower()
        naam = "".join(ch if ch.isalnum() else "-" for ch in basis).strip("-")[:60] or f"concept-{cid}"
        c = _zet_beeld(c, uit.getvalue(), mime, f"{naam}.{ext}", alt.strip() or c.get("blog_titel") or "",
                       bijschrift.strip(), "eigen")
    except ValueError as e:
        fout = str(e)
    except Exception as e:  # noqa: BLE001
        fout = "Uploaden naar WordPress is niet gelukt: " + str(e)[:200]
    return templates.TemplateResponse("_uitgelicht.html", ctx(request, c=c, fout=fout))


@app.post("/concepten/{cid}/publiceer", response_class=HTMLResponse)
def publiceer(request: Request, cid: int):
    """Publiceert het WordPress-concept direct op zerohero.one."""
    controleer_herkomst(request)
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    if not c or not c.get("wp_post_id"):
        raise HTTPException(400, "Geen WordPress-concept")
    fout = ""
    if not wp.wp_actief():
        fout = "De cockpit heeft geen WordPress-gegevens."
    else:
        try:
            p = wp._wp("GET", f"/wp/v2/posts/{int(c['wp_post_id'])}?context=edit&_fields=id,status,link")
            if p.get("status") == "publish":
                c = q1("UPDATE concepten SET gepubliceerd_op = coalesce(gepubliceerd_op, now()), wp_link = %s WHERE id = %s RETURNING *",
                       (p.get("link") or c.get("wp_link"), cid))
            elif p.get("status") == "trash":
                fout = "Dit bericht staat in de prullenbak van WordPress."
            else:
                try:
                    wp.beeld_in_artikel(c["wp_post_id"])  # zeker weten dat de foto ook in het artikel staat
                except Exception:  # noqa: BLE001
                    pass
                p = wp._wp("POST", f"/wp/v2/posts/{int(c['wp_post_id'])}", data={"status": "publish"})
                c = q1("UPDATE concepten SET gepubliceerd_op = now(), wp_link = %s WHERE id = %s RETURNING *",
                       (p.get("link") or c.get("wp_link"), cid))
        except urllib.error.HTTPError as e:
            fout = f"WordPress weigerde het publiceren ({e.code})."
        except Exception as e:  # noqa: BLE001
            fout = "WordPress niet bereikbaar: " + str(e)[:150]
    return templates.TemplateResponse("_concept_acties.html", ctx(request, c=c, fout=fout))


@app.get("/gezond")
def gezond():
    q1("SELECT 1 AS ok")
    return {"ok": True}


@app.exception_handler(404)
def niet_gevonden(request: Request, _exc):
    return RedirectResponse("/")


# ---------- dashboard (fase 6) ----------
from .dashboard import MIGRATIES as _DASHBOARD_MIGRATIES, router as _dashboard_router  # noqa: E402

MIGRATIES.extend(_DASHBOARD_MIGRATIES)
app.include_router(_dashboard_router)

# ---------- de filter laten leren van Negeer ----------
from .leren import MIGRATIES as _LEREN_MIGRATIES, router as _leren_router, toepassen as leren_toepassen  # noqa: E402

MIGRATIES.extend(_LEREN_MIGRATIES)
app.include_router(_leren_router)

# ---------- eigen artikelen ----------
from .artikel import MIGRATIES as _ARTIKEL_MIGRATIES, router as _artikel_router  # noqa: E402

MIGRATIES.extend(_ARTIKEL_MIGRATIES)
app.include_router(_artikel_router)

# ---------- adviseur ----------
from .adviseur import MIGRATIES as _ADVISEUR_MIGRATIES, router as _adviseur_router  # noqa: E402

MIGRATIES.extend(_ADVISEUR_MIGRATIES)
app.include_router(_adviseur_router)

# ---------- titel boven de LinkedIn-post ----------
from .li_titel import MIGRATIES as _LI_MIGRATIES, router as _li_router  # noqa: E402

MIGRATIES.extend(_LI_MIGRATIES)
app.include_router(_li_router)
