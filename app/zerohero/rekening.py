"""De echte Claude-rekening, opgehaald bij Anthropic, en de spaarstand van de cockpit.

1. Werkelijke kosten: met een admin-sleutel (ANTHROPIC_ADMIN_KEY, via deploy/anthropic_admin.sh) haalt de cockpit elke
   drie uur het kostenrapport op (Admin API, /v1/organizations/cost_report): per dag, per model en per soort (tokens,
   webzoekopdrachten). Dat is wat Anthropic factureert, dus inclusief alles wat n8n doet (weging, Telegram, concepten,
   feitencontrole). De schatting per onderdeel op het dashboard blijft, met het verschil apart zichtbaar.
2. Spaarstand: hoe ruim de n8n-weging bij kale Google News-koppen op het web zoekt, en een maandbudget. Boven het
   budget gaan de zoekstap en de automatische maandaganalyse van de adviseur vanzelf uit. n8n haalt de stand elke ronde op
   via /intern/weging/leerpunten.
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

API = "https://api.anthropic.com/v1/organizations/cost_report"
SLEUTEL = os.environ.get("ZEROHERO_ANTHROPIC_ADMIN_KEY", "").strip()
WERKRUIMTE = os.environ.get("ZEROHERO_ANTHROPIC_WERKRUIMTE", "").strip()   # leeg = alle werkruimtes van de organisatie
INTERVAL = 3 * 3600

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS claude_rekening (
        datum date NOT NULL, werkruimte text NOT NULL DEFAULT '', omschrijving text NOT NULL DEFAULT '',
        soort text, model text, dollar numeric NOT NULL DEFAULT 0, bijgewerkt timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (datum, werkruimte, omschrijving))""",
    "CREATE TABLE IF NOT EXISTS instellingen (sleutel text PRIMARY KEY, waarde text, bijgewerkt timestamptz NOT NULL DEFAULT now())",
]

# Zoekstap in de n8n-weging: alleen kale koppen (Google News) met minstens 'drempel' punten, hooguit 'max' per ronde
# van een kwartier, en per bericht hooguit 'zoekopdrachten' webzoekopdrachten. Elke zoekopdracht kost 1 cent plus de
# zoekresultaten die Claude als invoer leest (vaak duizenden tokens), dus dit is de grootste kostenpost van de weging.
ZOEKSTANDEN = {
    "uit": {"naam": "Uit", "drempel": 999, "max": 0, "zoekopdrachten": 0,
            "uitleg": "Koppen worden alleen op de kop gewogen. Goedkoopst; Google News-berichten blijven kort."},
    "zuinig": {"naam": "Zuinig (aanbevolen)", "drempel": 60, "max": 2, "zoekopdrachten": 1,
               "uitleg": "Alleen koppen die op de kop al veelbelovend zijn (60 of meer), hooguit 2 per kwartier, 1 zoekopdracht per kop."},
    "ruim": {"naam": "Ruim (zoals eerst)", "drempel": 45, "max": 8, "zoekopdrachten": 2,
             "uitleg": "Koppen vanaf 45 punten, hooguit 8 per kwartier, 2 zoekopdrachten per kop. Kost ongeveer 3 tot 4 dollar per dag."},
}
STANDAARD_ZOEKSTAND = "zuinig"


def actief():
    return bool(SLEUTEL)


# ---------- instellingen ----------

