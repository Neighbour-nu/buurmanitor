"""Koppeling met YouTube (Keepersjournaal, @ZEROHERO-ONE) via de YouTube Data API v3.

Alleen openbare cijfers, dus een API-sleutel volstaat (YOUTUBE_API_KEY in ~/cockpit/.env, instellen met
deploy/youtube.sh). Elke drie uur: abonnees, totale weergaven en aantal video's van het kanaal, plus
weergaven per video. Door elke dag een stand te bewaren ziet het dashboard de groei per periode.
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

SLEUTEL = os.environ.get("ZEROHERO_YOUTUBE_API_KEY", "").strip()
HANDLE = os.environ.get("ZEROHERO_YOUTUBE_HANDLE", "@ZEROHERO-ONE").strip()
API = "https://www.googleapis.com/youtube/v3"
INTERVAL = 3 * 3600

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS yt_videos (
        id text PRIMARY KEY, titel text, gepubliceerd timestamptz, weergaven int, likes int, reacties int,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS yt_video_dag (
        id text NOT NULL, datum date NOT NULL, weergaven int, PRIMARY KEY (id, datum))""",
]


def actief():
    return bool(SLEUTEL)


def _get(pad, params):
    url = f"{API}/{pad}?" + urllib.parse.urlencode({**params, "key": SLEUTEL})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=20) as r:
        return json.loads(r.read())


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def kanaal():
    items = _get("channels", {"part": "snippet,statistics,contentDetails", "forHandle": HANDLE}).get("items") or []
    if not items:
        raise RuntimeError(f"Kanaal {HANDLE} niet gevonden")
    return items[0]


def ophalen():
    k = kanaal()
    st = k.get("statistics") or {}
    uploads = ((k.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    ids, token = [], None
    while uploads and len(ids) < 500:
        p = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if token:
            p["pageToken"] = token
        r = _get("playlistItems", p)
        ids += [i["contentDetails"]["videoId"] for i in r.get("items") or []]
        token = r.get("nextPageToken")
        if not token:
            break
    videos = []
    for n in range(0, len(ids), 50):
        for v in _get("videos", {"part": "snippet,statistics", "id": ",".join(ids[n:n + 50])}).get("items") or []:
            s = v.get("statistics") or {}
            videos.append({"id": v["id"], "titel": (v.get("snippet") or {}).get("title", ""),
                           "gepubliceerd": (v.get("snippet") or {}).get("publishedAt"),
                           "weergaven": _i(s.get("viewCount")), "likes": _i(s.get("likeCount")), "reacties": _i(s.get("commentCount"))})
    return {"titel": (k.get("snippet") or {}).get("title", ""), "abonnees": _i(st.get("subscriberCount")),
            "weergaven": _i(st.get("viewCount")), "videos": _i(st.get("videoCount")), "lijst": videos,
            "abonnees_verborgen": bool(st.get("hiddenSubscriberCount"))}


def ververs(q):
    if not actief():
        return False, "Geen YOUTUBE_API_KEY ingesteld"
    try:
        g = ophalen()
        vandaag = dt.date.today()
        for sleutel, waarde in (("abonnees", g["abonnees"]), ("weergaven_totaal", g["weergaven"]), ("videos", g["videos"])):
            if waarde is not None:
                q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES ('youtube', %s, %s, %s, 'api')
                     ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'api', bijgewerkt = now()
                     RETURNING 1""", (sleutel, vandaag, waarde))
        for v in g["lijst"]:
            q("""INSERT INTO yt_videos (id, titel, gepubliceerd, weergaven, likes, reacties, bijgewerkt) VALUES (%s,%s,%s,%s,%s,%s, now())
                 ON CONFLICT (id) DO UPDATE SET titel = EXCLUDED.titel, weergaven = EXCLUDED.weergaven, likes = EXCLUDED.likes,
                   reacties = EXCLUDED.reacties, bijgewerkt = now() RETURNING 1""",
              (v["id"], v["titel"][:300], v["gepubliceerd"], v["weergaven"], v["likes"], v["reacties"]))
            q("""INSERT INTO yt_video_dag (id, datum, weergaven) VALUES (%s, %s, %s)
                 ON CONFLICT (id, datum) DO UPDATE SET weergaven = EXCLUDED.weergaven RETURNING 1""", (v["id"], vandaag, v["weergaven"]))
        fout = None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = ""
        fout = f"YouTube gaf fout {e.code}. {detail[:200]}"
    except Exception as e:  # noqa: BLE001
        fout = "YouTube niet bereikbaar: " + str(e)[:200]
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('youtube', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""",
      (fout is None, fout))
    return fout is None, fout


def overzicht(q, dagen):
    """Video's met weergaven in de periode (verschil met de eerste stand op of na het begin van de periode)."""
    videos = q("""SELECT v.*, v.weergaven - coalesce(
                     (SELECT d.weergaven FROM yt_video_dag d WHERE d.id = v.id AND d.datum <= current_date - %s ORDER BY d.datum DESC LIMIT 1),
                     (SELECT d.weergaven FROM yt_video_dag d WHERE d.id = v.id ORDER BY d.datum LIMIT 1)) AS in_periode,
                     (SELECT min(datum) FROM yt_video_dag d WHERE d.id = v.id) AS sinds
                  FROM yt_videos v ORDER BY v.gepubliceerd DESC NULLS LAST LIMIT 12""", (dagen,))
    return {"videos": videos, "status": (q("SELECT * FROM koppelingen WHERE naam = 'youtube'") or [None])[0], "actief": actief()}


_gestart = False


def start_achtergrond(q):
    global _gestart
    if _gestart or not actief() or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(50)
        while True:
            try:
                ververs(q)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=lus, name="youtube", daemon=True).start()


