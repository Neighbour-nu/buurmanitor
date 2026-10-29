"""Social Selling Index (SSI) van LinkedIn: maandelijks een screenshot van linkedin.com/sales/ssi uploaden.

LinkedIn heeft geen export of API voor de SSI. De cockpit leest de cijfers uit de screenshot met Claude (Haiku, met beeld),
laat ze controleren en bewaart ze dan, samen met de screenshot zelf. Zonder ANTHROPIC_API_KEY vul je de cijfers zelf in
naast de screenshot. De score en de vier onderdelen gaan ook naar metingen (kanaal linkedin), zodat ze in de CSV-download
en op het tabblad Bereik staan.
"""
import base64
import datetime as dt
import io
import json
import os
import urllib.error
import urllib.request

SLEUTEL = os.environ.get("ZEROHERO_ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("ZEROHERO_SSI_MODEL", "claude-haiku-4-5")

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS ssi_screenshots (
        id serial PRIMARY KEY, moment timestamptz NOT NULL DEFAULT now(), mime text, data bytea, gelezen jsonb)""",
    """CREATE TABLE IF NOT EXISTS ssi_metingen (
        datum date PRIMARY KEY, ssi numeric, merk numeric, personen numeric, informatie numeric, relaties numeric,
        team_rang text, team_gem numeric, branche_naam text, branche_gem numeric, branche_top numeric,
        netwerk_gem numeric, netwerk_top numeric, screenshot_id int, bron text, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
]

# Veld, label, of het een getal is. De vier onderdelen tellen samen op tot de SSI (elk maximaal 25).
VELDEN = [
    ("ssi", "SSI (0 tot 100)", True),
    ("merk", "Uw professionele merk creëren (0 tot 25)", True),
    ("personen", "Vind de juiste personen (0 tot 25)", True),
    ("informatie", "Informatie uitwisselen (0 tot 25)", True),
    ("relaties", "Relaties opbouwen (0 tot 25)", True),
    ("branche_top", "Positie in de branche: beste ...%", True),
    ("branche_gem", "Gemiddelde SSI in de branche", True),
    ("branche_naam", "Naam van de branche", False),
    ("netwerk_top", "Positie in je netwerk: beste ...%", True),
    ("netwerk_gem", "Gemiddelde SSI in je netwerk", True),
    ("team_rang", "Rang in je team (bijv. 1 van 1)", False),
    ("team_gem", "Gemiddelde SSI in je team", True),
]
ONDERDELEN = [("merk", "Professioneel merk"), ("personen", "Juiste personen vinden"),
              ("informatie", "Informatie uitwisselen"), ("relaties", "Relaties opbouwen")]

PROMPT = """Dit is een screenshot van de pagina Social Selling Index (SSI) van LinkedIn, mogelijk in het Nederlands.
Lees de getallen af en geef ALLEEN een JSON-object terug, zonder uitleg, met deze sleutels:
ssi (de huidige score van 0 tot 100), merk (Uw professionele merk creëren), personen (Vind de juiste personen),
informatie (Informatie uitwisselen), relaties (Relaties opbouwen), team_rang (tekst, bijvoorbeeld "1 van 1"),
team_gem (gemiddelde SSI van het team), branche_naam (de bedrijfstak die genoemd wordt), branche_gem (gemiddelde SSI
in die bedrijfstak), branche_top (het percentage achter 'beste' bij de branche, alleen het getal), netwerk_gem
(gemiddelde SSI in het netwerk), netwerk_top (het percentage achter 'beste' bij het netwerk, alleen het getal).
Getallen met een punt als decimaalteken (21,625 wordt 21.625). Gebruik null als iets niet op de screenshot staat."""


def actief():
    return bool(SLEUTEL)


def verklein(inhoud):
    """Maakt de screenshot kleiner (max 1800 px breed, JPEG) zodat hij snel te lezen en klein te bewaren is."""
    from PIL import Image
    beeld = Image.open(io.BytesIO(inhoud))
    beeld = beeld.convert("RGB")
    if beeld.width > 1800:
        beeld = beeld.resize((1800, round(beeld.height * 1800 / beeld.width)))
    uit = io.BytesIO()
    beeld.save(uit, "JPEG", quality=85)
    return uit.getvalue(), "image/jpeg"


def lees(data, mime, prompt=None, velden=None):
    """Vraagt Claude de cijfers af te lezen. Geeft (dict, fout) terug. Ook gebruikt voor andere screenshots (profielbezoekers)."""
    prompt = prompt or PROMPT
    velden = velden or VELDEN
    if not actief():
        return {}, "Er is geen Claude-sleutel ingesteld; vul de cijfers zelf in."
    body = {"model": MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": prompt}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": SLEUTEL, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            antwoord = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {}, f"Claude gaf fout {e.code}; vul de cijfers zelf in."
    except Exception as e:  # noqa: BLE001
        return {}, f"Claude niet bereikbaar ({type(e).__name__}); vul de cijfers zelf in."
    tekst = "".join(b.get("text", "") for b in antwoord.get("content", []) if b.get("type") == "text")
    try:
        start, eind = tekst.index("{"), tekst.rindex("}") + 1
        gelezen = json.loads(tekst[start:eind])
    except ValueError:
        return {}, "De cijfers konden niet worden afgelezen; vul ze zelf in."
    return {k: gelezen.get(k) for k, _l, _g in velden}, None


def _num(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).strip().replace("%", "").replace(",", "."))
    except ValueError:
        return None


def opslaan(q, datum, velden, screenshot_id, bron):
    w = {}
    for k, _l, getal in VELDEN:
        v = velden.get(k)
        w[k] = _num(v) if getal else ((str(v).strip() or None) if v is not None else None)
    kol = ", ".join(w)
    upd = ", ".join(f"{k} = EXCLUDED.{k}" for k in w)
    q(f"""INSERT INTO ssi_metingen (datum, {kol}, screenshot_id, bron, bijgewerkt)
          VALUES (%s, {", ".join(["%s"] * len(w))}, %s, %s, now())
          ON CONFLICT (datum) DO UPDATE SET {upd}, screenshot_id = coalesce(EXCLUDED.screenshot_id, ssi_metingen.screenshot_id),
            bron = EXCLUDED.bron, bijgewerkt = now() RETURNING 1""", (datum, *w.values(), screenshot_id, bron))
    for sleutel, waarde in (("ssi", w["ssi"]), ("ssi_merk", w["merk"]), ("ssi_personen", w["personen"]),
                            ("ssi_informatie", w["informatie"]), ("ssi_relaties", w["relaties"]),
                            ("ssi_branche_top", w["branche_top"]), ("ssi_netwerk_top", w["netwerk_top"])):
        if waarde is not None:
            q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES ('linkedin', %s, %s, %s, 'screenshot')
                 ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'screenshot', bijgewerkt = now()
                 RETURNING 1""", (sleutel, datum, waarde))
    return w


def historie(q):
    return q("SELECT * FROM ssi_metingen ORDER BY datum")


def upload_nodig(q):
    r = q("SELECT max(datum) AS d FROM ssi_metingen")[0]["d"]
    return r is None or (dt.date.today() - r).days >= 30


def overzicht(q):
    rijen = historie(q)
    o = {"rijen": rijen, "laatste": rijen[-1] if rijen else None, "vorige": rijen[-2] if len(rijen) > 1 else None,
         "nodig": upload_nodig(q), "actief": actief()}
    o["inzichten"] = _inzichten(rijen)
    return o


def _f(v, d=1):
    if v is None:
        return "·"
    w = round(float(v), d)
    if d == 0 or w == int(w):
        return str(int(round(w)))
    return str(w).replace(".", ",")


def _inzichten(rijen):
    uit = []
    if not rijen:
        return uit
    nu = rijen[-1]
    if nu["ssi"] is not None:
        delen = [(label, float(nu[k])) for k, label in ONDERDELEN if nu[k] is not None]
        if delen:
            sterk = max(delen, key=lambda x: x[1])
            zwak = min(delen, key=lambda x: x[1])
            uit.append(f"Sterkste onderdeel: {sterk[0].lower()} ({_f(sterk[1], 2)} van 25). "
                       f"Meeste ruimte: {zwak[0].lower()} ({_f(zwak[1], 2)} van 25, nog {_f(25 - zwak[1], 1)} punten te winnen).")
        if nu["branche_gem"] is not None:
            uit.append(f"Je SSI van {_f(nu['ssi'], 0)} ligt {_f(float(nu['ssi']) - float(nu['branche_gem']), 0)} punten boven het gemiddelde "
                       f"van {nu['branche_naam'] or 'je branche'} ({_f(nu['branche_gem'], 0)})"
                       + (f"; daarmee hoor je bij de beste {_f(nu['branche_top'], 0)}%." if nu["branche_top"] is not None else "."))
    if len(rijen) > 1 and nu["ssi"] is not None:
        eerste = next((r for r in rijen if r["ssi"] is not None), None)
        if eerste and eerste is not nu:
            v = float(nu["ssi"]) - float(eerste["ssi"])
            uit.append(f"Sinds {eerste['datum'].strftime('%d-%m-%Y')} is je SSI {'gestegen' if v > 0 else 'gedaald' if v < 0 else 'gelijk gebleven'}"
                       + (f" met {_f(abs(v), 1)} {'punt' if abs(v) == 1 else 'punten'}." if v else "."))
            groei = []
            for k, label in ONDERDELEN:
                if nu[k] is not None and eerste[k] is not None:
                    groei.append((label, float(nu[k]) - float(eerste[k])))
            if groei:
                g = max(groei, key=lambda x: x[1])
                if g[1] > 0:
                    uit.append(f"Grootste groei sinds de eerste meting: {g[0].lower()} (+{_f(g[1], 2)}).")
    return uit