def _instelling(q, sleutel, standaard=None):
    try:
        r = q("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel,))
    except Exception:  # noqa: BLE001
        return standaard
    return r[0]["waarde"] if r and r[0]["waarde"] not in (None, "") else standaard


def bewaar(q, sleutel, waarde):
    q("""INSERT INTO instellingen (sleutel, waarde, bijgewerkt) VALUES (%s, %s, now())
         ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now() RETURNING 1""", (sleutel, waarde))


def budget(q):
    try:
        b = float(str(_instelling(q, "budget_maand", "") or "").replace(",", "."))
        return b if b > 0 else None
    except ValueError:
        return None


def zoekstand_gekozen(q):
    s = _instelling(q, "zoekstand", STANDAARD_ZOEKSTAND)
    return s if s in ZOEKSTANDEN else STANDAARD_ZOEKSTAND


def deze_maand(q):
    """Claude-kosten deze maand: de echte rekening als die er is, anders de schatting van de cockpit."""
    echt = maandtotaal(q, dt.date.today().replace(day=1))
    if echt is not None:
        return echt, True
    from . import kosten
    nu = dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.combine(dt.date.today().replace(day=1), dt.time(), tzinfo=nu.tzinfo)
    return sum(x["dollar"] for x in kosten._onderdelen(q, start, nu)), False  # noqa: SLF001


def stand(q):
    """De spaarstand zoals hij nu werkt, met de reden als het budget hem heeft teruggeschroefd."""
    gekozen = zoekstand_gekozen(q)
    b = budget(q)
    besteed, echt = deze_maand(q)
    over = bool(b and besteed >= b)
    effectief = "uit" if over else gekozen
    volgende = (dt.date.today().replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    maanden = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]
    return {"tot": f"1 {maanden[volgende.month - 1]}", "gekozen": gekozen, "effectief": effectief, "zoeken": dict(ZOEKSTANDEN[effectief], stand=effectief),
            "budget": b, "besteed": besteed, "echt": echt, "over_budget": over,
            "procent": round(100 * besteed / b) if b else None}


def zoekinstelling(q):
    """Voor n8n: drempel, maximum per ronde en zoekopdrachten per kop."""
    z = stand(q)["zoeken"]
    return {"stand": z["stand"], "drempel": z["drempel"], "max": z["max"], "zoekopdrachten": z["zoekopdrachten"]}


def automatisch_toegestaan(q):
    """Achtergrondtaken die veel kosten (zoals de maandaganalyse) slaan over als het maandbudget op is."""
    try:
        return not stand(q)["over_budget"]
    except Exception:  # noqa: BLE001
        return True


# ---------- de echte rekening ----------

def _get(params):
    url = API + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"x-api-key": SLEUTEL, "anthropic-version": "2023-06-01",
                                               "User-Agent": "Doellijn/1.0 (+https://zerohero.one)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def bedrag(r):
    """Anthropic geeft 'amount' als tekst in de kleinste munteenheid (dollarcenten)."""
    try:
        return float(r.get("amount") or 0) / 100
    except (TypeError, ValueError):
        return 0.0


def _soort(r):
    t = str(r.get("cost_type") or "").lower()
    o = str(r.get("description") or "").lower()
    if "search" in t or "search" in o:
        return "zoeken"
    if "code" in t:
        return "code"
    return "tokens"


def ophalen(q, dagen=40):
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dagen)).replace(hour=0, minute=0, second=0, microsecond=0)
    params = {"starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "group_by[]": ["workspace_id", "description"],
              "bucket_width": "1d", "limit": 31}
    rijen, pagina = [], None
    for _ in range(10):
        p = dict(params, page=pagina) if pagina else params
        r = _get(p)
        for emmer in r.get("data") or []:
            datum = dt.date.fromisoformat(str(emmer.get("starting_at"))[:10])
            for x in emmer.get("results") or []:
                rijen.append((datum, x.get("workspace_id") or "", (x.get("description") or x.get("cost_type") or "")[:300],
                              _soort(x), x.get("model"), bedrag(x)))
        if not r.get("has_more") or not r.get("next_page"):
            break
        pagina = r["next_page"]
    # optellen per sleutel (meerdere regels kunnen dezelfde omschrijving hebben)
    som = {}
    for datum, ws, oms, soort, model, d in rijen:
        k = (datum, ws, oms)
        if k in som:
            som[k][2] += d
        else:
            som[k] = [soort, model, d]
    for (datum, ws, oms), (soort, model, d) in som.items():
        q("""INSERT INTO claude_rekening (datum, werkruimte, omschrijving, soort, model, dollar, bijgewerkt)
             VALUES (%s, %s, %s, %s, %s, %s, now())
             ON CONFLICT (datum, werkruimte, omschrijving) DO UPDATE SET soort = EXCLUDED.soort, model = EXCLUDED.model,
               dollar = EXCLUDED.dollar, bijgewerkt = now() RETURNING 1""", (datum, ws, oms, soort, model, round(d, 6)))
    return len(som)


