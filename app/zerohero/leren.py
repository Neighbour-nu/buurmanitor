"""De nieuwsfilter laten leren van wat Martijn negeert.

Bij Negeer geef je een reden (en bij 'geen keeper' welke namen geen keeper zijn). Dat werkt op twee manieren:
1. Direct in de cockpit: nieuwe berichten waarin alleen namen staan die als 'geen keeper' bekendstaan, worden
   automatisch weggefilterd (status geleerd_weg). Ze zijn terug te vinden in de Inbox onder Weggefilterd.
2. In de weging: n8n haalt bij elke ronde de leerpunten op (/intern/weging/leerpunten) en zet ze in de opdracht
   aan Claude: de namen die geen keeper zijn, je eigen filterregels en recente voorbeelden van genegeerde berichten.
"""
import datetime as dt

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .main import CONCEPT_SECRET, clusteren, controleer_herkomst, ctx, q, q1, templates

router = APIRouter()

MIGRATIES = [
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS negeer_reden text",
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS negeer_toelichting text",
    """CREATE TABLE IF NOT EXISTS geen_keepers (
        naam text PRIMARY KEY, weergave text, toelichting text, item_id int, sinds timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS filterregels (
        id serial PRIMARY KEY, tekst text NOT NULL, actief boolean NOT NULL DEFAULT true, sinds timestamptz NOT NULL DEFAULT now())""",
]

REDENEN = {
    "geen_keeper": "Gaat niet over een keeper (veldspeler, trainer of ander)",
    "niet_relevant": "Wel een keeper, maar niet interessant voor ZEROHERO",
    "dubbel": "Dubbel of al eerder gezien",
    "oud": "Oud nieuws",
    "verkeerde_info": "Klopt niet (verkeerde samenvatting of score)",
    "anders": "Anders",
}
GELEERD = "geleerd_weg"
templates.env.globals["negeer_redenen"] = REDENEN


def norm(naam):
    return " ".join(str(naam or "").split()).lower()


def toepassen(q_=None):
    """Filtert nieuwe berichten weg waarin alleen namen staan die als 'geen keeper' bekendstaan."""
    qq = q_ or q
    return qq("""UPDATE items SET status = %s, status_op = now(), negeer_reden = 'automatisch: geen keeper'
                 WHERE status = 'nieuw' AND binnen > now() - interval '7 days' AND coalesce(negeer_reden, '') <> 'teruggezet'
                   AND coalesce(array_length(keepers, 1), 0) > 0
                   AND NOT EXISTS (SELECT 1 FROM unnest(keepers) k
                                   WHERE lower(regexp_replace(trim(k), '\\s+', ' ', 'g')) NOT IN (SELECT naam FROM geen_keepers))
                 RETURNING id""", (GELEERD,))


def leerpunten():
    geen = q("SELECT * FROM geen_keepers ORDER BY sinds DESC")
    regels = q("SELECT * FROM filterregels WHERE actief ORDER BY id")
    voorbeelden = q("""SELECT titel, sport, keepers, negeer_reden, negeer_toelichting, score FROM items
                       WHERE status = 'genegeerd' AND negeer_reden IS NOT NULL AND status_op > now() - interval '120 days'
                       ORDER BY status_op DESC LIMIT 15""")
    delen = []
    if geen:
        delen.append("Deze personen zijn GEEN keeper (Martijn heeft dat gecorrigeerd). Zet ze nooit in keepers en geef "
                     "keepergehalte 0 als het bericht alleen over hen gaat: " + "; ".join(g["weergave"] or g["naam"] for g in geen) + ".")
    if regels:
        delen.append("Eigen filterregels van Martijn (volg ze strikt):\n" + "\n".join(f"- {r['tekst']}" for r in regels))
    if voorbeelden:
        regels_vb = []
        for v in voorbeelden:
            reden = REDENEN.get(v["negeer_reden"], v["negeer_reden"])
            extra = f" ({v['negeer_toelichting']})" if v["negeer_toelichting"] else ""
            regels_vb.append(f"- \"{(v['titel'] or '')[:140]}\" [{v['sport'] or 'onbekend'}, score was {v['score']}]: {reden}{extra}")
        delen.append("Recente berichten die Martijn negeerde, met zijn reden. Leer hiervan en geef vergelijkbare berichten "
                     "een lagere score:\n" + "\n".join(regels_vb))
    return {"geen_keepers": [g["weergave"] or g["naam"] for g in geen], "regels": [r["tekst"] for r in regels],
            "voorbeelden": len(voorbeelden), "tekst": "\n\n".join(delen)}


