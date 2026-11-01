"""Jesse, socials. Leest de kanalen uit de Doellijn (werkruimte ZEROHERO): de cijfers per kanaal (tabel metingen),
de laatste posts met hun resultaten (Instagram, Facebook en Threads via Meta; LinkedIn uit de exports; YouTube) en
de concepten die klaarstaan om te posten. Vermeldingen van PRO Helmond en Martijn komen uit het nieuws van Promonitor.

Jesse leest alleen; hij post, reageert, liket en stuurt nooit iets. Zijn voorstellen zijn tekst om te kopiëren.
"""
import datetime as dt
import re
from decimal import Decimal

from ..kern import config

KANALEN = ("linkedin", "linkedin_pagina", "instagram", "facebook", "threads", "youtube")
KORT = {"linkedin": "LinkedIn Martijn", "linkedin_pagina": "LinkedIn ZEROHERO", "instagram": "Instagram", "facebook": "Facebook",
        "threads": "Threads", "youtube": "YouTube"}
# Het cijfer dat per kanaal het meest zegt over de afgelopen periode (naast volgers of abonnees).
HOOFDCIJFER = {"linkedin": "impressies", "linkedin_pagina": "weergaven", "instagram": "weergaven", "facebook": "weergaven",
               "threads": "profielweergaven", "youtube": "weergaven_totaal"}
VERMELDING = re.compile(r"PRO Helmond|Martijn Buurman|GroenLinks-PvdA Helmond", re.I)


def _getal(v):
    if isinstance(v, Decimal):
        return int(v) if v == int(v) else float(v)
    return v


def kanalen(dagen=7):
    """Per kanaal de cijfers over de periode, met de verandering ten opzichte van de periode ervoor (uit het dashboard
    van de Doellijn). None als de Doellijn hier niet draait."""
    if not config.ZEROHERO_ACTIEF:
        return None
    from ..zerohero import dashboard  # pylint: disable=import-outside-toplevel
    uit = []
    for k in dashboard.kanaalcijfers(dagen):
        if k["id"] not in KANALEN:
            continue
        cijfers = {r["sleutel"]: r for r in k["regels"]}
        volg = cijfers.get("volgers") or cijfers.get("abonnees")
        hoofd = cijfers.get(HOOFDCIJFER[k["id"]])
        uit.append({"id": k["id"], "naam": KORT[k["id"]], "volledig": k["naam"], "heeft_cijfers": k["heeft_cijfers"],
                    "volgers": volg and volg["waarde"], "volgers_delta": volg and volg["delta"],
                    "hoofd_label": hoofd and hoofd["label"], "hoofd": hoofd and hoofd["waarde"], "hoofd_delta": hoofd and hoofd["delta"],
                    "regels": k["regels"]})
    return uit


def posts(dagen=14):
    """De posts van de laatste dagen over alle kanalen, nieuwste eerst. None als de Doellijn hier niet draait."""
    if not config.ZEROHERO_ACTIEF:
        return None
    from ..zerohero import main as zh  # pylint: disable=import-outside-toplevel
    sinds = dt.datetime.now(config.TZ) - dt.timedelta(days=dagen)
    uit = []
    for r in zh.q("""SELECT kanaal, tekst, gepubliceerd, link, likes, reacties, gedeeld, bereik, weergaven, interacties FROM meta_posts
                     WHERE gepubliceerd >= %s ORDER BY gepubliceerd DESC LIMIT 60""", (sinds,)):
        inter = r["interacties"] if r["interacties"] is not None else sum(x or 0 for x in (r["likes"], r["reacties"], r["gedeeld"]))
        uit.append({"kanaal": r["kanaal"], "tekst": (r["tekst"] or "").strip(), "tijd": r["gepubliceerd"], "link": r["link"] or "",
                    "weergaven": r["weergaven"] if r["weergaven"] is not None else r["bereik"], "interacties": inter, "reacties": r["reacties"]})
    for r in zh.q("""SELECT link, titel, aangemaakt, weergaven, reacties, commentaren, reposts FROM li_posts
                     WHERE aangemaakt >= %s ORDER BY aangemaakt DESC LIMIT 30""", (sinds.date(),)):
        uit.append({"kanaal": "linkedin_pagina", "tekst": (r["titel"] or "").strip(), "tijd": _als_tijd(r["aangemaakt"]), "link": r["link"] or "",
                    "weergaven": r["weergaven"], "interacties": sum(x or 0 for x in (r["reacties"], r["commentaren"], r["reposts"])),
                    "reacties": r["commentaren"]})
    for r in zh.q("""SELECT url, publicatie, interacties, impressies FROM lp_posts WHERE publicatie >= %s ORDER BY publicatie DESC LIMIT 30""",
                  (sinds.date(),)):
        uit.append({"kanaal": "linkedin", "tekst": "", "tijd": _als_tijd(r["publicatie"]), "link": r["url"] or "",
                    "weergaven": r["impressies"], "interacties": r["interacties"], "reacties": None})
    for r in zh.q("""SELECT id, titel, gepubliceerd, weergaven, likes, reacties FROM yt_videos WHERE gepubliceerd >= %s
                     ORDER BY gepubliceerd DESC LIMIT 20""", (sinds,)):
        uit.append({"kanaal": "youtube", "tekst": (r["titel"] or "").strip(), "tijd": r["gepubliceerd"],
                    "link": f"https://www.youtube.com/watch?v={r['id']}", "weergaven": r["weergaven"],
                    "interacties": (r["likes"] or 0) + (r["reacties"] or 0), "reacties": r["reacties"]})
    for p in uit:
        p["naam"] = KORT.get(p["kanaal"], p["kanaal"])
        p["link"] = p["link"] if p["link"].startswith(("http://", "https://")) else ""
        p["weergaven"], p["interacties"] = _getal(p["weergaven"]), _getal(p["interacties"])
    nul = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    uit.sort(key=lambda p: p["tijd"] or nul, reverse=True)
    return uit


