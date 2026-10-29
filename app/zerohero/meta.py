"""Koppeling met Meta: de Facebook-pagina en het Instagram-account van ZEROHERO, via de Graph API.

Sleutel: META_TOKEN in ~/cockpit/.env (een systeemgebruikerstoken uit Meta Business, instellen met deploy/meta.sh).
Elke drie uur: volgers van pagina en account, dagcijfers (weergaven, bereik, interacties, nieuwe volgers) en de
laatste berichten met hun cijfers. Meta wijzigt regelmatig welke statistieken beschikbaar zijn; de koppeling probeert
elke statistiek los en slaat over wat Meta niet (meer) geeft, zodat één verdwenen statistiek niet alles stillegt.
"""
from . import omgeving
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("ZEROHERO_META_TOKEN", "").strip()
PAGINA_ID = os.environ.get("ZEROHERO_META_PAGE_ID", "").strip()
VERSIE = os.environ.get("ZEROHERO_META_API_VERSIE", "v23.0")
API = f"https://graph.facebook.com/{VERSIE}"
INTERVAL = 3 * 3600

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS meta_posts (
        id text PRIMARY KEY, kanaal text NOT NULL, tekst text, gepubliceerd timestamptz, soort text, link text, beeld text,
        likes int, reacties int, gedeeld int, bewaard int, bereik int, weergaven int, interacties int,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS meta_post_dag (id text NOT NULL, datum date NOT NULL, weergaven int, interacties int,
        PRIMARY KEY (id, datum))""",
]

# Dagstatistieken per kanaal: sleutel in metingen -> lijst met Meta-namen om te proberen (de eerste die werkt telt).
FB_DAG = {
    "weergaven": ["page_media_view", "page_impressions"],
    "bereik": ["page_total_media_view_unique", "page_impressions_unique"],
    "interacties": ["page_post_engagements"],
    "nieuwe_volgers": ["page_daily_follows_unique", "page_fan_adds_unique"],
}
IG_TOTAAL = ["views", "reach", "total_interactions", "profile_views", "accounts_engaged", "website_clicks"]
IG_SLEUTEL = {"views": "weergaven", "reach": "bereik", "total_interactions": "interacties", "profile_views": "profielbezoeken",
              "accounts_engaged": "betrokken_accounts", "website_clicks": "websiteklikken"}


def actief():
    return bool(TOKEN)


def _get(pad, params=None, token=None):
    p = dict(params or {})
    p["access_token"] = token or TOKEN
    url = f"{API}/{pad.lstrip('/')}?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=30) as r:
        return json.loads(r.read())


def _fout(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return (json.loads(e.read() or b"{}").get("error") or {}).get("message", "")[:200] or f"fout {e.code}"
        except Exception:  # noqa: BLE001
            return f"fout {e.code}"
    return str(e)[:200]


def accounts():
    """De pagina('s) waar de token bij kan, met paginatoken en gekoppeld Instagram-account."""
    r = _get("me/accounts", {"fields": "id,name,access_token,followers_count,fan_count,instagram_business_account{id,username,followers_count,media_count}"})
    return r.get("data") or []


def kies_pagina():
    paginas = accounts()
    if not paginas:
        raise RuntimeError("De token heeft geen toegang tot een Facebook-pagina. Wijs de pagina toe aan de systeemgebruiker.")
    if PAGINA_ID:
        for p in paginas:
            if p["id"] == PAGINA_ID:
                return p
    return next((p for p in paginas if "zerohero" in (p.get("name") or "").lower()), paginas[0])


def _meting(q, kanaal, sleutel, datum, waarde, soort="api"):
    if waarde is None:
        return
    q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES (%s, %s, %s, %s, %s)
         ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = EXCLUDED.bron, bijgewerkt = now()
         RETURNING 1""", (kanaal, sleutel, datum, waarde, soort))


def _waarde(v):
    if isinstance(v, dict):  # sommige statistieken geven een verdeling; tel die op
        return sum(x for x in v.values() if isinstance(x, (int, float)))
    return v


def _facebook(q, p, ptoken, meldingen):
    vandaag = dt.date.today()
    info = _get(p["id"], {"fields": "followers_count,fan_count,name"}, ptoken)
    _meting(q, "facebook", "volgers", vandaag, info.get("followers_count") or info.get("fan_count"))
    since = int(time.mktime((vandaag - dt.timedelta(days=28)).timetuple()))
    until = int(time.mktime(vandaag.timetuple()))
    for sleutel, namen in FB_DAG.items():
        for naam in namen:
            try:
                r = _get(f"{p['id']}/insights", {"metric": naam, "period": "day", "since": since, "until": until}, ptoken)
            except Exception as e:  # noqa: BLE001
                meldingen.append(f"Facebook {naam}: {_fout(e)}")
                continue
            for reeks in r.get("data") or []:
                for v in reeks.get("values") or []:
                    d = dt.date.fromisoformat(v["end_time"][:10]) - dt.timedelta(days=1)
                    _meting(q, "facebook", sleutel, d, _waarde(v.get("value")))
            break
    # Laatste berichten
    # Meta laat berichten soms alleen via één van deze routes zien, afhankelijk van de toestemmingen van het token.
    # Lukt het met reacties en commentaren niet, dan zonder (de weergaven komen dan uit de insights per bericht).
    posts, fouten = None, []
    volledig = "id,message,created_time,permalink_url,full_picture,shares,reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)"
    kaal = "id,message,created_time,permalink_url,full_picture,shares"
    for route in ("published_posts", "posts", "feed"):
        for velden in (volledig, kaal):
            try:
                posts = _get(f"{p['id']}/{route}", {"limit": 25, "fields": velden}, ptoken)
                break
            except Exception as e:  # noqa: BLE001
                fouten.append(_fout(e))
        if posts is not None:
            break
    if posts is None:
        if any("pages_read_user_content" in f for f in fouten):
            meldingen.append("Facebook-berichten: Meta vraagt hiervoor de toestemming pages_read_user_content. Voeg die toe aan de app "
                             "(Gebruiksscenario's, Alles op je pagina beheren, Aanpassen), maak een nieuw token met die toestemming erbij "
                             "en draai deploy/meta.sh opnieuw. De overige Facebook-cijfers werken al.")
        else:
            meldingen.append(f"Facebook-berichten: {fouten[-1] if fouten else 'onbekende fout'}")
        return
    for x in posts.get("data") or []:
        weergaven = None
        for naam in ("post_media_view", "post_impressions"):
            try:
                ins = _get(f"{x['id']}/insights", {"metric": naam}, ptoken)
                weergaven = _waarde(((ins.get("data") or [{}])[0].get("values") or [{}])[0].get("value"))
                break
            except Exception:  # noqa: BLE001
                continue
        likes = ((x.get("reactions") or {}).get("summary") or {}).get("total_count")
        reacties = ((x.get("comments") or {}).get("summary") or {}).get("total_count")
        gedeeld = (x.get("shares") or {}).get("count", 0)
        _post(q, x["id"], "facebook", x.get("message"), x.get("created_time"), "bericht", x.get("permalink_url"), x.get("full_picture"),
              likes, reacties, gedeeld, None, None, weergaven, (likes or 0) + (reacties or 0) + (gedeeld or 0))


def _instagram(q, ig, ptoken, meldingen):
    vandaag = dt.date.today()
    info = _get(ig["id"], {"fields": "followers_count,media_count,username"}, ptoken)
    _meting(q, "instagram", "volgers", vandaag, info.get("followers_count"))
    _meting(q, "instagram", "posts", vandaag, info.get("media_count"))
    # Nieuwe volgers per dag (Meta geeft hooguit 30 dagen terug)
    try:
        r = _get(f"{ig['id']}/insights", {"metric": "follower_count", "period": "day",
                                          "since": int(time.mktime((vandaag - dt.timedelta(days=28)).timetuple())),
                                          "until": int(time.mktime(vandaag.timetuple()))}, ptoken)
        for reeks in r.get("data") or []:
            for v in reeks.get("values") or []:
                _meting(q, "instagram", "nieuwe_volgers", dt.date.fromisoformat(v["end_time"][:10]) - dt.timedelta(days=1), _waarde(v.get("value")))
    except Exception as e:  # noqa: BLE001
        meldingen.append(f"Instagram follower_count: {_fout(e)}")
    # Dagtotalen: Meta geeft deze alleen als totaal over een periode, dus per afgeronde dag apart opvragen (de laatste 3 dagen).
    werkt = list(IG_TOTAAL)
    for terug in (1, 2, 3):
        d = vandaag - dt.timedelta(days=terug)
        start = int(time.mktime(d.timetuple()))
        for metric in list(werkt):
            try:
                r = _get(f"{ig['id']}/insights", {"metric": metric, "metric_type": "total_value", "period": "day",
                                                  "since": start, "until": start + 86400}, ptoken)
            except Exception as e:  # noqa: BLE001
                werkt.remove(metric)
                meldingen.append(f"Instagram {metric}: {_fout(e)}")
                continue
            for reeks in r.get("data") or []:
                _meting(q, "instagram", IG_SLEUTEL[metric], d, (reeks.get("total_value") or {}).get("value"))
    # Laatste posts met hun cijfers
    try:
        media = _get(f"{ig['id']}/media", {"limit": 25, "fields": "id,caption,timestamp,media_type,media_product_type,permalink,"
                                                                  "thumbnail_url,media_url,like_count,comments_count"}, ptoken)
    except Exception as e:  # noqa: BLE001
        meldingen.append(f"Instagram-posts: {_fout(e)}")
        return
    for m in media.get("data") or []:
        cijfers = {}
        for metric in ("views", "reach", "saved", "shares", "total_interactions"):
            try:
                ins = _get(f"{m['id']}/insights", {"metric": metric}, ptoken)
                cijfers[metric] = _waarde(((ins.get("data") or [{}])[0].get("values") or [{}])[0].get("value"))
            except Exception:  # noqa: BLE001
                continue
        soort = {"REELS": "reel", "STORY": "story"}.get(m.get("media_product_type"), {"CAROUSEL_ALBUM": "carrousel", "VIDEO": "video"}.get(m.get("media_type"), "foto"))
        _post(q, m["id"], "instagram", m.get("caption"), m.get("timestamp"), soort, m.get("permalink"),
              m.get("thumbnail_url") or (m.get("media_url") if m.get("media_type") != "VIDEO" else None),
              m.get("like_count"), m.get("comments_count"), cijfers.get("shares"), cijfers.get("saved"), cijfers.get("reach"),
              cijfers.get("views"), cijfers.get("total_interactions"))


def _post(q, pid, kanaal, tekst, tijd, soort, link, beeld, likes, reacties, gedeeld, bewaard, bereik, weergaven, interacties):
    q("""INSERT INTO meta_posts (id, kanaal, tekst, gepubliceerd, soort, link, beeld, likes, reacties, gedeeld, bewaard, bereik,
             weergaven, interacties, bijgewerkt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
         ON CONFLICT (id) DO UPDATE SET tekst = EXCLUDED.tekst, link = EXCLUDED.link, beeld = EXCLUDED.beeld, likes = EXCLUDED.likes,
           reacties = EXCLUDED.reacties, gedeeld = EXCLUDED.gedeeld, bewaard = coalesce(EXCLUDED.bewaard, meta_posts.bewaard),
           bereik = coalesce(EXCLUDED.bereik, meta_posts.bereik), weergaven = coalesce(EXCLUDED.weergaven, meta_posts.weergaven),
           interacties = EXCLUDED.interacties, bijgewerkt = now() RETURNING 1""",
      (pid, kanaal, (tekst or "")[:2000], tijd, soort, link, beeld, likes, reacties, gedeeld, bewaard, bereik, weergaven, interacties))
    q("""INSERT INTO meta_post_dag (id, datum, weergaven, interacties) VALUES (%s, current_date, %s, %s)
         ON CONFLICT (id, datum) DO UPDATE SET weergaven = EXCLUDED.weergaven, interacties = EXCLUDED.interacties RETURNING 1""",
      (pid, weergaven, interacties))


def ververs(q):
    if not actief():
        return False, "Geen META_TOKEN ingesteld"
    meldingen, fout = [], None
    try:
        p = kies_pagina()
        ptoken = p.get("access_token") or TOKEN
        try:
            _facebook(q, p, ptoken, meldingen)
        except Exception as e:  # noqa: BLE001
            meldingen.append(f"Facebook: {_fout(e)}")
        ig = p.get("instagram_business_account")
        if ig:
            try:
                _instagram(q, ig, ptoken, meldingen)
            except Exception as e:  # noqa: BLE001
                meldingen.append(f"Instagram: {_fout(e)}")
        else:
            meldingen.append("Er is geen Instagram-account aan de Facebook-pagina gekoppeld.")
    except Exception as e:  # noqa: BLE001
        fout = "Meta: " + _fout(e)
    tekst = fout or ("; ".join(meldingen)[:900] if meldingen else None)
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('meta', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""",
      (fout is None, tekst))
    return fout is None, tekst


def posts(q, kanaal, dagen, sorteer="nieuw"):
    volgorde = {"weergaven": "weergaven DESC NULLS LAST", "interacties": "interacties DESC NULLS LAST",
                "bereik": "bereik DESC NULLS LAST"}.get(sorteer, "gepubliceerd DESC")
    rijen = q(f"""SELECT p.*, p.weergaven - coalesce((SELECT d.weergaven FROM meta_post_dag d WHERE d.id = p.id
                                AND d.datum <= current_date - %s ORDER BY d.datum DESC LIMIT 1), 0) AS erbij
                  FROM meta_posts p WHERE kanaal = %s ORDER BY {volgorde} LIMIT 50""", (dagen, kanaal))
    for r in rijen:
        r["ratio"] = round((r["interacties"] or 0) / r["weergaven"] * 100, 1) if r.get("weergaven") else None
    return rijen


def status(q):
    return (q("SELECT * FROM koppelingen WHERE naam = 'meta'") or [None])[0]


_gestart = False


def start_achtergrond(q):
    global _gestart
    if _gestart or not actief() or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(80)
        while True:
            try:
                ververs(q)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=lus, name="meta", daemon=True).start()
