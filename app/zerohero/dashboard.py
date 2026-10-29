"""Dashboard: wat zegt iets over het succes van ZEROHERO?

Twee lagen, naar het principe sanity versus vanity:
- Waar het om draait: uitnodigingen (kansen), congreskaarten, boekverkoop, nieuwsbrief, media-aandacht.
- Bereik: volgers, weergaven en bezoekers per kanaal. Context, geen doel op zich.

Cijfers komen uit de eigen database (Doellijn, Ticket Tailor), uit het kansenlogboek en uit de
tabel metingen. Metingen worden nu met de hand ingevoerd; koppelingen (GA4, Meta, YouTube,
MailerLite, WooCommerce) schrijven later in dezelfde tabel met bron 'api'.
"""
import datetime as dt
import html
import json
import os
import urllib.parse
from decimal import Decimal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from . import omgeving
from . import gdata, kosten, linkedin, rekening, threads, tickettailor, mailerlite, meta, podcast, profielbezoek, ssi, woo, youtube
from .main import CONCEPT_SECRET, congres_event, controleer_herkomst, ctx, q, q1, templates

router = APIRouter()

PERIODES = {7: "7 dagen", 30: "30 dagen", 90: "90 dagen", 365: "12 maanden"}

SOORTEN = {
    "lezing": "Lezing of presentatie",
    "media": "Media (krant, radio, tv, online)",
    "podcast": "Podcast of interview",
    "congres": "Congres: partner, spreker of sponsor",
    "samenwerking": "Samenwerking",
    "overig": "Overig",
}
STATUSSEN = {
    "binnen": "Binnen",
    "gesprek": "In gesprek",
    "bevestigd": "Bevestigd",
    "afgerond": "Afgerond",
    "afgewezen": "Afgewezen of vervallen",
}
def seo_artikel_link(k):
    """Link naar Artikelen met de zoekterm als idee, ingevuld voor een SEO-artikel."""
    from urllib.parse import urlencode
    naam, uitleg = gdata.SOORT_KANS[k["soort"]]
    idee = (f"Een artikel dat de zoekvraag '{k['term']}' helder en volledig beantwoordt, vanuit de ZEROHERO-blik "
            "(keepers en goalies uit alle sporten).")
    aanwijzing = (f"SEO-kans uit Search Console ({naam.lower()}): zerohero.one verscheen {k['vertoningen']} keer voor '{k['term']}', "
                  f"gemiddeld op plek {k['positie']}, met {k['klikken']} klikken. Gebruik de zoekterm letterlijk in de titel of een tussenkop "
                  "en beantwoord de vraag in de eerste alinea.")
    if k.get("pagina"):
        aanwijzing += f" Nu verschijnt deze pagina: {k['pagina']}. Link daar vanuit het nieuwe artikel naar, of andersom."
    return "/artikelen?" + urlencode({"idee_titel": k["term"][:1].upper() + k["term"][1:], "idee": idee, "idee_aanwijzing": aanwijzing}) + "#idee"


BRONNEN = ["LinkedIn", "Sales Navigator", "E-mail", "Website of formulier", "Netwerk", "Media", "Anders"]

