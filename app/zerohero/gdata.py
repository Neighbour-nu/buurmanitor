"""Koppeling met Google Analytics 4 (Data API) en Google Search Console.

Werkt met een serviceaccount (alleen-lezen). De sleutel staat base64-gecodeerd in GOOGLE_SLEUTEL_B64
in ~/cockpit/.env (instellen met deploy/google.sh). Haalt elke drie uur dagcijfers op en bewaart ze:
- GA4: gebruikers, sessies, betrokken sessies, paginaweergaven per dag; pagina's en bronnen per dag.
- Search Console: klikken, vertoningen en positie per dag; zoektermen en pagina's per dag.

Let op: GA4 ziet alleen bezoekers die cookies accepteren; Search Console telt alles wat via Google komt.
"""
from . import omgeving
import base64
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROPERTY = os.environ.get("ZEROHERO_GA4_PROPERTY", "441520596").strip()
GSC_SITE = os.environ.get("ZEROHERO_GSC_SITE", "").strip()
INTERVAL = 3 * 3600
SCOPES = "https://www.googleapis.com/auth/analytics.readonly https://www.googleapis.com/auth/webmasters.readonly"

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS ga_dag (
        datum date PRIMARY KEY, gebruikers int, nieuwe int, sessies int, betrokken int, weergaven int,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS ga_paginas (
        datum date NOT NULL, pad text NOT NULL, titel text, weergaven int, gebruikers int,
        PRIMARY KEY (datum, pad))""",
    """CREATE TABLE IF NOT EXISTS ga_bronnen (
        datum date NOT NULL, kanaal text NOT NULL, bron text NOT NULL, sessies int, gebruikers int,
        PRIMARY KEY (datum, kanaal, bron))""",
    """CREATE TABLE IF NOT EXISTS ga_campagnes (
        datum date NOT NULL, bron text NOT NULL, medium text NOT NULL, campagne text NOT NULL, inhoud text NOT NULL DEFAULT '',
        sessies int, gebruikers int, PRIMARY KEY (datum, bron, medium, campagne, inhoud))""",
    """CREATE TABLE IF NOT EXISTS utm_links (
        id serial PRIMARY KEY, url text NOT NULL, bron text, medium text, campagne text, inhoud text, link text NOT NULL,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS gsc_dag (
        datum date PRIMARY KEY, klikken int, vertoningen int, positie numeric, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS gsc_zoektermen (
        datum date NOT NULL, term text NOT NULL, klikken int, vertoningen int, positie numeric,
        PRIMARY KEY (datum, term))""",
    """CREATE TABLE IF NOT EXISTS gsc_term_paginas (
        term text NOT NULL, pagina text NOT NULL, klikken int, vertoningen int, positie numeric,
        bijgewerkt timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (term, pagina))""",
]


def _sleutel():
    ruw = os.environ.get("ZEROHERO_GOOGLE_SLEUTEL_B64", "").strip()
    if not ruw:
        return None
    try:
        return json.loads(base64.b64decode(ruw))
    except Exception:  # noqa: BLE001
        return None


def actief():
    return _sleutel() is not None


_token = {"waarde": None, "tot": 0}


def _toegang():
    if _token["waarde"] and _token["tot"] > time.time() + 60:
        return _token["waarde"]
    from google.auth import crypt, jwt
    info = _sleutel()
    if not info:
        raise RuntimeError("Geen Google-sleutel ingesteld")
    nu = int(time.time())
    signer = crypt.RSASigner.from_service_account_info(info)
    claim = {"iss": info["client_email"], "scope": SCOPES, "aud": info.get("token_uri", "https://oauth2.googleapis.com/token"),
             "iat": nu, "exp": nu + 3600}
    assertion = jwt.encode(signer, claim)
    if isinstance(assertion, bytes):
        assertion = assertion.decode()
    data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}).encode()
    with urllib.request.urlopen(urllib.request.Request(claim["aud"], data=data, method="POST"), timeout=20) as r:
        antw = json.loads(r.read())
    _token.update(waarde=antw["access_token"], tot=nu + int(antw.get("expires_in", 3600)))
    return _token["waarde"]


def _api(url, body=None, methode=None):
    h = {"Authorization": "Bearer " + _toegang(), "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=methode or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def serviceaccount():
    info = _sleutel() or {}
    return info.get("client_email", "")


# ---------- Google Analytics 4 ----------

def ga_rapport(dimensies, metrics, start, eind, limiet=10000):
    body = {"dateRanges": [{"startDate": start, "endDate": eind}], "dimensions": [{"name": d} for d in dimensies],
            "metrics": [{"name": m} for m in metrics], "limit": limiet, "keepEmptyRows": False}
    r = _api(f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY}:runReport", body)
    rijen = []
    for rij in r.get("rows") or []:
        d = [x.get("value") for x in rij.get("dimensionValues") or []]
        m = [x.get("value") for x in rij.get("metricValues") or []]
        rijen.append((d, m))
    return rijen


def _datum(v):
    return dt.date(int(v[:4]), int(v[4:6]), int(v[6:8])) if v and len(v) == 8 else dt.date.fromisoformat(v)


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def ga_ophalen(q, dagen):
    start, eind = f"{dagen}daysAgo", "today"
    for (d, m) in ga_rapport(["date"], ["activeUsers", "newUsers", "sessions", "engagedSessions", "screenPageViews"], start, eind):
        q("""INSERT INTO ga_dag (datum, gebruikers, nieuwe, sessies, betrokken, weergaven, bijgewerkt) VALUES (%s,%s,%s,%s,%s,%s, now())
             ON CONFLICT (datum) DO UPDATE SET gebruikers = EXCLUDED.gebruikers, nieuwe = EXCLUDED.nieuwe, sessies = EXCLUDED.sessies,
               betrokken = EXCLUDED.betrokken, weergaven = EXCLUDED.weergaven, bijgewerkt = now() RETURNING 1""",
          (_datum(d[0]), *[_i(x) for x in m]))
    for (d, m) in ga_rapport(["date", "pagePath", "pageTitle"], ["screenPageViews", "activeUsers"], start, eind):
        q("""INSERT INTO ga_paginas (datum, pad, titel, weergaven, gebruikers) VALUES (%s,%s,%s,%s,%s)
             ON CONFLICT (datum, pad) DO UPDATE SET titel = EXCLUDED.titel, weergaven = EXCLUDED.weergaven, gebruikers = EXCLUDED.gebruikers
             RETURNING 1""", (_datum(d[0]), d[1][:500], (d[2] or "")[:300], _i(m[0]), _i(m[1])))
    for (d, m) in ga_rapport(["date", "sessionDefaultChannelGroup", "sessionSource"], ["sessions", "activeUsers"], start, eind):
        q("""INSERT INTO ga_bronnen (datum, kanaal, bron, sessies, gebruikers) VALUES (%s,%s,%s,%s,%s)
             ON CONFLICT (datum, kanaal, bron) DO UPDATE SET sessies = EXCLUDED.sessies, gebruikers = EXCLUDED.gebruikers RETURNING 1""",
          (_datum(d[0]), d[1][:100], d[2][:200], _i(m[0]), _i(m[1])))
    for (d, m) in ga_rapport(["date", "sessionSource", "sessionMedium", "sessionCampaignName", "sessionManualAdContent"],
                             ["sessions", "activeUsers"], start, eind):
        if (d[3] or "(not set)") in ("(not set)", "(direct)", "(organic)", "(referral)"):
            continue
        q("""INSERT INTO ga_campagnes (datum, bron, medium, campagne, inhoud, sessies, gebruikers) VALUES (%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (datum, bron, medium, campagne, inhoud) DO UPDATE SET sessies = EXCLUDED.sessies, gebruikers = EXCLUDED.gebruikers
             RETURNING 1""", (_datum(d[0]), d[1][:200], d[2][:100], d[3][:200], ("" if d[4] == "(not set)" else d[4])[:200], _i(m[0]), _i(m[1])))


# ---------- Search Console ----------

def gsc_site():
    global GSC_SITE
    if GSC_SITE:
        return GSC_SITE
    sites = _api("https://www.googleapis.com/webmasters/v3/sites").get("siteEntry") or []
    kandidaten = [s["siteUrl"] for s in sites if "zerohero.one" in s.get("siteUrl", "") and s.get("permissionLevel") != "siteUnverifiedUser"]
    kandidaten.sort(key=lambda u: (not u.startswith("sc-domain:"), u))
    if not kandidaten:
        raise RuntimeError("Het serviceaccount heeft nog geen toegang tot zerohero.one in Search Console")
    GSC_SITE = kandidaten[0]
    return GSC_SITE


def gsc_query(dimensies, start, eind, limiet=25000):
    site = urllib.parse.quote(gsc_site(), safe="")
    body = {"startDate": start.isoformat(), "endDate": eind.isoformat(), "dimensions": dimensies, "rowLimit": limiet, "dataState": "all"}
    return _api(f"https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query", body).get("rows") or []


def gsc_ophalen(q, dagen):
    eind = dt.date.today()
    start = eind - dt.timedelta(days=dagen)
    for r in gsc_query(["date"], start, eind):
        q("""INSERT INTO gsc_dag (datum, klikken, vertoningen, positie, bijgewerkt) VALUES (%s,%s,%s,%s, now())
             ON CONFLICT (datum) DO UPDATE SET klikken = EXCLUDED.klikken, vertoningen = EXCLUDED.vertoningen,
               positie = EXCLUDED.positie, bijgewerkt = now() RETURNING 1""",
          (dt.date.fromisoformat(r["keys"][0]), _i(r.get("clicks")), _i(r.get("impressions")), round(float(r.get("position") or 0), 1)))
    for r in gsc_query(["date", "query"], start, eind):
        q("""INSERT INTO gsc_zoektermen (datum, term, klikken, vertoningen, positie) VALUES (%s,%s,%s,%s,%s)
             ON CONFLICT (datum, term) DO UPDATE SET klikken = EXCLUDED.klikken, vertoningen = EXCLUDED.vertoningen,
               positie = EXCLUDED.positie RETURNING 1""",
          (dt.date.fromisoformat(r["keys"][0]), r["keys"][1][:300], _i(r.get("clicks")), _i(r.get("impressions")),
           round(float(r.get("position") or 0), 1)))
    # welke pagina bij welke zoekterm verschijnt (laatste 90 dagen, als geheel), voor de SEO-kansen
    rijen = gsc_query(["query", "page"], eind - dt.timedelta(days=90), eind)
    if rijen:
        q("DELETE FROM gsc_term_paginas WHERE bijgewerkt < now() - interval '1 minute' RETURNING 1")
        for r in rijen:
            q("""INSERT INTO gsc_term_paginas (term, pagina, klikken, vertoningen, positie) VALUES (%s,%s,%s,%s,%s)
                 ON CONFLICT (term, pagina) DO UPDATE SET klikken = EXCLUDED.klikken, vertoningen = EXCLUDED.vertoningen,
                   positie = EXCLUDED.positie, bijgewerkt = now() RETURNING 1""",
              (r["keys"][0][:300], r["keys"][1][:500], _i(r.get("clicks")), _i(r.get("impressions")), round(float(r.get("position") or 0), 1)))


# ---------- SEO-kansen ----------

# Gemiddelde klikkans per positie in Google (ruwe vuistregel uit openbare CTR-onderzoeken; bedoeld om te rangschikken, niet als belofte)
_CTR = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.018}
MERK = ("zerohero", "zero hero", "zero-hero", "martijn buurman", "buurman")


def verwachte_ctr(positie):
    p = max(1, round(float(positie or 99)))
    if p <= 10:
        return _CTR[p]
    return 0.01 if p <= 20 else 0.004


def seo_kansen(q, dagen=90, limiet=25, min_vertoningen=None):
    """Zoektermen waar met weinig werk meer bezoekers te halen zijn, uit Search Console.

    Soorten:
    - 'titel': op pagina 1 maar opvallend weinig klikken; titel en beschrijving in Google aantrekkelijker maken.
    - 'bijna': positie 4 tot 10; met een betere tussenkop, een alinea extra en interne links naar de top 3.
    - 'pagina2': positie 11 tot 20; de pagina uitbreiden of een eigen artikel over precies deze vraag.
    - 'nieuw': positie boven 20 maar wel gezien; er is vraag, nog geen goed antwoord: een nieuw artikel.
    Rangschikking op het geschatte aantal extra klikken per maand als de term naar plek 3 gaat (of, bij 'titel', als de
    klikkans normaal wordt voor de huidige plek). Merknamen (ZEROHERO, Martijn Buurman) tellen niet mee.
    """
    if min_vertoningen is None:
        min_vertoningen = max(10, round(dagen / 6))
    rijen = q("""SELECT term, sum(klikken) AS klikken, sum(vertoningen) AS vertoningen,
                        round(sum(positie * vertoningen) / nullif(sum(vertoningen), 0), 1) AS positie,
                        count(*) AS dagen_gezien
                 FROM gsc_zoektermen WHERE datum > current_date - %s GROUP BY term
                 HAVING sum(vertoningen) >= %s""", (dagen, min_vertoningen))
    try:
        pag = q("""SELECT DISTINCT ON (term) term, pagina, positie FROM gsc_term_paginas
                   ORDER BY term, vertoningen DESC, klikken DESC""")
    except Exception:  # noqa: BLE001
        pag = []
    paginas = {r["term"]: r["pagina"] for r in pag}
    per_maand = 30 / max(1, dagen)
    uit = []
    for r in rijen:
        term = r["term"] or ""
        if any(m in term.lower() for m in MERK):
            continue
        vert, klik = int(r["vertoningen"] or 0), int(r["klikken"] or 0)
        pos = float(r["positie"] or 99)
        ctr = klik / vert if vert else 0
        normaal = verwachte_ctr(pos)
        if pos <= 10 and vert >= 2 * min_vertoningen and ctr < normaal * 0.5:
            soort, doel = "titel", normaal
        elif pos <= 3:
            continue
        elif pos <= 10:
            soort, doel = "bijna", verwachte_ctr(3)
        elif pos <= 20:
            soort, doel = "pagina2", verwachte_ctr(3)
        else:
            soort, doel = "nieuw", verwachte_ctr(5)
        extra = vert * max(0.0, doel - ctr) * per_maand
        if extra < 0.3:
            continue
        uit.append({"term": term, "klikken": klik, "vertoningen": vert, "positie": round(pos, 1), "ctr": round(ctr * 100, 1),
                    "verwacht": round(normaal * 100, 1), "soort": soort, "extra": round(extra, 1),
                    "pagina": paginas.get(term), "dagen_gezien": r["dagen_gezien"]})
    uit.sort(key=lambda x: -x["extra"])
    return uit[:limiet]


SOORT_KANS = {
    "titel": ("Titel verbeteren", "Je staat op pagina 1, maar weinig mensen klikken. Herschrijf de SEO-titel en metabeschrijving van de pagina zodat ze precies op deze zoekvraag antwoorden."),
    "bijna": ("Bijna bovenaan", "Pagina 1, nog niet in de top 3. Gebruik de zoekterm in een tussenkop, beantwoord de vraag in de eerste alinea en link vanuit andere artikelen naar deze pagina."),
    "pagina2": ("Tweede pagina", "Google ziet je als relevant, maar net niet goed genoeg. Breid de pagina uit met een eigen stuk over deze vraag, of schrijf er een apart artikel over."),
    "nieuw": ("Nieuw artikel", "Er wordt op gezocht en je verschijnt al, maar ver weg. Er is nog geen pagina die echt antwoord geeft: schrijf er een artikel over."),
}


# ---------- samen ----------

def _fout(e, dienst):
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = ""
        if e.code == 403:
            return f"{dienst}: geen toegang (403). Geef {serviceaccount()} leesrechten. {detail[:160]}"
        return f"{dienst}: fout {e.code}. {detail[:200]}"
    return f"{dienst}: {str(e)[:220]}"


def ververs(q):
    """Haalt GA4 en Search Console op. Eerste keer 16 maanden terug, daarna de laatste 10 dagen."""
    if not actief():
        return False, "Geen Google-sleutel ingesteld"
    fouten = []
    ga_eerste = not q("SELECT 1 AS x FROM ga_dag LIMIT 1")
    try:
        ga_ophalen(q, 480 if ga_eerste else 10)
    except Exception as e:  # noqa: BLE001
        fouten.append(_fout(e, "Google Analytics"))
    gsc_eerste = not q("SELECT 1 AS x FROM gsc_dag LIMIT 1")
    try:
        gsc_ophalen(q, 480 if gsc_eerste else 10)
    except Exception as e:  # noqa: BLE001
        fouten.append(_fout(e, "Search Console"))
    fout = " | ".join(fouten) or None
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('google', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""",
      (fout is None, fout))
    return fout is None, fout


def overzicht(q, dagen):
    """Cijfers voor het tabblad Website: totalen, vergelijking, pagina's, bronnen en zoektermen."""
    def som(tabel, kolommen, van, tot):
        return q(f"SELECT {', '.join(f'coalesce(sum({k}), 0) AS {k}' for k in kolommen)} FROM {tabel} "
                 "WHERE datum > current_date - %s AND datum <= current_date - %s", (van, tot))[0]
    ga_nu = som("ga_dag", ["gebruikers", "sessies", "betrokken", "weergaven"], dagen, 0)
    ga_toen = som("ga_dag", ["gebruikers", "sessies", "betrokken", "weergaven"], dagen * 2, dagen)
    gsc_nu = som("gsc_dag", ["klikken", "vertoningen"], dagen, 0)
    gsc_toen = som("gsc_dag", ["klikken", "vertoningen"], dagen * 2, dagen)
    pos = q("""SELECT round(sum(positie * vertoningen) / nullif(sum(vertoningen), 0), 1) AS p FROM gsc_dag
               WHERE datum > current_date - %s""", (dagen,))[0]["p"]
    paginas = q("""SELECT pad, max(titel) AS titel, sum(weergaven) AS weergaven, sum(gebruikers) AS gebruikers FROM ga_paginas
                   WHERE datum > current_date - %s GROUP BY pad ORDER BY weergaven DESC LIMIT 15""", (dagen,))
    bronnen = q("""SELECT kanaal, bron, sum(sessies) AS sessies FROM ga_bronnen
                   WHERE datum > current_date - %s GROUP BY kanaal, bron ORDER BY sessies DESC LIMIT 12""", (dagen,))
    kanalen = q("""SELECT kanaal, sum(sessies) AS sessies FROM ga_bronnen WHERE datum > current_date - %s
                   GROUP BY kanaal ORDER BY sessies DESC""", (dagen,))
    linkedin = q("""SELECT coalesce(sum(sessies), 0) AS n FROM ga_bronnen WHERE datum > current_date - %s
                    AND (bron ILIKE '%%linkedin%%' OR bron ILIKE 'lnkd.in%%')""", (dagen,))[0]["n"]
    termen = q("""SELECT term, sum(klikken) AS klikken, sum(vertoningen) AS vertoningen,
                         round(sum(positie * vertoningen) / nullif(sum(vertoningen), 0), 1) AS positie
                  FROM gsc_zoektermen WHERE datum > current_date - %s GROUP BY term
                  ORDER BY klikken DESC, vertoningen DESC LIMIT 15""", (dagen,))
    reeks = q("""SELECT d::date AS datum, coalesce(g.gebruikers, 0) AS gebruikers, coalesce(s.klikken, 0) AS klikken
                 FROM generate_series(current_date - %s + 1, current_date, interval '1 day') d
                 LEFT JOIN ga_dag g ON g.datum = d::date LEFT JOIN gsc_dag s ON s.datum = d::date ORDER BY d""", (min(dagen, 90),))
    laatste_gsc = q("SELECT max(datum) AS d FROM gsc_dag")[0]["d"]
    return {"ga_nu": ga_nu, "ga_toen": ga_toen, "gsc_nu": gsc_nu, "gsc_toen": gsc_toen, "positie": pos,
            "paginas": paginas, "bronnen": bronnen, "kanalen": kanalen, "linkedin": linkedin, "termen": termen,
            "reeks": reeks, "laatste_gsc": laatste_gsc, "heeft_ga": bool(q("SELECT 1 AS x FROM ga_dag LIMIT 1")),
            "heeft_gsc": bool(q("SELECT 1 AS x FROM gsc_dag LIMIT 1"))}


_gestart = False


def start_achtergrond(q):
    global _gestart
    if _gestart or not actief() or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(35)
        while True:
            try:
                ververs(q)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=lus, name="google", daemon=True).start()
