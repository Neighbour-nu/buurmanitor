"""Podcastcijfers uit de CSV-exports van Spotify for Creators (er is geen API).

Elk bestand wordt herkend aan de kolomkoppen, niet aan de bestandsnaam (die bevat de periode).
- Dagcijfers (pod_dag): afspelen en downloads, publiek, luistertijd, volgers, weergaven, nieuw/terugkerend.
- Afleveringen (pod_afleveringen): afspelen in de eerste 7 dagen met prestatie, voltooiingspercentage.
- Momentopnames (pod_momentopname): toplijsten, apps, leeftijd en geslacht, retentie; de laatste upload telt.
Volgers en afspelen gaan ook naar de tabel metingen, zodat het tabblad Bereik ze toont.
"""
import csv
import datetime as dt
import io
import json

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS pod_dag (
        datum date PRIMARY KEY, afspelen int, op_spotify int, andere int, publiek int, uren numeric, volgers int,
        weergaven int, nieuw int, terugkerend int, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS pod_afleveringen (
        titel text PRIMARY KEY, uri text, publicatie date, eerste7 int, prestatie text, voltooiing numeric,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS pod_momentopname (
        soort text PRIMARY KEY, periode text, data jsonb, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS pod_uploads (
        id serial PRIMARY KEY, moment timestamptz NOT NULL DEFAULT now(), bestanden int, regels int, tot date)""",
]

# Welke exports er zijn, met de kolom waaraan ze herkend worden (eerste kolommen, kleine letters).
SOORTEN = {
    "prestaties": ("Prestaties (afspelen, downloads, publiek)", ["datum", "keer afgespeeld en gedownload"]),
    "streams": ("Streams en downloads", ["datum", "streams en downloads"]),
    "betrokkenheid": ("Betrokkenheid (luistertijd, volgers)", ["datum", "kijk-/luistertijd (uur)"]),
    "weergaven": ("Weergaven per dag", ["datum", "weergaven"]),
    "segmenten": ("Publiek: nieuw en terugkerend", ["datum", "totaal", "terugkerend"]),
    "eerste7": ("Afleveringen: eerste 7 dagen", ["naam van aflevering", "publicatiedatum", "prestaties"]),
    "voltooiing": ("Afleveringen: voltooiing", ["titel van aflevering", "voltooiingspercentage (%)"]),
    "top_luistertijd": ("Topafleveringen op luistertijd", ["titel van aflevering", "kijk-/luistertijd (uur)"]),
    "top_weergaven": ("Topafleveringen op weergaven", ["titel van aflevering", "totaal aantal weergaven"]),
    "apps": ("Apps", ["app", "percentage"]),
    "leeftijd": ("Leeftijd en geslacht", ["leeftijd", "totaal percentage"]),
    "geslacht": ("Geslacht", ["geslacht", "percentage"]),
    "retentie": ("Retentie per week", ["start van week", "retentiepercentage (%)"]),
}


def _datum(v):
    v = (v or "").strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _getal(v, soort=int):
    v = (v or "").strip().replace("%", "")
    if v == "":
        return None
    try:
        f = float(v.replace(",", ".")) if v.count(",") == 1 and "." not in v else float(v)
    except ValueError:
        return None
    return int(round(f)) if soort is int else round(f, 2)


def herken(kop):
    k = [c.strip().lower().lstrip("﻿") for c in kop]
    for soort, (_label, verwacht) in SOORTEN.items():
        if k[:len(verwacht)] == verwacht:
            return soort
    return None


def _upsert_dag(q, datum, **velden):
    velden = {k: v for k, v in velden.items() if v is not None}
    if not datum or not velden:
        return
    kol = ", ".join(velden)
    plek = ", ".join(["%s"] * len(velden))
    upd = ", ".join(f"{k} = EXCLUDED.{k}" for k in velden)
    q(f"""INSERT INTO pod_dag (datum, {kol}, bijgewerkt) VALUES (%s, {plek}, now())
          ON CONFLICT (datum) DO UPDATE SET {upd}, bijgewerkt = now() RETURNING 1""", (datum, *velden.values()))


def _meting(q, sleutel, datum, waarde):
    if waarde is None or not datum:
        return
    q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES ('podcast', %s, %s, %s, 'export')
         ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'export', bijgewerkt = now()
         RETURNING 1""", (sleutel, datum, waarde))


def verwerk(q, naam, inhoud):
    """Verwerkt één CSV-bestand. Geeft (soort, aantal regels, laatste datum) terug; soort None als onbekend."""
    tekst = inhoud.decode("utf-8-sig", errors="replace")
    rijen = list(csv.reader(io.StringIO(tekst)))
    if len(rijen) < 2:
        return None, 0, None
    soort = herken(rijen[0])
    data = [r for r in rijen[1:] if any(c.strip() for c in r)]
    laatste = None
    periode = naam.rsplit("_", 1)[-1].replace(".csv", "") if "_" in naam else ""

    def d(r):
        nonlocal laatste
        x = _datum(r[0])
        if x and (laatste is None or x > laatste):
            laatste = x
        return x

    if soort == "prestaties":
        for r in data:
            x = d(r)
            _upsert_dag(q, x, afspelen=_getal(r[1]), op_spotify=_getal(r[2]), andere=_getal(r[3]) or 0, publiek=_getal(r[4]))
            _meting(q, "streams", x, _getal(r[1]))
    elif soort == "streams":
        for r in data:
            x = d(r)
            bestaand = q("SELECT afspelen FROM pod_dag WHERE datum = %s", (x,)) if x else []
            if not bestaand or bestaand[0]["afspelen"] is None:
                _upsert_dag(q, x, afspelen=_getal(r[1]))
                _meting(q, "streams", x, _getal(r[1]))
    elif soort == "betrokkenheid":
        for r in data:
            x = d(r)
            volgers = _getal(r[4]) if len(r) > 4 else None
            _upsert_dag(q, x, uren=_getal(r[1], float), volgers=volgers)
            _meting(q, "volgers", x, volgers)
            _meting(q, "uren", x, _getal(r[1], float))
    elif soort == "weergaven":
        for r in data:
            _upsert_dag(q, d(r), weergaven=_getal(r[1]))
    elif soort == "segmenten":
        for r in data:
            _upsert_dag(q, d(r), terugkerend=_getal(r[2]), nieuw=_getal(r[3]))
    elif soort == "eerste7":
        for r in data:
            q("""INSERT INTO pod_afleveringen (titel, publicatie, eerste7, prestatie, bijgewerkt) VALUES (%s, %s, %s, %s, now())
                 ON CONFLICT (titel) DO UPDATE SET publicatie = coalesce(EXCLUDED.publicatie, pod_afleveringen.publicatie),
                   eerste7 = EXCLUDED.eerste7, prestatie = EXCLUDED.prestatie, bijgewerkt = now() RETURNING 1""",
              (r[0].strip()[:400], _datum(r[1]), _getal(r[3]), r[2].strip()[:60]))
    elif soort == "voltooiing":
        for r in data:
            q("""INSERT INTO pod_afleveringen (titel, voltooiing, publicatie, uri, bijgewerkt) VALUES (%s, %s, %s, %s, now())
                 ON CONFLICT (titel) DO UPDATE SET voltooiing = EXCLUDED.voltooiing, uri = coalesce(EXCLUDED.uri, pod_afleveringen.uri),
                   publicatie = coalesce(pod_afleveringen.publicatie, EXCLUDED.publicatie), bijgewerkt = now() RETURNING 1""",
              (r[0].strip()[:400], _getal(r[1], float), _datum(r[2]), (r[3].strip() if len(r) > 3 else None)))
    elif soort:
        kop = [c.strip().lstrip("﻿") for c in rijen[0]]
        q("""INSERT INTO pod_momentopname (soort, periode, data, bijgewerkt) VALUES (%s, %s, %s::jsonb, now())
             ON CONFLICT (soort) DO UPDATE SET periode = EXCLUDED.periode, data = EXCLUDED.data, bijgewerkt = now() RETURNING 1""",
          (soort, periode.replace("--", " t/m "), json.dumps({"kop": kop, "rijen": data[:50]}, ensure_ascii=False)))
    return soort, len(data), laatste


def overzicht(q, dagen):
    """Telt vanaf de laatste dag in de export terug (de export loopt achter op vandaag)."""
    tot = q("SELECT max(datum) AS t FROM pod_dag WHERE afspelen IS NOT NULL")[0]["t"]
    som = {"nu": 0, "toen": 0, "uren": 0, "weergaven": 0, "publiek": 0, "ooit": 0, "tot": tot, "vanaf": None}
    if tot:
        som = q("""SELECT coalesce(sum(afspelen) FILTER (WHERE datum > %(t)s::date - %(d)s), 0) AS nu,
                          coalesce(sum(afspelen) FILTER (WHERE datum > %(t)s::date - 2 * %(d)s AND datum <= %(t)s::date - %(d)s), 0) AS toen,
                          coalesce(sum(uren) FILTER (WHERE datum > %(t)s::date - %(d)s), 0) AS uren,
                          coalesce(sum(weergaven) FILTER (WHERE datum > %(t)s::date - %(d)s), 0) AS weergaven,
                          coalesce(sum(nieuw) FILTER (WHERE datum > %(t)s::date - %(d)s), 0) AS nieuw,
                          coalesce(sum(afspelen), 0) AS ooit, min(datum) FILTER (WHERE afspelen IS NOT NULL) AS vanaf
                   FROM pod_dag""", {"t": tot, "d": dagen})[0]
        som["tot"] = tot
    maanden = q("""SELECT date_trunc('month', datum)::date AS maand, coalesce(sum(afspelen), 0) AS afspelen
                   FROM pod_dag WHERE afspelen IS NOT NULL GROUP BY 1 ORDER BY 1""")
    volgers = q("SELECT volgers, datum FROM pod_dag WHERE volgers IS NOT NULL ORDER BY datum DESC LIMIT 1")
    volgers_eerder = None
    if volgers:
        r = q("""SELECT volgers FROM pod_dag WHERE volgers IS NOT NULL AND datum <= %s::date - %s
                 ORDER BY datum DESC LIMIT 1""", (volgers[0]["datum"], dagen))
        volgers_eerder = r[0]["volgers"] if r else None
    afleveringen = q("""SELECT * FROM pod_afleveringen ORDER BY publicatie DESC NULLS LAST, titel LIMIT 60""")
    gem7 = q("SELECT round(avg(eerste7)) AS g FROM pod_afleveringen WHERE eerste7 IS NOT NULL")[0]["g"]
    snap = {r["soort"]: r for r in q("SELECT * FROM pod_momentopname")}
    for r in snap.values():
        if isinstance(r["data"], str):
            r["data"] = json.loads(r["data"])
    laatste = (q("SELECT * FROM pod_uploads ORDER BY moment DESC LIMIT 1") or [None])[0]
    return {"som": som, "maanden": maanden, "volgers": volgers[0] if volgers else None, "volgers_eerder": volgers_eerder,
            "afleveringen": afleveringen, "gem7": gem7, "snap": snap, "laatste_upload": laatste,
            "heeft_data": bool(tot or afleveringen or snap)}


def log_upload(q, bestanden, regels, tot):
    q("INSERT INTO pod_uploads (bestanden, regels, tot) VALUES (%s, %s, %s) RETURNING 1", (bestanden, regels, tot))


def upload_nodig(q):
    """True als de laatste upload meer dan een maand geleden is (of er nog nooit een was)."""
    r = q("SELECT max(moment) AS m FROM pod_uploads")[0]["m"]
    return r is None or (dt.datetime.now(dt.timezone.utc) - r).days >= 30


SORTERINGEN = {"nieuw": "Nieuwste eerst", "eerste7": "Beste eerste week", "voltooiing": "Best uitgeluisterd"}
WEEKDAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _int(v):
    return int(v) if v is not None else None


def detail(q, dagen, sorteer="nieuw"):
    """Alles voor de podcastpagina: overzicht plus dagreeks, jaren, weekdagen, retentie en inzichten."""
    o = overzicht(q, dagen)
    tot = o["som"]["tot"]
    o["dagreeks"] = q("""SELECT datum, coalesce(afspelen, 0) AS afspelen, coalesce(uren, 0) AS uren,
                                coalesce(weergaven, 0) AS weergaven, nieuw, terugkerend
                         FROM pod_dag WHERE datum > %s::date - %s AND datum <= %s ORDER BY datum""",
                      (tot, dagen, tot)) if tot else []
    o["jaren"] = q("""SELECT extract(year FROM datum)::int AS jaar, coalesce(sum(afspelen), 0)::int AS afspelen,
                             count(DISTINCT date_trunc('month', datum)) AS maanden,
                             sum(uren) AS uren
                      FROM pod_dag WHERE afspelen IS NOT NULL GROUP BY 1 ORDER BY 1""")
    o["weekdagen"] = q("""SELECT extract(isodow FROM datum)::int AS dag, round(avg(afspelen), 1) AS gem
                          FROM pod_dag WHERE afspelen IS NOT NULL AND datum > %s::date - 365
                          GROUP BY 1 ORDER BY 1""", (tot,)) if tot else []
    volgorde = {"eerste7": "eerste7 DESC NULLS LAST", "voltooiing": "voltooiing DESC NULLS LAST"}.get(sorteer, "publicatie DESC NULLS LAST")
    o["afleveringen"] = q(f"SELECT * FROM pod_afleveringen ORDER BY {volgorde}, titel")
    o["sorteer"] = sorteer if sorteer in SORTERINGEN else "nieuw"
    o["inzichten"] = _inzichten(q, o, dagen)
    return o


def _kort(t, n=90):
    t = (t or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(" ,:;-") + "…"


def _inzichten(q, o, dagen):
    uit = []
    som, tot = o["som"], o["som"]["tot"]
    if not tot:
        return uit
    # Trend: laatste 90 dagen tegen dezelfde 90 dagen een jaar eerder.
    t = q("""SELECT coalesce(sum(afspelen) FILTER (WHERE datum > %(t)s::date - 90), 0) AS nu,
                    coalesce(sum(afspelen) FILTER (WHERE datum > %(t)s::date - 455 AND datum <= %(t)s::date - 365), 0) AS vorig,
                    bool_or(datum <= %(t)s::date - 365) AS lang_genoeg
             FROM pod_dag""", {"t": tot})[0]
    if t["lang_genoeg"]:
        nu, vorig = int(t["nu"]), int(t["vorig"])
        if vorig:
            pct = round((nu - vorig) / vorig * 100)
            uit.append(f"De afgelopen 90 dagen: {nu} keer afgespeeld, tegen {vorig} in dezelfde 90 dagen een jaar eerder "
                       f"({'+' if pct >= 0 else ''}{pct}%).")
        else:
            uit.append(f"De afgelopen 90 dagen: {nu} keer afgespeeld (een jaar eerder: 0).")
    nieuwste = q("SELECT titel, publicatie FROM pod_afleveringen WHERE publicatie IS NOT NULL ORDER BY publicatie DESC LIMIT 1")
    if nieuwste:
        dagen_stil = (dt.date.today() - nieuwste[0]["publicatie"]).days
        uit.append(f"De laatste aflevering verscheen {dagen_stil} dagen geleden ({nieuwste[0]['publicatie'].strftime('%d-%m-%Y')}). "
                   + ("Zonder nieuwe afleveringen zakt het afspelen meestal weg; alleen gezochte namen blijven luisteraars trekken."
                      if dagen_stil > 60 else ""))
    piek = q("""SELECT p.datum, p.afspelen, (SELECT a.titel FROM pod_afleveringen a WHERE a.publicatie BETWEEN p.datum - 3 AND p.datum
                                              ORDER BY a.publicatie DESC LIMIT 1) AS titel
                FROM pod_dag p WHERE p.afspelen IS NOT NULL ORDER BY p.afspelen DESC, p.datum DESC LIMIT 1""")
    if piek and piek[0]["afspelen"]:
        p = piek[0]
        uit.append(f"Drukste dag ooit: {p['datum'].strftime('%d-%m-%Y')} met {p['afspelen']} keer afgespeeld"
                   + (f", vlak na “{_kort(p['titel'])}”." if p["titel"] else "."))
    best = q("SELECT titel, eerste7 FROM pod_afleveringen WHERE eerste7 IS NOT NULL ORDER BY eerste7 DESC LIMIT 1")
    if best and o["gem7"]:
        keer = round(best[0]["eerste7"] / float(o["gem7"]), 1)
        uit.append(f"Beste eerste week: “{_kort(best[0]['titel'])}” met {best[0]['eerste7']} keer, "
                   f"{str(keer).replace('.', ',')} keer zoveel als gemiddeld ({int(o['gem7'])}).")
    vol = q("SELECT titel, voltooiing FROM pod_afleveringen WHERE voltooiing IS NOT NULL ORDER BY voltooiing DESC LIMIT 1")
    if vol:
        uit.append(f"Best uitgeluisterd: “{_kort(vol[0]['titel'])}” ({int(vol[0]['voltooiing'])}% van de aflevering).")
    tw = o["snap"].get("top_weergaven")
    if tw:
        rijen = tw["data"]["rijen"]
        totaal = sum(_getal(r[1]) or 0 for r in rijen)
        zoeken = sum(_getal(r[3]) or 0 for r in rijen)
        if totaal:
            uit.append(f"{round(zoeken / totaal * 100)}% van de keren dat Spotify een aflevering liet zien, kwam via zoeken "
                       f"(periode {tw['periode']}). Bekende namen in de titel worden gevonden.")
    if som["weergaven"] and som["nu"]:
        uit.append(f"Van getoond naar afgespeeld: {som['nu']} keer afgespeeld op {som['weergaven']} keer getoond in Spotify "
                   f"in deze periode ({round(int(som['nu']) / int(som['weergaven']) * 100)}%).")
    if o["weekdagen"]:
        top = max(o["weekdagen"], key=lambda r: r["gem"] or 0)
        if top["gem"]:
            uit.append(f"Over het afgelopen jaar wordt er het meest geluisterd op {WEEKDAGEN[top['dag'] - 1]} "
                       f"(gemiddeld {str(top['gem']).replace('.', ',')} keer).")
    return uit