# Kanalen en hun cijfers. soort 'stand' = een stand op een datum (volgers), 'periode' = opgeteld (weergaven per week).
KANALEN = [
    {"id": "linkedin", "naam": "LinkedIn: Martijn Buurman", "koppeling": "Persoonlijk profiel. Export maandelijks uploaden, SSI en profielweergaven handmatig", "cijfers": [
        ("volgers", "Volgers", "stand"), ("impressies", "Impressies", "periode"), ("interacties", "Interacties", "periode"),
        ("nieuwe_volgers", "Nieuwe volgers", "periode"), ("ssi", "Social Selling Index (0 tot 100)", "stand"),
        ("profielweergaven", "Profielbezoekers", "periode")]},
    {"id": "linkedin_pagina", "naam": "LinkedIn: ZEROHERO", "koppeling": "Bedrijfspagina. Exports maandelijks uploaden, volgers handmatig", "cijfers": [
        ("volgers", "Volgers", "stand"), ("weergaven", "Weergaven", "periode"), ("interacties", "Klikken en reacties", "periode"),
        ("nieuwe_volgers", "Nieuwe volgers", "periode"), ("bezoeken", "Paginabezoek", "periode")]},
    {"id": "instagram", "naam": "Instagram", "koppeling": "Nog niet gekoppeld: deploy/meta.sh", "cijfers": [
        ("volgers", "Volgers", "stand"), ("weergaven", "Weergaven", "periode"), ("bereik", "Bereik (som per dag)", "periode"),
        ("interacties", "Interacties", "periode"), ("nieuwe_volgers", "Nieuwe volgers", "periode"),
        ("profielbezoeken", "Profielbezoeken", "periode")]},
    {"id": "facebook", "naam": "Facebook", "koppeling": "Nog niet gekoppeld: deploy/meta.sh", "cijfers": [
        ("volgers", "Volgers", "stand"), ("weergaven", "Weergaven", "periode"), ("bereik", "Bereik (som per dag)", "periode"),
        ("interacties", "Interacties", "periode"), ("nieuwe_volgers", "Nieuwe volgers", "periode")]},
    {"id": "threads", "naam": "Threads", "koppeling": "Nog niet gekoppeld: deploy/threads.sh", "cijfers": [
        ("volgers", "Volgers", "stand"), ("profielweergaven", "Profielweergaven", "periode"),
        ("interacties", "Interacties (likes, reacties, reposts, citaten)", "periode"), ("likes", "Likes", "periode")]},
    {"id": "youtube", "naam": "YouTube (Keepersjournaal)", "koppeling": "Gepland: YouTube API", "cijfers": [
        ("abonnees", "Abonnees", "stand"), ("weergaven_totaal", "Weergaven (totaal)", "stand"), ("videos", "Video's", "stand")]},
    {"id": "podcast", "naam": "Podcast (Spotify)", "koppeling": "Maandelijks: export uit Spotify for Creators uploaden", "cijfers": [
        ("volgers", "Volgers", "stand"), ("streams", "Afgespeeld en gedownload", "periode"), ("uren", "Luistertijd (uur)", "periode")]},
    {"id": "nieuwsbrief", "naam": "Nieuwsbrief (MailerLite)", "koppeling": "Gepland: MailerLite API", "cijfers": [
        ("abonnees", "Abonnees", "stand"), ("openratio", "Openratio laatste nieuwsbrief (%)", "stand")]},
    {"id": "boek", "naam": "Boeken buiten de webshop", "koppeling": "Handmatig (boekhandel, Managementboek, lezingen)", "cijfers": [
        ("verkocht", "Verkocht (week)", "periode")]},
]
KANAAL = {k["id"]: k for k in KANALEN}
# Cijfers die alleen uit een koppeling of export komen; die staan niet op het invulscherm.
ALLEEN_EXPORT = {("threads", "profielweergaven"), ("threads", "interacties"), ("threads", "likes"), ("instagram", "weergaven"), ("instagram", "bereik"), ("instagram", "interacties"), ("instagram", "nieuwe_volgers"),
                 ("instagram", "profielbezoeken"), ("facebook", "weergaven"), ("facebook", "bereik"), ("facebook", "interacties"),
                 ("facebook", "nieuwe_volgers"),
                 ("linkedin", "ssi"), ("linkedin", "profielweergaven"), ("linkedin", "impressies"), ("linkedin", "interacties"), ("linkedin", "nieuwe_volgers"),
                 ("linkedin_pagina", "weergaven"), ("linkedin_pagina", "interacties"), ("linkedin_pagina", "nieuwe_volgers"),
                 ("linkedin_pagina", "bezoeken"), ("podcast", "streams"), ("podcast", "uren"), ("podcast", "volgers"),
                 ("youtube", "abonnees"), ("youtube", "weergaven_totaal"), ("youtube", "videos"),
                 ("nieuwsbrief", "abonnees"), ("nieuwsbrief", "openratio")}

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS kansen (
        id serial PRIMARY KEY,
        datum date NOT NULL DEFAULT current_date,
        soort text NOT NULL,
        organisatie text NOT NULL,
        omschrijving text,
        bron text,
        status text NOT NULL DEFAULT 'binnen',
        waarde_cent int,
        notitie text,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS metingen (
        kanaal text NOT NULL,
        sleutel text NOT NULL,
        datum date NOT NULL,
        waarde numeric NOT NULL,
        bron text NOT NULL DEFAULT 'handmatig',
        bijgewerkt timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (kanaal, sleutel, datum))""",
] + mailerlite.MIGRATIES + woo.MIGRATIES + gdata.MIGRATIES + youtube.MIGRATIES + podcast.MIGRATIES + linkedin.MIGRATIES + ssi.MIGRATIES + profielbezoek.MIGRATIES + meta.MIGRATIES + kosten.MIGRATIES + rekening.MIGRATIES


# ---------- berekeningen ----------

def _getal(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return int(v) if v == int(v) else float(v)
    return v


def _verschil(nu, toen):
    if nu is None or toen is None:
        return None
    return _getal(nu) - _getal(toen)


def kanaalcijfers(dagen):
    """Per kanaal en cijfer: huidige waarde en verandering ten opzichte van de vorige periode."""
    vandaag = dt.date.today()
    start, vorige_start = vandaag - dt.timedelta(days=dagen), vandaag - dt.timedelta(days=2 * dagen)
    rijen = q("""SELECT kanaal, sleutel, datum, waarde, bron FROM metingen
                 WHERE datum > %s ORDER BY datum""", (vorige_start - dt.timedelta(days=400),))
    per = {}
    for r in rijen:
        per.setdefault((r["kanaal"], r["sleutel"]), []).append(r)
    uit = []
    for k in KANALEN:
        regels, iets = [], False
        for sleutel, label, soort in k["cijfers"]:
            reeks = per.get((k["id"], sleutel), [])
            if soort == "stand":
                laatste = reeks[-1] if reeks else None
                eerder = [r for r in reeks if r["datum"] <= start]
                waarde = _getal(laatste["waarde"]) if laatste else None
                delta = _verschil(waarde, eerder[-1]["waarde"]) if (laatste and eerder) else None
                peil = laatste["datum"] if laatste else None
            else:
                nu_ = [r["waarde"] for r in reeks if start < r["datum"] <= vandaag]
                toen = [r["waarde"] for r in reeks if vorige_start < r["datum"] <= start]
                waarde = _getal(sum(nu_)) if nu_ else None
                delta = _verschil(waarde, sum(toen)) if (nu_ and toen) else None
                peil = reeks[-1]["datum"] if reeks else None
            iets = iets or waarde is not None
            regels.append({"sleutel": sleutel, "label": label, "soort": soort, "waarde": waarde,
                           "delta": delta, "peil": peil})
        uit.append({**k, "regels": regels, "heeft_cijfers": iets})
    return uit


def kerncijfers(dagen):
    p = (dagen,)
    kansen = q1("""SELECT count(*) FILTER (WHERE datum > current_date - %s) AS nu,
                          count(*) FILTER (WHERE datum > current_date - 2 * %s AND datum <= current_date - %s) AS toen,
                          count(*) FILTER (WHERE status IN ('bevestigd', 'afgerond') AND datum > current_date - %s) AS bevestigd,
                          count(*) FILTER (WHERE status IN ('binnen', 'gesprek')) AS open,
                          coalesce(sum(waarde_cent) FILTER (WHERE status IN ('bevestigd', 'afgerond') AND datum > current_date - %s), 0) AS waarde
                   FROM kansen""", (dagen, dagen, dagen, dagen, dagen))
    kansen["laatste"] = q1("""SELECT organisatie, soort, datum FROM kansen WHERE status IN ('binnen', 'gesprek')
                              ORDER BY datum DESC, id DESC LIMIT 1""")
    filt, params = "", []
    if congres_event():
        filt, params = " AND event_id = %s", [congres_event()]
    kaarten = q1(f"""SELECT coalesce(sum(aantal) FILTER (WHERE besteld > now() - make_interval(days => %s)), 0) AS nu,
                            coalesce(sum(aantal) FILTER (WHERE besteld > now() - make_interval(days => %s * 2)
                                                         AND besteld <= now() - make_interval(days => %s)), 0) AS toen,
                            coalesce(sum(aantal), 0) AS totaal,
                            coalesce(sum(totaal_cent), 0) AS omzet
                     FROM tt_orders WHERE status = 'completed'{filt}""", [dagen, dagen, dagen] + params)
    media = q("""SELECT id, titel, url, bron, binnen FROM items
                 WHERE binnen > now() - make_interval(days => %s)
                   AND (titel ILIKE '%%zerohero%%' OR samenvatting ILIKE '%%zerohero%%'
                        OR titel ILIKE '%%martijn buurman%%' OR samenvatting ILIKE '%%martijn buurman%%'
                        OR titel ILIKE '%%keeper-mindset%%' OR titel ILIKE '%%keeper mindset%%')
                 ORDER BY binnen DESC LIMIT 20""", p)
    werk = q1("""SELECT (SELECT count(*) FROM items WHERE binnen > now() - make_interval(days => %s)) AS berichten,
                        (SELECT count(*) FROM items WHERE binnen > now() - make_interval(days => %s) AND score >= 80) AS top,
                        (SELECT count(*) FROM items WHERE status_op > now() - make_interval(days => %s) AND status = 'geselecteerd') AS geselecteerd,
                        (SELECT count(*) FROM concepten WHERE aangemaakt > now() - make_interval(days => %s)) AS concepten,
                        (SELECT count(*) FROM concepten WHERE aangemaakt > now() - make_interval(days => %s) AND wp_post_id IS NOT NULL) AS in_wp,
                        (SELECT count(*) FROM concepten WHERE gepubliceerd_op > now() - make_interval(days => %s)) AS gepubliceerd""",
              (dagen,) * 6)
    return {"kansen": kansen, "kaarten": kaarten, "media": media, "werk": werk}


def mailerlite_overzicht(dagen):
    """Formulieren met aanmeldingen in de periode, laatste nieuwsbrieven en de status van de koppeling."""
    status = q1("SELECT * FROM koppelingen WHERE naam = 'mailerlite'")
    start = dt.date.today() - dt.timedelta(days=dagen)
    formulieren = q("""SELECT f.*, (SELECT waarde FROM metingen m WHERE m.kanaal = 'formulier' AND m.sleutel = f.id
                                    AND m.datum <= %s ORDER BY m.datum DESC LIMIT 1) AS eerder,
                              (SELECT min(datum) FROM metingen m WHERE m.kanaal = 'formulier' AND m.sleutel = f.id) AS sinds
                       FROM ml_formulieren f ORDER BY f.conversies DESC NULLS LAST""", (start,))
    aanmeldingen, volledig = 0, True
    for f in formulieren:
        if f["eerder"] is not None and f["conversies"] is not None:
            f["in_periode"] = max(0, f["conversies"] - int(f["eerder"]))
            aanmeldingen += f["in_periode"]
        else:
            f["in_periode"] = None
            volledig = False
    campagnes = q("SELECT * FROM ml_campagnes ORDER BY verzonden_op DESC NULLS LAST LIMIT 6")
    return {"status": status, "formulieren": formulieren, "campagnes": campagnes,
            "aanmeldingen": aanmeldingen if formulieren else None, "volledig": volledig, "actief": mailerlite.actief()}


# ---------- schermen ----------

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, periode: int = 30):
    if periode not in PERIODES:
        periode = 30
    mailerlite.start_achtergrond(q)
    woo.start_achtergrond(q)
    gdata.start_achtergrond(q)
    youtube.start_achtergrond(q)
    meta.start_achtergrond(q)
    tickettailor.start_achtergrond(q)
    threads.start_achtergrond(q)
    rekening.start_achtergrond(q)
    yt = youtube.overzicht(q, periode)
    pod = podcast.overzicht(q, periode)
    pod_nodig = podcast.upload_nodig(q)
    li_nodig = linkedin.upload_nodig(q, "profiel")
    lp_nodig = linkedin.upload_nodig(q, "pagina")
    exports_nodig = [x for x in (("Spotify (podcast)", "/dashboard/podcast", pod_nodig),
                                 ("LinkedIn-profiel", "/dashboard/linkedin", li_nodig),
                                 ("LinkedIn-bedrijfspagina", "/dashboard/linkedin", lp_nodig),
                                 ("LinkedIn SSI (screenshot)", "/dashboard/ssi", ssi.upload_nodig(q)),
                                 ("LinkedIn-profielbezoekers (screenshot)", "/dashboard/profielbezoek", profielbezoek.overzicht(q)["nodig"])) if x[2]]
    site = gdata.overzicht(q, periode)
    site["status"] = q1("SELECT * FROM koppelingen WHERE naam = 'google'")
    site["actief"] = gdata.actief()
    site["kansen_dagen"] = max(periode, 28)
    site["kansen"] = gdata.seo_kansen(q, site["kansen_dagen"])
    for k in site["kansen"]:
        k["link"] = seo_artikel_link(k)
    winkel = woo.overzicht(q, periode)
    winkel["status"] = q1("SELECT * FROM koppelingen WHERE naam = 'woocommerce'")
    winkel["actief"] = woo.actief()
    kern = kerncijfers(periode)
    kanalen = kanaalcijfers(periode)
    ml = mailerlite_overzicht(periode)
    for k in kanalen:
        if k["id"] == "nieuwsbrief" and ml["actief"]:
            st = ml["status"]
            k["koppeling"] = ("Gekoppeld met MailerLite" if not st or st["ok"] else "MailerLite: fout bij ophalen")
        if k["id"] == "threads" and threads.actief():
            st = threads.status(q)
            k["koppeling"] = "Gekoppeld met Threads, elke drie uur bijgewerkt" if not st or st["ok"] else "Threads: fout bij ophalen"
        if k["id"] in ("instagram", "facebook") and meta.actief():
            st = meta.status(q)
            k["koppeling"] = "Gekoppeld met Meta, elke drie uur bijgewerkt" if not st or st["ok"] else "Meta: fout bij ophalen"
        if k["id"] == "youtube" and yt["actief"]:
            st = yt["status"]
            k["koppeling"] = ("Gekoppeld met YouTube, groei = verschil in deze periode" if not st or st["ok"] else "YouTube: fout bij ophalen")
    def stand(kanaal, sleutel):
        for k in kanalen:
            if k["id"] == kanaal:
                for r in k["regels"]:
                    if r["sleutel"] == sleutel:
                        return r
        return {}
    open_kansen = q("""SELECT * FROM kansen WHERE status IN ('binnen', 'gesprek')
                       ORDER BY datum DESC, id DESC LIMIT 50""")
    recent = q("""SELECT * FROM kansen WHERE status NOT IN ('binnen', 'gesprek')
                  ORDER BY bijgewerkt DESC LIMIT 10""")
    try:
        kost = kosten.overzicht(q, periode)
    except Exception:  # noqa: BLE001
        kost = None
    return templates.TemplateResponse("dashboard.html", ctx(
        request, pagina="dashboard", periode=periode, kost=kost, periodes=PERIODES, kern=kern, kanalen=kanalen,
        boek=stand("boek", "verkocht"), nieuwsbrief=stand("nieuwsbrief", "abonnees"),
        open_kansen=open_kansen, recent=recent, ml=ml, winkel=winkel, site=site, yt=yt, pod=pod, pod_nodig=pod_nodig, li_nodig=li_nodig, lp_nodig=lp_nodig, exports_nodig=exports_nodig, seo_soorten=gdata.SOORT_KANS, soorten=SOORTEN, statussen=STATUSSEN, bronnen=BRONNEN,
        vandaag=dt.date.today()))


@router.post("/dashboard/kansen")
def kans_nieuw(request: Request, soort: str = Form(...), organisatie: str = Form(...), datum: str = Form(""),
               omschrijving: str = Form(""), bron: str = Form(""), waarde: str = Form(""), notitie: str = Form("")):
    controleer_herkomst(request)
    if soort not in SOORTEN or not organisatie.strip():
        raise HTTPException(400, "Soort en organisatie zijn verplicht")
    try:
        d = dt.date.fromisoformat(datum) if datum else dt.date.today()
    except ValueError:
        d = dt.date.today()
    cent = None
    if waarde.strip():
        try:
            cent = round(float(waarde.replace("€", "").replace(".", "").replace(",", ".").strip()) * 100)
        except ValueError:
            cent = None
    q("""INSERT INTO kansen (datum, soort, organisatie, omschrijving, bron, waarde_cent, notitie)
         VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
      (d, soort, organisatie.strip()[:200], omschrijving.strip()[:500] or None, bron[:60] or None, cent, notitie.strip()[:2000] or None))
    return RedirectResponse("/dashboard#kansen", status_code=303)