SORTERINGEN = {"nieuw": "Nieuwste eerst", "weergaven": "Meeste weergaven", "periode": "Meeste erbij", "perdag": "Meeste per dag online"}


def detail(q, dagen, sorteer="nieuw"):
    """Alles voor de YouTube-pagina: kanaalverloop, groei per dag, alle video's met kengetallen en inzichten."""
    o = overzicht(q, dagen)
    videos = q("""SELECT v.*, v.weergaven - coalesce(
                     (SELECT d.weergaven FROM yt_video_dag d WHERE d.id = v.id AND d.datum <= current_date - %s ORDER BY d.datum DESC LIMIT 1),
                     (SELECT d.weergaven FROM yt_video_dag d WHERE d.id = v.id ORDER BY d.datum LIMIT 1)) AS in_periode,
                     greatest(1, current_date - v.gepubliceerd::date) AS dagen_online
                  FROM yt_videos v""", (dagen,))
    for v in videos:
        w = v["weergaven"] or 0
        v["per_dag"] = round(w / v["dagen_online"], 1) if v["dagen_online"] else None
        v["interactie"] = round(((v["likes"] or 0) + (v["reacties"] or 0)) / w * 100, 1) if w else None
    sleutel = {"weergaven": lambda v: -(v["weergaven"] or 0), "periode": lambda v: -(v["in_periode"] or 0),
               "perdag": lambda v: -(v["per_dag"] or 0)}.get(sorteer, lambda v: -(v["gepubliceerd"].timestamp() if v["gepubliceerd"] else 0))
    videos.sort(key=sleutel)
    o["alle"] = videos
    o["sorteer"] = sorteer if sorteer in SORTERINGEN else "nieuw"
    reeks = q("""SELECT datum, sleutel, waarde FROM metingen WHERE kanaal = 'youtube' AND sleutel IN ('abonnees', 'weergaven_totaal')
                 AND datum > current_date - %s - 1 ORDER BY datum""", (dagen,))
    per = {}
    for r in reeks:
        per.setdefault(r["datum"], {})[r["sleutel"]] = int(r["waarde"])
    dagen_l, vorige = [], None
    for d in sorted(per):
        w = per[d].get("weergaven_totaal")
        dagen_l.append({"datum": d, "abonnees": per[d].get("abonnees"),
                        "erbij": (w - vorige) if (w is not None and vorige is not None) else None})
        if w is not None:
            vorige = w
    o["dagreeks"] = [r for r in dagen_l if r["erbij"] is not None]
    o["abonneereeks"] = [r for r in dagen_l if r["abonnees"] is not None]
    eerste = q("SELECT min(datum) AS d FROM metingen WHERE kanaal = 'youtube'")[0]["d"]
    o["meet_sinds"] = eerste
    totaal = sum(v["weergaven"] or 0 for v in videos)
    o["totaal_videos"] = totaal
    o["erbij_periode"] = sum(v["in_periode"] or 0 for v in videos)
    o["inzichten"] = _inzichten(videos, totaal, dagen, eerste)
    return o


def _n(v):
    return f"{int(v or 0):,}".replace(",", ".")


def _kort(t, n=90):
    t = (t or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(" ,:;-") + "…"


def _inzichten(videos, totaal, dagen, eerste):
    uit = []
    if not videos:
        return uit
    gem = round(totaal / len(videos))
    uit.append(f"{len(videos)} video's met samen {_n(totaal)} weergaven, gemiddeld {_n(gem)} per video.")
    top = sorted(videos, key=lambda v: -(v["weergaven"] or 0))
    if totaal and len(videos) >= 5:
        aandeel = round(sum(v["weergaven"] or 0 for v in top[:3]) / totaal * 100)
        uit.append(f"De drie best bekeken video's zijn samen goed voor {aandeel}% van alle weergaven.")
    uit.append(f"Meest bekeken: “{_kort(top[0]['titel'])}” ({_n(top[0]['weergaven'])} weergaven).")
    snel = max(videos, key=lambda v: v["per_dag"] or 0)
    if snel["per_dag"]:
        uit.append(f"Groeit het snelst per dag online: “{_kort(snel['titel'])}” (gemiddeld "
                   f"{str(snel['per_dag']).replace('.', ',')} per dag).")
    if eerste and (dt.date.today() - eerste).days >= 2:
        groeiers = [v for v in videos if v["in_periode"]]
        if groeiers:
            g = max(groeiers, key=lambda v: v["in_periode"])
            uit.append(f"Meeste nieuwe weergaven sinds {max(eerste, dt.date.today() - dt.timedelta(days=dagen)).strftime('%d-%m')}: "
                       f"“{_kort(g['titel'])}” (+{_n(g['in_periode'])}). Oude video's die nog groeien, zijn de moeite van opnieuw delen waard.")
    nieuwste = max((v for v in videos if v["gepubliceerd"]), key=lambda v: v["gepubliceerd"], default=None)
    if nieuwste:
        d = (dt.date.today() - nieuwste["gepubliceerd"].date()).days
        uit.append(f"De nieuwste video staat {d} dagen online.")
    beste_i = [v for v in videos if v["interactie"] is not None and (v["weergaven"] or 0) >= 50]
    if beste_i:
        b = max(beste_i, key=lambda v: v["interactie"])
        uit.append(f"Meeste reacties en likes naar verhouding: “{_kort(b['titel'])}” "
                   f"({str(b['interactie']).replace('.', ',')}% van de kijkers).")
    return uit
