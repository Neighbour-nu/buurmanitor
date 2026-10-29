"""Kosten van Doellijn: wat Claude kost (gemeten aan de tokens die de cockpit bijhoudt) en de vaste kosten die Martijn invult.

Prijzen in dollars per miljoen tokens (standaardtarief van Anthropic, oktober 2026). Webzoekopdrachten kosten 10 dollar per 1000.
Waar de cockpit het aantal zoekopdrachten niet bijhoudt (n8n-werkstromen), schatten we het aan de hand van hoe de werkstroom zoekt.
"""
import datetime as dt
import re

PRIJZEN = {"sonnet": (2.0, 10.0), "haiku": (1.0, 5.0), "opus": (5.0, 25.0)}
ZOEKOPDRACHT = 0.01
SCREENSHOT = 0.005          # één screenshot lezen met Haiku, ongeveer
SCHATTING_ZOEK = {"concept": 3, "artikel_idee": 4, "item": 0.1}
KOERS = 0.86                # euro per dollar, aan te passen bij de vaste kosten

MIGRATIES = [
    "CREATE TABLE IF NOT EXISTS instellingen (sleutel text PRIMARY KEY, waarde text, bijgewerkt timestamptz NOT NULL DEFAULT now())",
]

VASTE_STANDAARD = """VPS bij OVH: (vul in)
Domeinnaam: (vul in)"""


def prijs(model):
    m = (model or "sonnet").lower()
    for sleutel, p in PRIJZEN.items():
        if sleutel in m:
            return p
    return PRIJZEN["sonnet"]


def dollar(tin, tout, model=None, zoek=0):
    pin, pout = prijs(model)
    return (tin or 0) * pin / 1e6 + (tout or 0) * pout / 1e6 + (zoek or 0) * ZOEKOPDRACHT


def _onderdelen(q, van, tot):
    """Kosten per onderdeel tussen twee tijdstippen. Geeft een lijst dicts met naam, aantal, dollars en of het geschat is."""
    p = (van, tot)
    uit = []
    r = q("""SELECT model, count(*) AS n, coalesce(sum(tokens_in), 0) AS tin, coalesce(sum(tokens_out), 0) AS tout
             FROM items WHERE binnen >= %s AND binnen < %s GROUP BY model""", p)
    uit.append({"naam": "Nieuws wegen (inbox en Telegram)", "eenheid": "berichten", "aantal": sum(x["n"] for x in r),
                "dollar": sum(dollar(x["tin"], x["tout"], x["model"] or "haiku", x["n"] * SCHATTING_ZOEK["item"]) for x in r), "geschat": True})
    r = q("""SELECT model, count(*) AS n, coalesce(sum(tokens_in), 0) AS tin, coalesce(sum(tokens_out), 0) AS tout,
                    count(*) FILTER (WHERE controle_op IS NOT NULL) AS controles
             FROM concepten WHERE aangemaakt >= %s AND aangemaakt < %s AND artikel_id IS NULL GROUP BY model""", p)
    uit.append({"naam": "Concepten uit nieuws (incl. feitencontrole)", "eenheid": "concepten", "aantal": sum(x["n"] for x in r),
                "dollar": sum(dollar(x["tin"], x["tout"], x["model"], x["n"] * SCHATTING_ZOEK["concept"] + x["controles"] * 2) for x in r),
                "geschat": True})
    r = q("""SELECT count(*) AS n, coalesce(sum(tokens_in), 0) AS tin, coalesce(sum(tokens_out), 0) AS tout,
                    coalesce(sum(coalesce(zoekopdrachten, CASE WHEN soort = 'idee' THEN %s ELSE 0 END)), 0) AS zoek,
                    count(*) FILTER (WHERE zoekopdrachten IS NULL AND soort = 'idee') AS geschat
             FROM artikelen WHERE aangemaakt >= %s AND aangemaakt < %s""", (SCHATTING_ZOEK["artikel_idee"],) + p)[0]
    c = q("""SELECT count(*) AS n, coalesce(sum(c.tokens_in), 0) AS tin, coalesce(sum(c.tokens_out), 0) AS tout FROM concepten c
             WHERE c.aangemaakt >= %s AND c.aangemaakt < %s AND c.artikel_id IS NOT NULL""", p)[0]
    uit.append({"naam": "Eigen artikelen (schrijven, rondes, social)", "eenheid": "artikelen", "aantal": r["n"],
                "dollar": dollar(r["tin"], r["tout"], "sonnet", r["zoek"]) + dollar(c["tin"], c["tout"], "sonnet"), "geschat": bool(r["geschat"])})
    a = q("""SELECT count(*) AS n, coalesce(sum(tokens_in), 0) AS tin, coalesce(sum(tokens_out), 0) AS tout,
                    coalesce(sum(zoekopdrachten), 0) AS zoek FROM advies_analyses WHERE aangemaakt >= %s AND aangemaakt < %s""", p)[0]
    g = q("""SELECT count(*) AS n, coalesce(sum(tokens_in), 0) AS tin, coalesce(sum(tokens_out), 0) AS tout,
                    coalesce(sum(zoekopdrachten), 0) AS zoek FROM advies_gesprekken WHERE aangemaakt >= %s AND aangemaakt < %s""", p)[0]
    uit.append({"naam": "Adviseur (analyses en gesprekken)", "eenheid": "analyses en gesprekken", "aantal": a["n"] + g["n"],
                "dollar": dollar(a["tin"], a["tout"], "sonnet", a["zoek"]) + dollar(g["tin"], g["tout"], "sonnet", g["zoek"]), "geschat": False})
    s = q("""SELECT (SELECT count(*) FROM ssi_screenshots WHERE moment >= %s AND moment < %s) AS n""", p)[0]["n"]
    uit.append({"naam": "Screenshots lezen (SSI, profielbezoekers)", "eenheid": "screenshots", "aantal": s, "dollar": s * SCREENSHOT, "geschat": True})
    return uit