@router.post("/dashboard/kansen/{kid}/status", response_class=HTMLResponse)
def kans_status(request: Request, kid: int, status: str = Form(...)):
    controleer_herkomst(request)
    if status not in STATUSSEN:
        raise HTTPException(400, "Onbekende status")
    k = q1("UPDATE kansen SET status = %s, bijgewerkt = now() WHERE id = %s RETURNING *", (status, kid))
    if not k:
        raise HTTPException(404)
    return templates.TemplateResponse("_kans.html", ctx(request, k=k, soorten=SOORTEN, statussen=STATUSSEN))


@router.post("/dashboard/kansen/{kid}/weg", response_class=HTMLResponse)
def kans_weg(request: Request, kid: int):
    controleer_herkomst(request)
    q("DELETE FROM kansen WHERE id = %s RETURNING id", (kid,))
    return HTMLResponse("")


@router.get("/dashboard/cijfers", response_class=HTMLResponse)
def cijfers_invoer(request: Request, datum: str = ""):
    try:
        d = dt.date.fromisoformat(datum) if datum else dt.date.today()
    except ValueError:
        d = dt.date.today()
    bestaand = {(r["kanaal"], r["sleutel"]): _getal(r["waarde"])
                for r in q("SELECT kanaal, sleutel, waarde FROM metingen WHERE datum = %s", (d,))}
    laatste = {(r["kanaal"], r["sleutel"]): (_getal(r["waarde"]), r["datum"])
               for r in q("""SELECT DISTINCT ON (kanaal, sleutel) kanaal, sleutel, waarde, datum
                             FROM metingen ORDER BY kanaal, sleutel, datum DESC""")}
    return templates.TemplateResponse("cijfers.html", ctx(
        request, pagina="dashboard", datum=d, kanalen=KANALEN, bestaand=bestaand, laatste=laatste, alleen_export=ALLEEN_EXPORT))