def _als_tijd(d):
    return dt.datetime.combine(d, dt.time(12), tzinfo=config.TZ) if d else None


def beste(posts_, dagen=7, nu=None):
    """De post met de meeste interacties in de laatste dagen. Pure functie."""
    nu = nu or dt.datetime.now(config.TZ)
    recent = [p for p in posts_ or [] if p["tijd"] and p["tijd"] >= nu - dt.timedelta(days=dagen) and p["interacties"]]
    return max(recent, key=lambda p: (p["interacties"], p["weergaven"] or 0)) if recent else None


def pijplijn():
    """Concepten in de Doellijn die klaar zijn maar nog niet gepubliceerd. None als de Doellijn hier niet draait."""
    if not config.ZEROHERO_ACTIEF:
        return None
    from ..zerohero import main as zh  # pylint: disable=import-outside-toplevel
    rijen = zh.q("""SELECT id, blog_titel, linkedin, instagram, sport, klaar_op, aangemaakt FROM concepten
                    WHERE status = 'klaar' AND gepubliceerd_op IS NULL AND aangemaakt > now() - interval '60 days'
                    ORDER BY coalesce(klaar_op, aangemaakt) DESC LIMIT 12""")
    return [{"id": r["id"], "titel": r["blog_titel"] or "Concept zonder titel", "linkedin": r["linkedin"] or "",
             "instagram": r["instagram"] or "", "sport": r["sport"] or "", "klaar": r["klaar_op"] or r["aangemaakt"]} for r in rijen]


def vermeldingen(dagen=14):
    """Nieuws waarin PRO Helmond of Martijn genoemd wordt (uit Promonitor). None als Promonitor hier niet draait."""
    if not config.POLITIEK_ACTIEF:
        return None
    from ..politiek import main as pm  # pylint: disable=import-outside-toplevel
    with pm.pool.connection() as c:
        rijen = c.execute("""SELECT id, titel, document_url, bronnaam, toegevoegd, context FROM stukken
                             WHERE bron = 'nieuws' AND toegevoegd > now() - make_interval(days => %s) AND document_url ~ '^https?://'
                               AND (titel ~* %s OR context ~* %s) ORDER BY toegevoegd DESC LIMIT 10""",
                          (dagen, VERMELDING.pattern, VERMELDING.pattern)).fetchall()
    return [{"id": r["id"], "titel": r["titel"], "url": r["document_url"], "bron": r["bronnaam"] or "", "tijd": r["toegevoegd"]} for r in rijen]


def veilig(functie, *args):
    """Een bron die hapert, mag de rest niet tegenhouden. Geeft (uitkomst, fout)."""
    try:
        return functie(*args), ""
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:200]


def overzicht(dagen=7):
    """Alles voor het scherm Socials (en kort voor Vandaag en Iris)."""
    k, fk = veilig(kanalen, dagen)
    p, fp = veilig(posts, 14)
    pl, fpl = veilig(pijplijn)
    v, fv = veilig(vermeldingen, 14)
    return {"kanalen": k, "posts": p, "beste": beste(p, dagen), "pijplijn": pl, "vermeldingen": v,
            "fouten": [f for f in (fk, fp, fpl, fv) if f]}