def _kaart(request, id_lijst, weergave):
    rijen = q("SELECT * FROM items WHERE id = ANY(%s) ORDER BY score DESC NULLS LAST", (id_lijst,))
    if weergave == "verhaal":
        return templates.TemplateResponse("_verhaal.html", ctx(request, v=clusteren(rijen)[0], nr=None))
    return templates.TemplateResponse("_item.html", ctx(request, x=rijen[0]))


def _ids(ids):
    lijst = [int(i) for i in str(ids).split(",") if i.strip().isdigit()][:50]
    if not lijst:
        raise HTTPException(400, "Geen berichten")
    return lijst


@router.get("/items/negeer-vraag", response_class=HTMLResponse)
def negeer_vraag(request: Request, ids: str, weergave: str = "item"):
    id_lijst = _ids(ids)
    namen = sorted({k for r in q("SELECT keepers FROM items WHERE id = ANY(%s)", (id_lijst,)) for k in (r["keepers"] or []) if k})
    return templates.TemplateResponse("_negeer_vraag.html", ctx(request, ids=ids, weergave=weergave, namen=namen, redenen=REDENEN))


@router.post("/items/negeer", response_class=HTMLResponse)
async def negeer(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    id_lijst = _ids(form.get("ids") or "")
    weergave = str(form.get("weergave") or "item")
    reden = str(form.get("reden") or "").strip()
    reden = reden if reden in REDENEN else None
    toelichting = str(form.get("toelichting") or "").strip()[:500] or None
    q("""UPDATE items SET status = 'genegeerd', status_op = now(), negeer_reden = %s, negeer_toelichting = %s
         WHERE id = ANY(%s) RETURNING id""", (reden, toelichting, id_lijst))
    if reden == "geen_keeper":
        for naam in form.getlist("geen_keeper"):
            n = norm(naam)
            if n:
                q("""INSERT INTO geen_keepers (naam, weergave, toelichting, item_id) VALUES (%s, %s, %s, %s)
                     ON CONFLICT (naam) DO UPDATE SET toelichting = coalesce(EXCLUDED.toelichting, geen_keepers.toelichting) RETURNING 1""",
                  (n, " ".join(str(naam).split()), toelichting, id_lijst[0]))
        toepassen()
    return _kaart(request, id_lijst, weergave)


def terugzetten(id_lijst):
    """Zet berichten terug op nieuw. Namen die vanuit deze berichten op 'geen keeper' kwamen, gaan er weer af,
    en berichten die alleen daarom automatisch waren weggefilterd, komen ook terug. Geeft de verwijderde namen terug."""
    # 'teruggezet' houdt deze berichten buiten het automatisch wegfilteren, ook als de naam op de lijst blijft.
    q("""UPDATE items SET status = 'nieuw', status_op = now(), negeer_reden = 'teruggezet', negeer_toelichting = NULL
         WHERE id = ANY(%s) RETURNING id""", (id_lijst,))
    weg = q("DELETE FROM geen_keepers WHERE item_id = ANY(%s) RETURNING naam, weergave", (id_lijst,))
    for n in [r["naam"] for r in weg]:
        q("""UPDATE items SET status = 'nieuw', negeer_reden = NULL
             WHERE status = %s AND %s = ANY(SELECT lower(regexp_replace(trim(k), '\\s+', ' ', 'g')) FROM unnest(keepers) k)
             RETURNING id""", (GELEERD, n))
    toepassen()
    return [r["weergave"] or r["naam"] for r in weg]


@router.post("/items/terugzetten", response_class=HTMLResponse)
async def terugzetten_route(request: Request):
    controleer_herkomst(request)
    form = await request.form()
    id_lijst = _ids(form.get("ids") or "")
    namen = terugzetten(id_lijst)
    if form.get("terug") == "filter":
        from urllib.parse import quote
        return RedirectResponse("/filter?teruggezet=" + quote(", ".join(namen) or "-") + "#genegeerd", status_code=303)
    return _kaart(request, id_lijst, str(form.get("weergave") or "item"))


# ---------- beheerpagina ----------

@router.get("/filter", response_class=HTMLResponse)
def filter_scherm(request: Request):
    toepassen()
    geen = q("SELECT * FROM geen_keepers ORDER BY sinds DESC")
    regels = q("SELECT * FROM filterregels ORDER BY actief DESC, id")
    genegeerd = q("""SELECT id, titel, url, sport, keepers, score, negeer_reden, negeer_toelichting, status_op FROM items
                     WHERE status = 'genegeerd' AND status_op > now() - interval '60 days' ORDER BY status_op DESC LIMIT 40""")
    per_reden = q("""SELECT negeer_reden, count(*) AS n, round(avg(score)) AS gem FROM items
                     WHERE status = 'genegeerd' AND status_op > now() - interval '90 days' GROUP BY 1 ORDER BY n DESC""")
    geleerd = q1("SELECT count(*) AS n FROM items WHERE status = %s AND status_op > now() - interval '30 days'", (GELEERD,))["n"]
    weg = q("""SELECT id, titel, url, sport, keepers, score, status_op FROM items
               WHERE status = %s AND status_op > now() - interval '30 days' ORDER BY status_op DESC LIMIT 40""", (GELEERD,))
    return templates.TemplateResponse("filter.html", ctx(
        request, pagina="vandaag", geen=geen, regels=regels, genegeerd=genegeerd, per_reden=per_reden, geleerd=geleerd, weg=weg,
        redenen=REDENEN, lp=leerpunten()))


@router.post("/filter/regels")
def regel_nieuw(request: Request, tekst: str = Form(...)):
    controleer_herkomst(request)
    t = " ".join(tekst.split())[:300]
    if t:
        q("INSERT INTO filterregels (tekst) VALUES (%s) RETURNING 1", (t,))
    return RedirectResponse("/filter#regels", status_code=303)


@router.post("/filter/regels/{rid}/aan")
def regel_aan(request: Request, rid: int):
    controleer_herkomst(request)
    q("UPDATE filterregels SET actief = NOT actief WHERE id = %s RETURNING 1", (rid,))
    return RedirectResponse("/filter#regels", status_code=303)


@router.post("/filter/regels/{rid}/weg")
def regel_weg(request: Request, rid: int):
    controleer_herkomst(request)
    q("DELETE FROM filterregels WHERE id = %s RETURNING 1", (rid,))
    return RedirectResponse("/filter#regels", status_code=303)


@router.post("/filter/geen-keeper")
def geen_keeper_nieuw(request: Request, naam: str = Form(...), toelichting: str = Form("")):
    controleer_herkomst(request)
    n = norm(naam)
    if n:
        q("""INSERT INTO geen_keepers (naam, weergave, toelichting) VALUES (%s, %s, %s)
             ON CONFLICT (naam) DO NOTHING RETURNING 1""", (n, " ".join(naam.split()), toelichting.strip() or None))
        toepassen()
    return RedirectResponse("/filter#namen", status_code=303)


@router.post("/filter/geen-keeper/weg")
def geen_keeper_weg(request: Request, naam: str = Form(...)):
    controleer_herkomst(request)
    q("DELETE FROM geen_keepers WHERE naam = %s RETURNING 1", (norm(naam),))
    # Berichten die alleen hierom automatisch waren weggefilterd, komen terug.
    q("""UPDATE items SET status = 'nieuw', negeer_reden = NULL
         WHERE status = %s AND %s = ANY(SELECT lower(regexp_replace(trim(k), '\\s+', ' ', 'g')) FROM unnest(keepers) k) RETURNING id""",
      (GELEERD, norm(naam)))
    toepassen()
    return RedirectResponse("/filter#namen", status_code=303)


# ---------- voor n8n ----------

@router.get("/intern/weging/leerpunten")
def intern_leerpunten(request: Request):
    """Alleen bereikbaar binnen het servernetwerk (zonder Caddy ervoor) of met het geheime wachtwoord."""
    van_buiten = bool(request.headers.get("x-forwarded-for") or request.headers.get("x-forwarded-host"))
    if van_buiten and (not CONCEPT_SECRET or request.headers.get("x-doellijn-secret") != CONCEPT_SECRET):
        raise HTTPException(403)
    toepassen()
    uit = leerpunten()
    try:
        from . import rekening
        uit["zoeken"] = rekening.zoekinstelling(q)
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(uit)