@router.post("/dashboard/cijfers")
async def cijfers_opslaan(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    try:
        d = dt.date.fromisoformat(str(form.get("datum") or ""))
    except ValueError:
        d = dt.date.today()
    n = 0
    for k in KANALEN:
        for sleutel, _label, _soort in k["cijfers"]:
            if (k["id"], sleutel) in ALLEEN_EXPORT:
                continue
            ruw = str(form.get(f"{k['id']}__{sleutel}") or "").strip().replace(".", "").replace(",", ".")
            if not ruw:
                continue
            try:
                w = float(ruw)
            except ValueError:
                continue
            q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES (%s, %s, %s, %s, 'handmatig')
                 ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'handmatig', bijgewerkt = now()
                 RETURNING 1""", (k["id"], sleutel, d, w))
            n += 1
    return RedirectResponse(f"/dashboard?opgeslagen={n}", status_code=303)


@router.post("/dashboard/ververs/mailerlite")
def ververs_mailerlite(request: Request):
    controleer_herkomst(request)
    ok, fout = mailerlite.ververs(q)
    return RedirectResponse("/dashboard?ververst=" + ("mailerlite" if ok else "fout") + "#nieuwsbrief", status_code=303)


@router.post("/dashboard/ververs/woocommerce")
def ververs_woocommerce(request: Request):
    controleer_herkomst(request)
    ok, _fout = woo.ververs(q)
    return RedirectResponse("/dashboard?ververst=" + ("webshop" if ok else "webshopfout") + "#webshop", status_code=303)



# ---------- lezingaanvraag via MailerLite (webhook via n8n) ----------

LEZING_GROEP = os.environ.get("ZEROHERO_MAILERLITE_LEZING_GROEP", "").strip()
VELD_NAMEN = {"name": "Naam", "last_name": "Achternaam", "company": "Organisatie", "phone": "Telefoon",
              "city": "Plaats", "z_i_p": "Postcode", "country": "Land"}


def _gebeurtenissen(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("events"), list):
        return body["events"]
    return [body] if isinstance(body, dict) else []


def _groep_ids(e):
    ids = set()
    g = e.get("group")
    if isinstance(g, dict) and g.get("id") is not None:
        ids.add(str(g["id"]))
    for bron in (e, e.get("subscriber") or {}):
        for g in bron.get("groups") or []:
            if isinstance(g, dict) and g.get("id") is not None:
                ids.add(str(g["id"]))
    return ids


@router.post("/intern/mailerlite")
async def intern_mailerlite(request: Request):
    """Ontvangt (via n8n) een MailerLite-webhook. Is het een aanmelding in de groep van het lezingformulier,
    dan wordt het een kans in het logboek. Antwoord: {"kans": bool, "tekst": Telegram-melding}."""
    if not CONCEPT_SECRET or request.headers.get("X-Doellijn-Secret") != CONCEPT_SECRET:
        raise HTTPException(403)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Geen JSON")
    if isinstance(body, dict) and isinstance(body.get("body"), (dict, list)):
        body = body["body"]  # n8n stuurt soms het hele webhookverzoek door
    teksten = []
    for e in _gebeurtenissen(body):
        if not isinstance(e, dict):
            continue
        sub = e.get("subscriber") if isinstance(e.get("subscriber"), dict) else e
        email = str(sub.get("email") or "").strip()
        if not LEZING_GROEP:
            continue
        groepen = _groep_ids(e)
        if LEZING_GROEP not in groepen and email and mailerlite.actief():
            # Niet elk soort seintje noemt de groep: vraag de groepen van deze abonnee op bij MailerLite.
            try:
                info = mailerlite._get("/subscribers/" + urllib.parse.quote(email)).get("data") or {}
                groepen |= {str(g.get("id")) for g in info.get("groups") or [] if isinstance(g, dict)}
                if not isinstance(sub.get("fields"), dict) and isinstance(info.get("fields"), dict):
                    sub = {**sub, "fields": info["fields"]}
            except Exception:  # noqa: BLE001
                pass
        if LEZING_GROEP not in groepen:
            continue
        if _al_bekend(email):
            continue  # al eerder als kans gemaakt (webhook of controle)
        teksten.append(_maak_lezingkans(email, sub.get("fields")))
    return {"kans": bool(teksten), "tekst": "\n\n".join(teksten)}



def _al_bekend(email):
    return bool(email) and q1("SELECT id FROM kansen WHERE soort = 'lezing' AND notitie LIKE %s", ("%" + email + "%",))


def _maak_lezingkans(email, velden):
    velden = velden if isinstance(velden, dict) else {}
    naam = " ".join(str(velden.get(k) or "").strip() for k in ("name", "last_name")).strip()
    organisatie = str(velden.get("company") or "").strip() or naam or (email.split("@")[-1] if "@" in email else "Onbekend")
    regels = [f"E-mail: {email}"] if email else []
    for k, v in velden.items():
        if v not in (None, "") and k not in ("name", "last_name", "company", "email"):
            regels.append(f"{VELD_NAMEN.get(k, k.replace('_', ' ').capitalize())}: {v}")
    omschrijving = "Aanvraag via het lezingformulier op zerohero.one" + (f" ({naam})" if naam and naam != organisatie else "")
    k = q1("""INSERT INTO kansen (soort, organisatie, omschrijving, bron, notitie)
              VALUES ('lezing', %s, %s, 'Website of formulier', %s) RETURNING id""",
           (organisatie[:200], omschrijving[:500], "\n".join(regels)[:2000] or None))
    esc = html.escape
    return ("🎤 <b>Nieuwe lezingaanvraag</b>\n" + esc(organisatie) + (f" ({esc(naam)})" if naam and naam != organisatie else "") +
            ("\n" + esc(email) if email else "") + "".join("\n" + esc(r) for r in regels[1:6]) +
            f'\n<a href="{omgeving.BASIS}/dashboard#kans-{k["id"]}">Open in het dashboard</a>')


@router.post("/intern/mailerlite/peil")
def intern_mailerlite_peil(request: Request):
    """Kijkt (elke paar minuten, aangeroepen door n8n) in de lezinggroep naar nieuwe aanmeldingen van de
    afgelopen drie dagen en maakt er kansen van. Werkt ook als de MailerLite-webhook niets stuurt."""
    if not CONCEPT_SECRET or request.headers.get("X-Doellijn-Secret") != CONCEPT_SECRET:
        raise HTTPException(403)
    if not (LEZING_GROEP and mailerlite.actief()):
        return {"kans": False, "tekst": "", "reden": "geen groep of sleutel ingesteld"}
    grens = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=3)
    teksten = []
    for sub in mailerlite._get(f"/groups/{LEZING_GROEP}/subscribers", {"limit": 50}).get("data") or []:
        email = str(sub.get("email") or "").strip()
        tijd = str(sub.get("subscribed_at") or sub.get("created_at") or "")[:19].replace("T", " ")
        try:
            nieuw = dt.datetime.fromisoformat(tijd) > grens
        except ValueError:
            nieuw = False
        if not email or not nieuw or _al_bekend(email):
            continue
        teksten.append(_maak_lezingkans(email, sub.get("fields")))
    return {"kans": bool(teksten), "tekst": "\n\n".join(teksten)}


@router.post("/dashboard/ververs/google")
def ververs_google(request: Request):
    controleer_herkomst(request)
    ok, _fout = gdata.ververs(q)
    return RedirectResponse("/dashboard?ververst=" + ("google" if ok else "googlefout") + "#website", status_code=303)



# ---------- linkmaker (UTM-links) ----------

LINK_KANALEN = [
    ("linkedin-post", "LinkedIn: in de post", "linkedin", "social", "post"),
    ("linkedin-reactie", "LinkedIn: eerste reactie onder je post", "linkedin", "social", "reactie"),
    ("linkedin-artikel", "LinkedIn: artikel", "linkedin", "social", "artikel"),
    ("linkedin-profiel", "LinkedIn: profiel of uitgelicht", "linkedin", "social", "profiel"),
    ("facebook-post", "Facebook: bericht", "facebook", "social", "post"),
    ("facebook-reactie", "Facebook: reactie", "facebook", "social", "reactie"),
    ("instagram-bio", "Instagram: link in bio", "instagram", "social", "bio"),
    ("instagram-story", "Instagram: story", "instagram", "social", "story"),
    ("threads", "Threads", "threads", "social", "post"),
    ("youtube", "YouTube: beschrijving Keepersjournaal", "youtube", "video", "beschrijving"),
    ("podcast", "Podcast: shownotes", "podcast", "audio", "shownotes"),
    ("nieuwsbrief", "Nieuwsbrief (MailerLite)", "nieuwsbrief", "email", "nieuwsbrief"),
    ("whatsapp", "WhatsApp", "whatsapp", "messaging", "bericht"),
    ("lezing", "Lezing of presentatie (slide of QR-code)", "lezing", "offline", "slide"),
]


@router.get("/links", response_class=HTMLResponse)
def linkmaker(request: Request):
    paginas = q("""SELECT pad, max(titel) AS titel, sum(weergaven) AS w FROM ga_paginas
                   WHERE datum > current_date - 90 GROUP BY pad ORDER BY w DESC LIMIT 40""")
    gepubliceerd = q("""SELECT blog_titel AS titel, wp_link AS url FROM concepten
                        WHERE gepubliceerd_op IS NOT NULL AND wp_link IS NOT NULL ORDER BY gepubliceerd_op DESC LIMIT 15""")
    links = q("""SELECT l.*, coalesce((SELECT sum(c.sessies) FROM ga_campagnes c
                                       WHERE lower(c.campagne) = lower(l.campagne) AND lower(c.bron) = lower(l.bron)
                                         AND (l.inhoud IS NULL OR l.inhoud = '' OR lower(c.inhoud) = lower(l.inhoud))), 0) AS bezoeken
                 FROM utm_links l ORDER BY l.aangemaakt DESC LIMIT 30""")
    campagnes = q("""SELECT campagne, bron, medium, sum(sessies) AS sessies, min(datum) AS eerste, max(datum) AS laatste
                     FROM ga_campagnes WHERE datum > current_date - 90 GROUP BY campagne, bron, medium
                     ORDER BY sessies DESC LIMIT 20""")
    return templates.TemplateResponse("links.html", ctx(
        request, pagina="links", kanalen=LINK_KANALEN, paginas=paginas, gepubliceerd=gepubliceerd,
        links=links, campagnes=campagnes))


@router.post("/links")
async def link_bewaren(request: Request):
    controleer_herkomst(request)
    try:
        d = await request.json()
    except ValueError:
        raise HTTPException(400)
    link = str(d.get("link") or "")[:2000]
    if not link.startswith("http"):
        raise HTTPException(400, "Geen geldige link")
    bestaand = q1("SELECT id FROM utm_links WHERE link = %s", (link,))
    if not bestaand:
        q("""INSERT INTO utm_links (url, bron, medium, campagne, inhoud, link) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
          (str(d.get("url") or "")[:1000], str(d.get("bron") or "")[:100], str(d.get("medium") or "")[:100],
           str(d.get("campagne") or "")[:200], str(d.get("inhoud") or "")[:200], link))
    return {"ok": True}