def _fout(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = (json.loads(e.read() or b"{}").get("error") or {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = ""
        uitleg = {401: "Anthropic weigert de admin-sleutel (401). Het moet een admin-sleutel zijn (begint met sk-ant-admin).",
                  403: "Geen toegang tot het kostenrapport (403). Alleen een admin-sleutel van de organisatie mag dit."}.get(e.code, f"Anthropic gaf fout {e.code}.")
        return (uitleg + (" " + detail if detail else ""))[:300]
    return ("Anthropic niet bereikbaar: " + str(e))[:300]


def ververs(q):
    if not actief():
        return False, "Geen ANTHROPIC_ADMIN_KEY ingesteld (deploy/anthropic_admin.sh)."
    try:
        n = ophalen(q)
        fout, melding = None, f"{n} kostenregels opgehaald."
    except Exception as e:  # noqa: BLE001
        fout = melding = _fout(e)
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('anthropic', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""", (fout is None, fout))
    return fout is None, melding


def status(q):
    try:
        return (q("SELECT * FROM koppelingen WHERE naam = 'anthropic'") or [None])[0]
    except Exception:  # noqa: BLE001
        return None


def _filter():
    return ("AND werkruimte = %s", (WERKRUIMTE,)) if WERKRUIMTE else ("", ())


def heeft_data(q):
    try:
        return bool(q("SELECT 1 AS x FROM claude_rekening LIMIT 1"))
    except Exception:  # noqa: BLE001
        return False


def som(q, van, tot):
    """Totaal tussen twee datums (van tot en met tot-1)."""
    f, p = _filter()
    return float(q(f"SELECT coalesce(sum(dollar), 0) AS d FROM claude_rekening WHERE datum >= %s AND datum < %s {f}", (van, tot) + p)[0]["d"])


def maandtotaal(q, maand_start):
    if not heeft_data(q):
        return None
    eind = (maand_start + dt.timedelta(days=32)).replace(day=1)
    return som(q, maand_start, eind)


def overzicht(q, dagen):
    """Wat de cockpit op het dashboard toont, of None als er nog geen rekening is opgehaald."""
    if not heeft_data(q):
        return None
    f, p = _filter()
    vandaag = dt.date.today()
    maand = vandaag.replace(day=1)
    vorige = (maand - dt.timedelta(days=1)).replace(day=1)
    laatste = q(f"SELECT max(datum) AS d, min(datum) AS e FROM claude_rekening WHERE dollar > 0 {f}", p)[0]
    # tempo: gemiddelde van de laatste 14 volle dagen waarover Anthropic al cijfers heeft
    eind = min(vandaag, (laatste["d"] or vandaag) + dt.timedelta(days=1))
    begin = max(eind - dt.timedelta(days=14), laatste["e"] or eind - dt.timedelta(days=14))
    tempo_dagen = max(1, (eind - begin).days)
    per_dag = som(q, begin, eind) / tempo_dagen
    van = vandaag - dt.timedelta(days=dagen - 1)
    per_model = q(f"""SELECT CASE WHEN soort = 'zoeken' THEN 'Webzoekopdrachten'
                                  WHEN soort = 'code' THEN 'Code uitvoeren'
                                  ELSE coalesce(nullif(regexp_replace(omschrijving, '\\s*(Usage)?\\s*-.*$', ''), ''), model, 'Overig') END AS naam,
                             sum(dollar) AS dollar
                      FROM claude_rekening WHERE datum >= %s {f} GROUP BY 1 HAVING sum(dollar) > 0.005 ORDER BY 2 DESC""", (van,) + p)
    dag = q(f"""SELECT d::date AS datum, coalesce(sum(r.dollar), 0) AS dollar
                FROM generate_series(%s::date, %s::date, interval '1 day') d
                LEFT JOIN claude_rekening r ON r.datum = d::date {f.replace('werkruimte', 'r.werkruimte')}
                GROUP BY 1 ORDER BY 1""", (max(van, vandaag - dt.timedelta(days=89)), vandaag) + p)
    dagen_maand = ((maand + dt.timedelta(days=32)).replace(day=1) - maand).days
    deze = som(q, maand, vandaag + dt.timedelta(days=1))
    resterend = dagen_maand - (eind - maand).days
    return {"deze_maand": deze, "vorige_maand": som(q, vorige, maand), "totaal": som(q, dt.date(2000, 1, 1), vandaag + dt.timedelta(days=1)),
            "sinds": laatste["e"], "tot": laatste["d"], "per_dag": per_dag, "tempo_dagen": tempo_dagen,
            "prognose_maand": deze + per_dag * max(0, resterend), "prognose_jaar": per_dag * 365,
            "periode": som(q, van, vandaag + dt.timedelta(days=1)), "per_model": per_model, "dag": dag,
            "status": status(q)}


_gestart = False


def start_achtergrond(q):
    global _gestart
    if _gestart or not actief() or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(45)
        while True:
            try:
                ververs(q)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=lus, name="anthropic-rekening", daemon=True).start()
