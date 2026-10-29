"""Koppeling met Threads (graph.threads.net), los van de Meta-koppeling voor Facebook en Instagram.

Haalt elke drie uur op: volgers, profielweergaven per dag, likes, reacties, reposts en citaten per dag, en de laatste
posts met hun cijfers (in dezelfde tabel als Instagram en Facebook, kanaal 'threads').

Token: THREADS_TOKEN in ~/cockpit/.env (via deploy/threads.sh), een lang geldig gebruikerstoken met threads_basic en
threads_manage_insights. Zo'n token is 60 dagen geldig; de cockpit vernieuwt het zelf elke week en bewaart het
vernieuwde token in de database (tabel instellingen), zodat het niet verloopt.
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

API = "https://graph.threads.net/v1.0"
ENV_TOKEN = os.environ.get("ZEROHERO_THREADS_TOKEN", "").strip()
INTERVAL = 3 * 3600
DAG_METRICS = ("likes", "replies", "reposts", "quotes")


def _q_token(q):
    try:
        r = q("SELECT waarde, bijgewerkt FROM instellingen WHERE sleutel = 'threads_token'")
    except Exception:  # noqa: BLE001
        r = []
    return (r[0]["waarde"], r[0]["bijgewerkt"]) if r and r[0]["waarde"] else (ENV_TOKEN, None)


def actief():
    return bool(ENV_TOKEN)


def _get(pad, params, token):
    params = dict(params or {}, access_token=token)
    url = (pad if pad.startswith("http") else f"{API}/{pad}") + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Doellijn/1.0 (+https://zerohero.one)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fout(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return (json.loads(e.read() or b"{}").get("error") or {}).get("message", f"fout {e.code}")[:300]
        except Exception:  # noqa: BLE001
            return f"fout {e.code}"
    return str(e)[:300]


def _vernieuw_token(q, token, sinds):
    """Elke zeven dagen het token verlengen (kan pas als het minstens 24 uur oud is)."""
    if sinds and (dt.datetime.now(dt.timezone.utc) - sinds).days < 7:
        return token
    try:
        r = _get("https://graph.threads.net/refresh_access_token", {"grant_type": "th_refresh_token"}, token)
    except Exception:  # noqa: BLE001
        return token  # bijvoorbeeld nog geen 24 uur oud; volgende ronde opnieuw
    nieuw = r.get("access_token")
    if nieuw:
        q("""INSERT INTO instellingen (sleutel, waarde, bijgewerkt) VALUES ('threads_token', %s, now())
             ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now() RETURNING 1""", (nieuw,))
        return nieuw
    return token


def _meting(q, sleutel, datum, waarde):
    if waarde is None:
        return
    q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES ('threads', %s, %s, %s, 'api')
         ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'api', bijgewerkt = now() RETURNING 1""",
      (sleutel, datum, waarde))


def _totaal(r, naam):
    for m in r.get("data") or []:
        if m.get("name") == naam:
            tv = m.get("total_value") or {}
            if "value" in tv:
                return tv["value"]
            vals = m.get("values") or []
            return sum(int(v.get("value") or 0) for v in vals) if vals else None
    return None


def ververs(q):
    """Ophalen en opslaan; status in de tabel koppelingen. Geeft (ok, melding) terug."""
    if not actief():
        return False, "Geen THREADS_TOKEN ingesteld (deploy/threads.sh)."
    meldingen, fout = [], None
    try:
        token, sinds = _q_token(q)
        token = _vernieuw_token(q, token, sinds)
        ik = _get("me", {"fields": "id,username"}, token)
        uid = ik["id"]
        vandaag = dt.date.today()
        # volgers (stand)
        try:
            r = _get(f"{uid}/threads_insights", {"metric": "followers_count"}, token)
            _meting(q, "volgers", vandaag, _totaal(r, "followers_count"))
        except Exception as e:  # noqa: BLE001
            meldingen.append(f"Volgers: {_fout(e)}")
        # profielweergaven per dag (tijdreeks), laatste 30 dagen
        try:
            since = int(time.mktime((vandaag - dt.timedelta(days=30)).timetuple()))
            until = int(time.mktime(vandaag.timetuple()))
            r = _get(f"{uid}/threads_insights", {"metric": "views", "since": since, "until": until}, token)
            for m in r.get("data") or []:
                for v in m.get("values") or []:
                    if v.get("end_time"):
                        d = dt.date.fromisoformat(v["end_time"][:10]) - dt.timedelta(days=1)
                        _meting(q, "profielweergaven", d, int(v.get("value") or 0))
        except Exception as e:  # noqa: BLE001
            meldingen.append(f"Profielweergaven: {_fout(e)}")
        # interacties per dag: de laatste drie volle dagen apart opvragen
        for terug in (1, 2, 3):
            dag = vandaag - dt.timedelta(days=terug)
            van = int(time.mktime(dag.timetuple()))
            tot = int(time.mktime((dag + dt.timedelta(days=1)).timetuple()))
            try:
                r = _get(f"{uid}/threads_insights", {"metric": ",".join(DAG_METRICS), "since": van, "until": tot}, token)
                waarden = {m: _totaal(r, m) for m in DAG_METRICS}
                if any(v is not None for v in waarden.values()):
                    _meting(q, "interacties", dag, sum(int(v or 0) for v in waarden.values()))
                    _meting(q, "likes", dag, waarden["likes"])
            except Exception as e:  # noqa: BLE001
                meldingen.append(f"Interacties {dag.strftime('%d-%m')}: {_fout(e)}")
                break
        # laatste posts met cijfers
        from .meta import _post
        try:
            posts = _get("me/threads", {"fields": "id,text,timestamp,permalink,media_type,media_url,thumbnail_url,is_quote_post", "limit": 25}, token)
            for x in posts.get("data") or []:
                if x.get("media_type") == "REPOST_FACADE":
                    continue
                c = {}
                try:
                    r = _get(f"{x['id']}/insights", {"metric": "views,likes,replies,reposts,quotes,shares"}, token)
                    for m in r.get("data") or []:
                        vals = m.get("values") or [{}]
                        c[m.get("name")] = (m.get("total_value") or {}).get("value", vals[0].get("value"))
                except Exception:  # noqa: BLE001
                    pass
                likes, reacties = c.get("likes"), c.get("replies")
                gedeeld = sum(int(c.get(k) or 0) for k in ("reposts", "quotes", "shares")) if any(c.get(k) is not None for k in ("reposts", "quotes", "shares")) else None
                _post(q, "th_" + str(x["id"]), "threads", x.get("text"), x.get("timestamp"), (x.get("media_type") or "").lower() or None,
                      x.get("permalink"), x.get("thumbnail_url") or x.get("media_url"), likes, reacties, gedeeld, None, None, c.get("views"),
                      sum(int(v or 0) for v in (likes, reacties, gedeeld)) if c else None)
        except Exception as e:  # noqa: BLE001
            meldingen.append(f"Posts: {_fout(e)}")
    except Exception as e:  # noqa: BLE001
        fout = "Threads: " + _fout(e)
        if "expired" in fout.lower() or "session" in fout.lower():
            fout += " Maak een nieuw token en draai deploy/threads.sh opnieuw."
    tekst = fout or ("; ".join(meldingen)[:900] if meldingen else None)
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('threads', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""", (fout is None, tekst))
    return fout is None, tekst


def status(q):
    return (q("SELECT * FROM koppelingen WHERE naam = 'threads'") or [None])[0]


_gestart = False


def start_achtergrond(q):
    global _gestart
    if _gestart or not actief() or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(100)
        while True:
            try:
                ververs(q)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=lus, name="threads", daemon=True).start()