@router.post("/links/{lid}/weg", response_class=HTMLResponse)
def link_weg(request: Request, lid: int):
    controleer_herkomst(request)
    q("DELETE FROM utm_links WHERE id = %s RETURNING 1", (lid,))
    return HTMLResponse("")


@router.post("/dashboard/ververs/youtube")
def ververs_youtube(request: Request):
    controleer_herkomst(request)
    ok, _fout = youtube.ververs(q)
    return RedirectResponse("/bereik/youtube?ververst=" + ("ok" if ok else "fout"), status_code=303)


# ---------- podcast: exports uit Spotify for Creators ----------

@router.get("/dashboard/podcast", response_class=HTMLResponse)
def podcast_upload_scherm(request: Request):
    uploads = q("SELECT * FROM pod_uploads ORDER BY moment DESC LIMIT 12")
    return templates.TemplateResponse("podcast.html", ctx(
        request, pagina="dashboard", uploads=uploads, soorten=podcast.SOORTEN, resultaat=None, nodig=podcast.upload_nodig(q)))


@router.post("/dashboard/podcast", response_class=HTMLResponse)
async def podcast_upload(request: Request, bestanden: list[UploadFile] = File(...)):
    controleer_herkomst(request)
    resultaat, totaal, tot, herkend = [], 0, None, 0
    for b in bestanden[:40]:
        naam = (b.filename or "bestand").rsplit("/", 1)[-1]
        inhoud = await b.read(5_000_001)
        if not naam.lower().endswith(".csv") or len(inhoud) > 5_000_000:
            resultaat.append({"naam": naam, "soort": None, "fout": "Geen CSV-bestand (of groter dan 5 MB)."})
            continue
        try:
            soort, regels, laatste = podcast.verwerk(q, naam, inhoud)
        except Exception as e:  # noqa: BLE001
            resultaat.append({"naam": naam, "soort": None, "fout": f"Kon het bestand niet lezen ({type(e).__name__})."})
            continue
        if not soort:
            resultaat.append({"naam": naam, "soort": None, "fout": "Niet herkend als Spotify-export."})
            continue
        herkend += 1
        totaal += regels
        if laatste and (tot is None or laatste > tot):
            tot = laatste
        resultaat.append({"naam": naam, "soort": podcast.SOORTEN[soort][0], "regels": regels, "laatste": laatste})
    if herkend:
        podcast.log_upload(q, herkend, totaal, tot)
    uploads = q("SELECT * FROM pod_uploads ORDER BY moment DESC LIMIT 12")
    return templates.TemplateResponse("podcast.html", ctx(
        request, pagina="dashboard", uploads=uploads, soorten=podcast.SOORTEN, resultaat=resultaat, herkend=herkend,
        nodig=podcast.upload_nodig(q)))