def _tabel_bestaat(q, naam):
    return bool(q("SELECT 1 AS x FROM information_schema.tables WHERE table_name = %s", (naam,)))


def vaste_kosten(q):
    """Regels 'Naam: bedrag' (euro per maand). Regels zonder bedrag tellen niet mee."""
    r = q("SELECT waarde FROM instellingen WHERE sleutel = 'vaste_kosten'")
    tekst = r[0]["waarde"] if r and r[0]["waarde"] is not None else VASTE_STANDAARD
    regels = []
    for regel in tekst.splitlines():
        if ":" not in regel:
            continue
        naam, rest = regel.split(":", 1)
        m = re.search(r"\d+(?:[.,]\d{1,2})?", rest)
        bedrag = float(m.group().replace(",", ".")) if m else None
        if "jaar" in rest.lower() and bedrag is not None:
            bedrag = bedrag / 12
        regels.append({"naam": naam.strip(), "bedrag": bedrag, "tekst": rest.strip()})
    return tekst, regels


def koers(q):
    r = q("SELECT waarde FROM instellingen WHERE sleutel = 'koers_eur_usd'")
    try:
        return float(r[0]["waarde"].replace(",", ".")) if r else KOERS
    except (ValueError, AttributeError):
        return KOERS


def overzicht(q, dagen):
    if not _tabel_bestaat(q, "ssi_screenshots") or not _tabel_bestaat(q, "advies_analyses"):
        return None
    nu = dt.datetime.now(dt.timezone.utc)
    vandaag = dt.date.today()
    maand_start = dt.datetime.combine(vandaag.replace(day=1), dt.time(), tzinfo=nu.tzinfo)
    vorige_start = (maand_start - dt.timedelta(days=1)).replace(day=1)
    periode = _onderdelen(q, nu - dt.timedelta(days=dagen), nu)
    deze_maand = sum(x["dollar"] for x in _onderdelen(q, maand_start, nu))
    vorige_maand = sum(x["dollar"] for x in _onderdelen(q, vorige_start, maand_start))
    laatste30 = sum(x["dollar"] for x in _onderdelen(q, nu - dt.timedelta(days=30), nu))
    start = q("SELECT min(binnen) AS t FROM items")[0]["t"] or nu
    totaal = sum(x["dollar"] for x in _onderdelen(q, start - dt.timedelta(days=1), nu))
    dagen_maand = ((maand_start + dt.timedelta(days=32)).replace(day=1) - maand_start).days
    actief = max(1, min(30, (nu - start).days or 1))
    per_dag = laatste30 / actief
    vast_tekst, vast = vaste_kosten(q)
    vast_maand = sum(r["bedrag"] for r in vast if r["bedrag"] is not None)
    k = koers(q)
    maanden = q("""WITH m AS (SELECT generate_series(date_trunc('month', current_date) - interval '11 months', date_trunc('month', current_date), interval '1 month') AS m)
                   SELECT m::date AS maand FROM m ORDER BY m""")
    reeks = []
    for r in maanden:
        van = dt.datetime.combine(r["maand"], dt.time(), tzinfo=nu.tzinfo)
        tot = dt.datetime.combine((r["maand"] + dt.timedelta(days=32)).replace(day=1), dt.time(), tzinfo=nu.tzinfo)
        if tot < start:
            continue
        reeks.append({"maand": r["maand"], "dollar": sum(x["dollar"] for x in _onderdelen(q, van, min(tot, nu)))})
    from . import rekening
    echt = rekening.overzicht(q, dagen)
    if echt:
        # De echte rekening van Anthropic (inclusief n8n en webzoekopdrachten) vervangt de schatting in de tegels.
        geschat = sum(x["dollar"] for x in periode)
        if echt["periode"] - geschat > 0.01:
            periode = periode + [{"naam": "Niet apart te meten (vooral n8n: zoekstap van de weging, Telegram, feitencontrole)",
                                  "eenheid": "", "aantal": 0, "dollar": echt["periode"] - geschat, "geschat": False, "rest": True}]
        deze_maand, vorige_maand, per_dag = echt["deze_maand"], echt["vorige_maand"], echt["per_dag"]
        totaal, start, actief = echt["totaal"], dt.datetime.combine(echt["sinds"], dt.time(), tzinfo=nu.tzinfo), echt["tempo_dagen"]
        prognose_maand = echt["prognose_maand"]
        reeks = [{"maand": r["maand"], "dollar": rekening.maandtotaal(q, r["maand"]) or 0} for r in maanden
                 if (r["maand"] + dt.timedelta(days=32)).replace(day=1) > echt["sinds"]]
    else:
        prognose_maand = deze_maand + per_dag * (dagen_maand - vandaag.day)
    spaar = rekening.stand(q)
    return {"periode": periode, "periode_totaal": sum(x["dollar"] for x in periode), "deze_maand": deze_maand, "echt": echt, "spaar": spaar,
            "zoekstanden": rekening.ZOEKSTANDEN, "rekening_actief": rekening.actief(),
            "vorige_maand": vorige_maand, "totaal": totaal, "sinds": start, "per_dag": per_dag,
            "prognose_maand": prognose_maand, "prognose_jaar": per_dag * 365,
            "vast": vast, "vast_tekst": vast_tekst, "vast_maand": vast_maand, "koers": k, "reeks": reeks,
            "totaal_maand_eur": prognose_maand * k + vast_maand,
            "totaal_jaar_eur": per_dag * 365 * k + vast_maand * 12, "gemeten_dagen": actief}