# ---------- verdiepingspagina's achter de kanalen op het tabblad Bereik ----------

DETAILKANALEN = {"linkedin", "linkedin_pagina", "instagram", "facebook", "threads", "boek"}


@router.get("/bereik/youtube", response_class=HTMLResponse)
def bereik_youtube(request: Request, periode: int = 90, sorteer: str = "nieuw"):
    if periode not in PERIODES:
        periode = 90
    yt = youtube.detail(q, periode, sorteer)
    k = KANAAL["youtube"]
    regels = next(x for x in kanaalcijfers(periode) if x["id"] == "youtube")["regels"]
    return templates.TemplateResponse("bereik_youtube.html", ctx(
        request, pagina="dashboard", periode=periode, periodes=PERIODES, yt=yt, regels=regels, kanaal=k,
        sorteringen=youtube.SORTERINGEN))


@router.get("/bereik/podcast", response_class=HTMLResponse)
def bereik_podcast(request: Request, periode: int = 90, sorteer: str = "nieuw"):
    if periode not in PERIODES:
        periode = 90
    pod = podcast.detail(q, periode, sorteer)
    return templates.TemplateResponse("bereik_podcast.html", ctx(
        request, pagina="dashboard", periode=periode, periodes=PERIODES, pod=pod, pod_nodig=podcast.upload_nodig(q),
        sorteringen=podcast.SORTERINGEN, weekdagen=podcast.WEEKDAGEN))


@router.get("/bereik/{kanaal}", response_class=HTMLResponse)
def bereik_kanaal(request: Request, kanaal: str, periode: int = 90):
    if kanaal not in DETAILKANALEN:
        raise HTTPException(404)
    if periode not in PERIODES:
        periode = 90
    k = KANAAL[kanaal]
    regels = next(x for x in kanaalcijfers(periode) if x["id"] == kanaal)["regels"]
    li = lp = None
    if kanaal == "linkedin":
        lp = linkedin.detail_profiel(q, periode)
        lp["ssi"] = ssi.overzicht(q)
        lp["bezoek"] = profielbezoek.overzicht(q)
    if kanaal == "linkedin_pagina":
        li = linkedin.detail_pagina(q, periode)
    if lp is not None or li is not None:
        regels = [r for r in regels if (kanaal, r["sleutel"]) not in ALLEEN_EXPORT]
    mp = None
    if kanaal in ("instagram", "facebook"):
        mp = {"actief": meta.actief(), "status": meta.status(q), "posts": meta.posts(q, kanaal, periode)}
        if meta.actief():
            k = dict(k, koppeling="Gekoppeld met Meta, elke drie uur bijgewerkt. Dagcijfers bouwen zich op vanaf de koppeling (Meta geeft hooguit 28 dagen terug).")
    if kanaal == "threads":
        threads.start_achtergrond(q)
        mp = {"actief": threads.actief(), "status": threads.status(q), "posts": meta.posts(q, "threads", periode),
              "ververs_url": "/dashboard/ververs/threads", "script": "threads.sh", "bron": "Threads"}
        if threads.actief():
            k = dict(k, koppeling="Gekoppeld met Threads, elke drie uur bijgewerkt. Profielweergaven gaan 30 dagen terug, interacties bouwen zich per dag op vanaf de koppeling.")
    reeksen = []
    for sleutel, label, soort in k["cijfers"]:
        if (lp is not None or li is not None) and (kanaal, sleutel) in ALLEEN_EXPORT:
            continue
        rijen = q("""SELECT datum, waarde, bron FROM metingen WHERE kanaal = %s AND sleutel = %s
                     AND datum > current_date - %s ORDER BY datum""", (kanaal, sleutel, periode))
        alle = q("SELECT count(*) AS n, min(datum) AS vanaf FROM metingen WHERE kanaal = %s AND sleutel = %s", (kanaal, sleutel))[0]
        punten = [{"datum": r["datum"], "waarde": _getal(r["waarde"]), "bron": r["bron"]} for r in rijen]
        for i, pnt in enumerate(punten):
            pnt["verschil"] = _verschil(pnt["waarde"], punten[i - 1]["waarde"]) if (i and soort == "stand") else None
        reeksen.append({"sleutel": sleutel, "label": label, "soort": soort, "punten": punten, "n": alle["n"], "vanaf": alle["vanaf"]})
    return templates.TemplateResponse("bereik_kanaal.html", ctx(
        request, pagina="dashboard", periode=periode, periodes=PERIODES, kanaal=k, regels=regels, reeksen=reeksen,
        li=li, lp=lp, mp=mp, li_nodig=linkedin.upload_nodig(q, "pagina" if li is not None else "profiel") if (li is not None or lp is not None) else False))


@router.get("/dashboard/linkedin", response_class=HTMLResponse)
def linkedin_upload_scherm(request: Request):
    return templates.TemplateResponse("linkedin_upload.html", ctx(
        request, pagina="dashboard", exports=linkedin.stand_exports(q), resultaat=None,
        uploads=q("SELECT * FROM li_uploads ORDER BY moment DESC LIMIT 20")))


@router.post("/dashboard/linkedin", response_class=HTMLResponse)
async def linkedin_upload(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    resultaat, herkend = [], 0
    for veld in linkedin.EXPORTS:
        for b in form.getlist(veld)[:3]:
            if not hasattr(b, "read") or not b.filename:
                continue
            naam = b.filename.rsplit("/", 1)[-1]
            inhoud = await b.read(10_000_001)
            regel = {"veld": veld, "naam": naam, "bladen": [], "fout": None, "melding": None}
            resultaat.append(regel)
            if not naam.lower().endswith((".xls", ".xlsx", ".csv")) or len(inhoud) > 10_000_000:
                regel["fout"] = "Geen .xls-, .xlsx- of .csv-bestand (of groter dan 10 MB)."
                continue
            try:
                export, bladen = linkedin.verwerk(q, naam, inhoud)
            except Exception as e:  # noqa: BLE001
                regel["fout"] = f"Kon het bestand niet lezen ({type(e).__name__})."
                continue
            if not bladen:
                regel["fout"] = "Niet herkend als LinkedIn-export."
                continue
            if export != veld:
                regel["melding"] = f"Dit bestand is de export {linkedin.EXPORTS[export]['naam']}; zo is het ook verwerkt."
            herkend += 1
            tot = max((l_ for _s, _b, _r, l_ in bladen if l_), default=None)
            linkedin.log_upload(q, export, sum(r_ for _s, _b, r_, _l in bladen), tot)
            regel["bladen"] = [{"soort": linkedin.SOORTEN.get(s_, s_), "blad": b_, "regels": r_, "laatste": l_} for s_, b_, r_, l_ in bladen]
    return templates.TemplateResponse("linkedin_upload.html", ctx(
        request, pagina="dashboard", exports=linkedin.stand_exports(q), resultaat=resultaat, herkend=herkend,
        uploads=q("SELECT * FROM li_uploads ORDER BY moment DESC LIMIT 20")))


# ---------- de statistiekendatabase als bestand ----------

@router.get("/dashboard/statistieken.csv")
def statistieken_csv(request: Request):
    """Alle metingen van alle kanalen, dag voor dag, als CSV (opent in Excel)."""
    import csv
    import io
    from fastapi.responses import Response
    namen = {k["id"]: k["naam"] for k in KANALEN}
    labels = {(k["id"], s_): l_ for k in KANALEN for s_, l_, _ in k["cijfers"]}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["kanaal", "kanaalnaam", "cijfer", "omschrijving", "datum", "waarde", "bron"])
    for r in q("SELECT kanaal, sleutel, datum, waarde, bron FROM metingen ORDER BY kanaal, sleutel, datum"):
        waarde = _getal(r["waarde"])
        w.writerow([r["kanaal"], namen.get(r["kanaal"], ""), r["sleutel"], labels.get((r["kanaal"], r["sleutel"]), ""),
                    r["datum"].isoformat(), str(waarde).replace(".", ","), r["bron"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="zerohero-statistieken-{dt.date.today().isoformat()}.csv"'})


# ---------- Social Selling Index: screenshot uploaden, laten aflezen, controleren, bewaren ----------

def _ssi_scherm(request, **extra):
    return templates.TemplateResponse("ssi.html", ctx(
        request, pagina="dashboard", o=ssi.overzicht(q), velden=ssi.VELDEN, onderdelen=ssi.ONDERDELEN,
        vandaag=dt.date.today(), **extra))


@router.get("/dashboard/ssi", response_class=HTMLResponse)
def ssi_scherm(request: Request):
    return _ssi_scherm(request, gelezen=None)


@router.post("/dashboard/ssi/lees", response_class=HTMLResponse)
async def ssi_lees(request: Request, screenshot: UploadFile = File(...)):
    controleer_herkomst(request)
    inhoud = await screenshot.read(15_000_001)
    if len(inhoud) > 15_000_000:
        return _ssi_scherm(request, gelezen=None, fout="De screenshot is groter dan 15 MB.")
    try:
        data, mime = ssi.verklein(inhoud)
    except Exception:  # noqa: BLE001
        return _ssi_scherm(request, gelezen=None, fout="Dit is geen afbeelding die de cockpit kan lezen (gebruik PNG of JPG).")
    gelezen, fout = ssi.lees(data, mime)
    sid = q("INSERT INTO ssi_screenshots (mime, data, gelezen) VALUES (%s, %s, %s::jsonb) RETURNING id",
            (mime, data, json.dumps(gelezen)))[0]["id"]
    som = sum(float(gelezen.get(k) or 0) for k, _l in ssi.ONDERDELEN) if gelezen else None
    return _ssi_scherm(request, gelezen=gelezen, screenshot_id=sid, leesfout=fout, som=som)


@router.post("/dashboard/ssi/opslaan")
async def ssi_opslaan(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    try:
        datum = dt.date.fromisoformat(str(form.get("datum") or ""))
    except ValueError:
        datum = dt.date.today()
    sid = str(form.get("screenshot_id") or "")
    sid = int(sid) if sid.isdigit() else None
    velden = {k: form.get(k) for k, _l, _g in ssi.VELDEN}
    if not str(velden.get("ssi") or "").strip():
        return _ssi_scherm(request, gelezen=velden, screenshot_id=sid, fout="Vul ten minste de SSI zelf in.")
    ssi.opslaan(q, datum, velden, sid, "screenshot" if sid else "handmatig")
    return RedirectResponse("/dashboard/ssi?opgeslagen=1", status_code=303)


@router.get("/dashboard/ssi/screenshot/{sid}")
def ssi_screenshot(sid: int):
    from fastapi.responses import Response
    r = q1("SELECT mime, data FROM ssi_screenshots WHERE id = %s", (sid,))
    if not r:
        raise HTTPException(404)
    return Response(bytes(r["data"]), media_type=r["mime"] or "image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/dashboard/ssi/{datum}/weg")
def ssi_weg(request: Request, datum: str):
    controleer_herkomst(request)
    try:
        d = dt.date.fromisoformat(datum)
    except ValueError:
        raise HTTPException(404)
    q("DELETE FROM ssi_metingen WHERE datum = %s RETURNING 1", (d,))
    q("DELETE FROM metingen WHERE kanaal = 'linkedin' AND (sleutel = 'ssi' OR sleutel LIKE 'ssi\\_%%') AND datum = %s RETURNING 1", (d,))
    return RedirectResponse("/dashboard/ssi", status_code=303)


# ---------- LinkedIn-profielbezoekers: screenshot uploaden, laten aflezen, controleren, bewaren ----------

def _pb_scherm(request, **extra):
    return templates.TemplateResponse("profielbezoek.html", ctx(
        request, pagina="dashboard", o=profielbezoek.overzicht(q), velden=profielbezoek.VELDEN, vandaag=dt.date.today(), **extra))


@router.get("/dashboard/profielbezoek", response_class=HTMLResponse)
def pb_scherm(request: Request):
    return _pb_scherm(request, gelezen=None)


@router.post("/dashboard/profielbezoek/lees", response_class=HTMLResponse)
async def pb_lees(request: Request, screenshot: UploadFile = File(...)):
    controleer_herkomst(request)
    inhoud = await screenshot.read(15_000_001)
    if len(inhoud) > 15_000_000:
        return _pb_scherm(request, gelezen=None, fout="De screenshot is groter dan 15 MB.")
    try:
        data, mime = ssi.verklein(inhoud)
    except Exception:  # noqa: BLE001
        return _pb_scherm(request, gelezen=None, fout="Dit is geen afbeelding die de cockpit kan lezen (gebruik PNG of JPG).")
    gelezen, fout = profielbezoek.lees(data, mime)
    sid = q("INSERT INTO ssi_screenshots (mime, data, gelezen) VALUES (%s, %s, %s::jsonb) RETURNING id",
            (mime, data, json.dumps(gelezen)))[0]["id"]
    return _pb_scherm(request, gelezen=gelezen, screenshot_id=sid, leesfout=fout,
                      dagen=profielbezoek.dagen_uit(gelezen.get("periode")))


@router.post("/dashboard/profielbezoek/opslaan")
async def pb_opslaan(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    try:
        datum = dt.date.fromisoformat(str(form.get("datum") or ""))
    except ValueError:
        datum = dt.date.today()
    sid = str(form.get("screenshot_id") or "")
    sid = int(sid) if sid.isdigit() else None
    velden = {k: form.get(k) for k, _l, _g in profielbezoek.VELDEN}
    try:
        profielbezoek.opslaan(q, datum, velden, sid, "screenshot" if sid else "handmatig")
    except ValueError as e:
        return _pb_scherm(request, gelezen=velden, screenshot_id=sid, fout=str(e))
    return RedirectResponse("/dashboard/profielbezoek?opgeslagen=1", status_code=303)


@router.post("/dashboard/profielbezoek/{datum}/{dagen}/weg")
def pb_weg(request: Request, datum: str, dagen: int):
    controleer_herkomst(request)
    try:
        d = dt.date.fromisoformat(datum)
    except ValueError:
        raise HTTPException(404)
    q("DELETE FROM lp_profielbezoek WHERE datum = %s AND periode_dagen = %s RETURNING 1", (d, dagen))
    q("DELETE FROM metingen WHERE kanaal = 'linkedin' AND sleutel = %s AND datum = %s RETURNING 1", (f"profielbezoekers_{dagen}d", d))
    if dagen <= 31:
        q("DELETE FROM metingen WHERE kanaal = 'linkedin' AND sleutel = 'profielweergaven' AND datum = %s AND bron = 'screenshot' RETURNING 1", (d,))
    return RedirectResponse("/dashboard/profielbezoek", status_code=303)


@router.post("/dashboard/ververs/meta")
def ververs_meta(request: Request, terug: str = Form("/bereik/instagram")):
    controleer_herkomst(request)
    ok, _melding = meta.ververs(q)
    terug = terug if terug.startswith("/bereik/") else "/bereik/instagram"
    return RedirectResponse(terug + ("?ververst=ok" if ok else "?ververst=fout"), status_code=303)


@router.post("/dashboard/ververs/threads")
def ververs_threads(request: Request):
    controleer_herkomst(request)
    ok, _melding = threads.ververs(q)
    return RedirectResponse("/bereik/threads" + ("?ververst=ok" if ok else "?ververst=fout"), status_code=303)


@router.post("/dashboard/spaarstand")
def spaarstand_opslaan(request: Request, zoekstand: str = Form(""), budget: str = Form("")):
    controleer_herkomst(request)
    if zoekstand in rekening.ZOEKSTANDEN:
        rekening.bewaar(q, "zoekstand", zoekstand)
    b = budget.strip().replace(",", ".").replace("$", "").strip()
    try:
        rekening.bewaar(q, "budget_maand", str(round(float(b), 2)) if b and float(b) > 0 else "")
    except ValueError:
        pass
    return RedirectResponse("/dashboard?spaar=1#doellijn", status_code=303)


@router.post("/dashboard/ververs/anthropic")
def ververs_anthropic(request: Request):
    controleer_herkomst(request)
    ok, melding = rekening.ververs(q)
    return RedirectResponse("/dashboard?rekening=" + ("ok" if ok else "fout") + "#doellijn", status_code=303)


@router.post("/dashboard/kosten")
def kosten_opslaan(request: Request, vast: str = Form(""), koers: str = Form("")):
    controleer_herkomst(request)
    for sleutel, waarde in (("vaste_kosten", vast.strip()), ("koers_eur_usd", koers.strip().replace(",", "."))):
        if sleutel == "koers_eur_usd":
            try:
                if not 0.3 < float(waarde) < 3:
                    continue
            except ValueError:
                continue
        q("""INSERT INTO instellingen (sleutel, waarde, bijgewerkt) VALUES (%s, %s, now())
             ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now() RETURNING 1""", (sleutel, waarde))
    return RedirectResponse("/dashboard?kosten=1#doellijn", status_code=303)
