"""LinkedIn-statistieken uit de exports van LinkedIn (de API is alleen voor goedgekeurde partners).

Twee bronnen, strikt gescheiden:
- Persoonlijk profiel (Martijn Buurman): AggregateAnalytics_....xlsx uit Analyse van je profiel. Tabbladen ONTDEKKING,
  ENGAGEMENT (impressies en interacties per dag), TOPBIJDRAGEN, VOLGERS (totaal en nieuwe volgers per dag) en twee
  tabbladen met demografische gegevens. Tabellen lp_dag en lp_posts, metingen onder kanaal 'linkedin'.
- Bedrijfspagina ZEROHERO: drie .xls-exports (content, volgers, bezoekers). Tabellen li_dag en li_posts, metingen onder
  kanaal 'linkedin_pagina'. Verdelingen van beide bronnen staan in li_momentopname (sleutel begint met pagina- of profiel:).

LinkedIn levert per onderdeel een .xls met meerdere tabbladen:
- Content (zerohero1one_content_...): weergaven, klikken, reacties, commentaren en reposts per dag, plus alle bijdragen.
- Volgers (..._followers_...): nieuwe volgers per dag, plus volgers naar locatie, functie, branche, senioriteit, bedrijfsgrootte.
- Bezoekers (..._visitors_...): paginaweergaven en unieke bezoekers per dag, plus dezelfde verdelingen.
Tabbladen worden herkend aan de kolomkoppen (Nederlands of Engels). Datums in de export zijn maand/dag/jaar.
"""
import csv
import datetime as dt
import io
import json

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS li_dag (
        datum date PRIMARY KEY, weergaven int, weergaven_spontaan int, weergaven_gesponsord int, uniek int,
        klikken int, reacties int, commentaren int, reposts int, nieuwe_volgers int, paginaweergaven int, bezoekers int,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS li_posts (
        link text PRIMARY KEY, titel text, soort text, door text, aangemaakt date, weergaven int, klikken int,
        ctr numeric, reacties int, commentaren int, reposts int, gevolgd int, interactie numeric,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS li_momentopname (
        soort text PRIMARY KEY, titel text, periode text, data jsonb, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS li_uploads (
        id serial PRIMARY KEY, moment timestamptz NOT NULL DEFAULT now(), bestanden int, regels int, tot date)""",
    "ALTER TABLE li_uploads ADD COLUMN IF NOT EXISTS export text",
    """CREATE TABLE IF NOT EXISTS lp_dag (
        datum date PRIMARY KEY, impressies int, interacties int, nieuwe_volgers int, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS lp_posts (
        url text PRIMARY KEY, publicatie date, interacties int, impressies int, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # De eerste versie zette de bedrijfspagina onder kanaal 'linkedin' met sleutels pagina_*; die horen bij linkedin_pagina.
    """UPDATE metingen SET kanaal = 'linkedin_pagina', sleutel = substr(sleutel, 8)
       WHERE kanaal = 'linkedin' AND sleutel LIKE 'pagina\\_%'
       AND NOT EXISTS (SELECT 1 FROM metingen m2 WHERE m2.kanaal = 'linkedin_pagina' AND m2.sleutel = substr(metingen.sleutel, 8)
                       AND m2.datum = metingen.datum)""",
    "DELETE FROM metingen WHERE kanaal = 'linkedin' AND sleutel LIKE 'pagina\\_%'",
    """UPDATE li_momentopname SET soort = 'pagina-' || soort WHERE soort NOT LIKE 'pagina-%' AND soort NOT LIKE 'profiel:%'""",
]

# De vier exports, elk met een eigen uploadveld. De volgorde is die van de uploadpagina.
EXPORTS = {
    "profiel": {"naam": "Profiel: analyse-export", "groep": "profiel",
                "bestand": "AggregateAnalytics_Martijn_Buurman_....xlsx",
                "waar": "Op je profiel: Analyse, dan Impressies van bijdragen (of Volgers). Kies Afgelopen 365 dagen en klik op Exporteren. "
                        "LinkedIn zet impressies, interacties, topbijdragen, volgers en demografie in één bestand; "
                        "vanaf de pagina Volgers krijg je hetzelfde bestand, één keer uploaden is genoeg."},
    "pagina_content": {"naam": "Bedrijfspagina: Content", "groep": "pagina", "bestand": "zerohero1one_content_....xls",
                       "waar": "Beheerpagina ZEROHERO, Analyse, Content. Kies Afgelopen 365 dagen en klik op Exporteren."},
    "pagina_volgers": {"naam": "Bedrijfspagina: Volgers", "groep": "pagina", "bestand": "zerohero1one_followers_....xls",
                       "waar": "Beheerpagina ZEROHERO, Analyse, Volgers. Kies Afgelopen 365 dagen en klik op Exporteren."},
    "pagina_bezoekers": {"naam": "Bedrijfspagina: Bezoekers", "groep": "pagina", "bestand": "zerohero1one_visitors_....xls",
                         "waar": "Beheerpagina ZEROHERO, Analyse, Bezoekers. Kies Afgelopen 365 dagen en klik op Exporteren."},
}

# Dagkolommen: veld -> mogelijke kolomkoppen (kleine letters), Nederlands en Engels.
DAGKOLOMMEN = {
    "weergaven": ["weergaven (totaal)", "impressions (total)"],
    "weergaven_spontaan": ["weergaven (spontaan)", "impressions (organic)"],
    "weergaven_gesponsord": ["weergaven (gesponsord)", "impressions (sponsored)"],
    "uniek": ["unieke weergaven (spontaan)", "unique impressions (organic)"],
    "klikken": ["klikken (totaal)", "clicks (total)"],
    "reacties": ["reacties (totaal)", "reactions (total)"],
    "commentaren": ["commentaren (totaal)", "comments (total)"],
    "reposts": ["reposts (totaal)", "reposts (total)"],
    "nieuwe_volgers": ["totaal aantal volgers", "total followers", "totaal aantal nieuwe volgers", "total new followers"],
    "paginaweergaven": ["totaal aantal paginaweergaven (totaal)", "total page views (total)"],
    "bezoekers": ["totaal aantal unieke bezoekers (totaal)", "total unique visitors (total)"],
}
POSTKOLOMMEN = {
    "titel": ["titel bijdrage", "post title"],
    "link": ["link plaatsen", "post link"],
    "soort": ["soort bijdrage", "post type"],
    "door": ["geplaatst door", "posted by"],
    "aangemaakt": ["aangemaakt", "created date"],
    "weergaven": ["weergaven", "impressions"],
    "klikken": ["klikken", "clicks"],
    "ctr": ["doorklikfrequentie (ctr)", "click through rate (ctr)"],
    "reacties": ["interessant", "likes", "reacties", "reactions"],
    "commentaren": ["commentaren", "comments"],
    "reposts": ["reposts"],
    "gevolgd": ["gevolgd", "follows"],
    "interactie": ["interactiepercentage", "engagement rate"],
}
SOORTEN = {
    "content": "Content: weergaven en interacties per dag",
    "posts": "Content: alle bijdragen",
    "volgers": "Volgers: nieuwe volgers per dag",
    "bezoekers": "Bezoekers: paginaweergaven per dag",
    "verdeling": "Verdeling (locatie, functie, branche en dergelijke)",
}


def _datum(v):
    if isinstance(v, (int, float)) and v > 20000:  # Excel-datumgetal
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    v = str(v or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _getal(v, soort=int):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        try:
            f = float(str(v).strip().replace("%", "").replace(",", "."))
        except ValueError:
            return None
    return int(round(f)) if soort is int else round(f, 6)


def _bladen(naam, inhoud):
    """Geeft [(bladnaam, rijen)] terug voor .xls, .xlsx-loos en .csv."""
    if naam.lower().endswith(".csv"):
        tekst = inhoud.decode("utf-8-sig", errors="replace")
        return [(naam, list(csv.reader(io.StringIO(tekst))))]
    import xlrd  # alleen nodig voor .xls
    boek = xlrd.open_workbook(file_contents=inhoud)
    return [(b.name, [b.row_values(r) for r in range(b.nrows)]) for b in boek.sheets()]


def _koprij(rijen):
    """Index van de rij met kolomkoppen: de eerste rij met minstens twee gevulde cellen na een eventuele uitlegregel."""
    for i, r in enumerate(rijen[:6]):
        gevuld = [c for c in r if str(c).strip()]
        if len(gevuld) >= 2:
            return i
    return None


def _kolommen(kop, kaart):
    k = [str(c).strip().lower() for c in kop]
    uit = {}
    for veld, namen in kaart.items():
        for naam in namen:
            if naam in k:
                uit[veld] = k.index(naam)
                break
    return uit


def _upsert_dag(q, datum, velden):
    velden = {k: v for k, v in velden.items() if v is not None}
    if not datum or not velden:
        return
    kol = ", ".join(velden)
    upd = ", ".join(f"{k} = EXCLUDED.{k}" for k in velden)
    q(f"""INSERT INTO li_dag (datum, {kol}, bijgewerkt) VALUES (%s, {", ".join(["%s"] * len(velden))}, now())
          ON CONFLICT (datum) DO UPDATE SET {upd}, bijgewerkt = now() RETURNING 1""", (datum, *velden.values()))


def _meting(q, sleutel, datum, waarde, kanaal="linkedin_pagina"):
    if waarde is None or not datum:
        return
    q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES (%s, %s, %s, %s, 'export')
         ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'export', bijgewerkt = now()
         RETURNING 1""", (kanaal, sleutel, datum, waarde))


def _bron(naam):
    n = naam.lower()
    return "volgers" if "follower" in n or "volger" in n else "bezoekers" if "visitor" in n or "bezoeker" in n else "content"


def soort_export(naam, bladen_namen):
    """Welke van de vier exports dit is, op grond van de tabbladen (en als laatste redmiddel de bestandsnaam)."""
    b = [x.lower() for x in bladen_namen]
    if "engagement" in b or "ontdekking" in b:
        return "profiel"
    if "nieuwe volgers" in b or "new followers" in b:
        return "pagina_volgers"
    if any("bezoekers" in x or "visitor" in x for x in b):
        return "pagina_bezoekers"
    if "statistieken" in b or "alle bijdragen" in b or "metrics" in b or "all posts" in b:
        return "pagina_content"
    return {"volgers": "pagina_volgers", "bezoekers": "pagina_bezoekers"}.get(_bron(naam), "pagina_content")


def verwerk(q, naam, inhoud):
    """Verwerkt één export. Geeft (export, [(soort, bladnaam, regels, laatste datum)]) terug; lijst leeg als niets herkend."""
    if naam.lower().endswith(".xlsx"):
        return "profiel", verwerk_profiel(q, inhoud)
    bladen = _bladen(naam, inhoud)
    export = soort_export(naam, [b for b, _ in bladen])
    return export, _verwerk_pagina(q, naam, bladen)


def _verwerk_pagina(q, naam, bladen):
    uit = []
    bron = _bron(naam)
    for blad, rijen in bladen:
        i = _koprij(rijen)
        if i is None:
            continue
        kop = rijen[i]
        data = [r for r in rijen[i + 1:] if any(str(c).strip() for c in r)]
        eerste = str(kop[0]).strip().lower()
        if eerste in ("datum", "date"):
            kol = _kolommen(kop, DAGKOLOMMEN)
            if not kol:
                continue
            laatste = None
            for r in data:
                d = _datum(r[0])
                if not d:
                    continue
                laatste = d if laatste is None or d > laatste else laatste
                w = {veld: _getal(r[c]) for veld, c in kol.items() if c < len(r)}
                _upsert_dag(q, d, w)
                if w.get("weergaven") is not None:
                    _meting(q, "weergaven", d, w["weergaven"])
                inter = [w.get(x) for x in ("klikken", "reacties", "commentaren", "reposts") if w.get(x) is not None]
                if inter:
                    _meting(q, "interacties", d, sum(inter))
                if w.get("nieuwe_volgers") is not None:
                    _meting(q, "nieuwe_volgers", d, w["nieuwe_volgers"])
                if w.get("paginaweergaven") is not None:
                    _meting(q, "bezoeken", d, w["paginaweergaven"])
            soort = "content" if "weergaven" in kol or "klikken" in kol else ("volgers" if "nieuwe_volgers" in kol else "bezoekers")
            uit.append((soort, blad, len(data), laatste))
        elif eerste in ("titel bijdrage", "post title"):
            kol = _kolommen(kop, POSTKOLOMMEN)
            if "link" not in kol:
                continue
            for r in data:
                g = lambda veld, soort=int: _getal(r[kol[veld]], soort) if veld in kol and kol[veld] < len(r) else None  # noqa: E731
                t = lambda veld: str(r[kol[veld]]).strip() if veld in kol and kol[veld] < len(r) else None  # noqa: E731
                link = t("link")
                if not link:
                    continue
                q("""INSERT INTO li_posts (link, titel, soort, door, aangemaakt, weergaven, klikken, ctr, reacties, commentaren,
                                           reposts, gevolgd, interactie, bijgewerkt)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                     ON CONFLICT (link) DO UPDATE SET titel = EXCLUDED.titel, soort = EXCLUDED.soort, door = EXCLUDED.door,
                       aangemaakt = EXCLUDED.aangemaakt, weergaven = EXCLUDED.weergaven, klikken = EXCLUDED.klikken, ctr = EXCLUDED.ctr,
                       reacties = EXCLUDED.reacties, commentaren = EXCLUDED.commentaren, reposts = EXCLUDED.reposts,
                       gevolgd = EXCLUDED.gevolgd, interactie = EXCLUDED.interactie, bijgewerkt = now() RETURNING 1""",
                  (link[:500], (t("titel") or "")[:500], t("soort"), t("door"), _datum(r[kol["aangemaakt"]]) if "aangemaakt" in kol else None,
                   g("weergaven"), g("klikken"), g("ctr", float), g("reacties"), g("commentaren"), g("reposts"), g("gevolgd"),
                   g("interactie", float)))
            uit.append(("posts", blad, len(data), None))
        elif len([c for c in kop if str(c).strip()]) >= 2:
            # Verdeling: eerste kolom een categorie, daarna aantallen. Hoogste aantallen eerst, top 25.
            rijen_v = []
            for r in data:
                waarde = _getal(r[1]) if len(r) > 1 else None
                if str(r[0]).strip() and waarde is not None:
                    rijen_v.append([str(r[0]).strip(), waarde])
            if not rijen_v:
                continue
            rijen_v.sort(key=lambda x: -x[1])
            sleutel = f"pagina-{bron}:{blad}"[:120]
            q("""INSERT INTO li_momentopname (soort, titel, periode, data, bijgewerkt) VALUES (%s, %s, %s, %s::jsonb, now())
                 ON CONFLICT (soort) DO UPDATE SET titel = EXCLUDED.titel, data = EXCLUDED.data, bijgewerkt = now() RETURNING 1""",
              (sleutel, blad, None, json.dumps({"kop": [str(c) for c in kop[:2]], "rijen": rijen_v[:25],
                                                "totaal": sum(x[1] for x in rijen_v)}, ensure_ascii=False)))
            uit.append(("verdeling", blad, len(rijen_v), None))
    return uit


def log_upload(q, export, regels, tot):
    q("INSERT INTO li_uploads (bestanden, regels, tot, export) VALUES (1, %s, %s, %s) RETURNING 1", (regels, tot, export))


def stand_exports(q):
    """Per export: laatste upload en tot welke datum de cijfers lopen, plus of hij aan de beurt is (30 dagen)."""
    rijen = {r["export"]: r for r in q("""SELECT DISTINCT ON (export) export, moment, tot FROM li_uploads
                                          WHERE export IS NOT NULL ORDER BY export, moment DESC""")}
    nu = dt.datetime.now(dt.timezone.utc)
    uit = []
    for sleutel, e in EXPORTS.items():
        r = rijen.get(sleutel)
        uit.append({"sleutel": sleutel, **e, "moment": r["moment"] if r else None, "tot": r["tot"] if r else None,
                    "nodig": r is None or (nu - r["moment"]).days >= 30})
    return uit


def laatste_upload(q, groep=None):
    if groep:
        return (q("SELECT * FROM li_uploads WHERE export LIKE %s ORDER BY moment DESC LIMIT 1", (groep + "%",)) or [None])[0]
    return (q("SELECT * FROM li_uploads ORDER BY moment DESC LIMIT 1") or [None])[0]


def upload_nodig(q, groep=None):
    return any(e["nodig"] for e in stand_exports(q) if groep is None or e["groep"] == groep)


def _kort(t, n=90):
    t = (t or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(" ,:;-") + "…"


def _n(v):
    return f"{int(v or 0):,}".replace(",", ".")


def detail_pagina(q, dagen):
    """Alles voor de pagina van de bedrijfspagina, geteld tot en met de laatste dag in de export."""
    tot = q("SELECT max(datum) AS t FROM li_dag")[0]["t"]
    o = {"tot": tot, "laatste_upload": laatste_upload(q, "pagina"), "heeft_data": bool(tot)}
    if not tot:
        o["posts"] = q("SELECT * FROM li_posts ORDER BY aangemaakt DESC NULLS LAST")
        o["heeft_data"] = bool(o["posts"])
        o.update(som=None, toen=None, dagreeks=[], maanden=[], verdelingen=[], inzichten=[], vanaf=None)
        return o
    velden = ["weergaven", "weergaven_spontaan", "weergaven_gesponsord", "uniek", "klikken", "reacties", "commentaren",
              "reposts", "nieuwe_volgers", "paginaweergaven", "bezoekers"]
    sel = ", ".join(f"sum({v}) AS {v}" for v in velden)
    o["som"] = q(f"SELECT {sel} FROM li_dag WHERE datum > %s::date - %s AND datum <= %s", (tot, dagen, tot))[0]
    o["toen"] = q(f"SELECT {sel} FROM li_dag WHERE datum > %s::date - 2 * %s AND datum <= %s::date - %s", (tot, dagen, tot, dagen))[0]
    o["ooit"] = q(f"SELECT {sel}, min(datum) AS vanaf FROM li_dag")[0]
    o["vanaf"] = o["ooit"]["vanaf"]
    for s in (o["som"], o["toen"], o["ooit"]):
        s["interacties"] = sum(int(s[x] or 0) for x in ("klikken", "reacties", "commentaren", "reposts"))
    o["dagreeks"] = q("""SELECT * FROM li_dag WHERE datum > %s::date - %s AND datum <= %s ORDER BY datum""", (tot, dagen, tot))
    o["maanden"] = q("""SELECT date_trunc('month', datum)::date AS maand, coalesce(sum(weergaven), 0)::int AS weergaven,
                               coalesce(sum(weergaven_spontaan), 0)::int AS spontaan, coalesce(sum(weergaven_gesponsord), 0)::int AS gesponsord,
                               coalesce(sum(klikken), 0)::int AS klikken, coalesce(sum(nieuwe_volgers), 0)::int AS volgers,
                               coalesce(sum(paginaweergaven), 0)::int AS bezoeken
                        FROM li_dag GROUP BY 1 ORDER BY 1""")
    o["heeft_volgers"] = o["ooit"]["nieuwe_volgers"] is not None
    o["heeft_bezoekers"] = o["ooit"]["paginaweergaven"] is not None
    o["posts"] = q("SELECT * FROM li_posts ORDER BY aangemaakt DESC NULLS LAST")
    verd = q("SELECT * FROM li_momentopname WHERE soort LIKE 'pagina-%%' ORDER BY soort")
    for r in verd:
        if isinstance(r["data"], str):
            r["data"] = json.loads(r["data"])
    o["verdelingen"] = verd
    o["inzichten"] = _inzichten(q, o, dagen)
    return o


def _inzichten(q, o, dagen):
    uit, ooit = [], o["ooit"]
    w, sp, ge = int(ooit["weergaven"] or 0), int(ooit["weergaven_spontaan"] or 0), int(ooit["weergaven_gesponsord"] or 0)
    if w:
        uit.append(f"Sinds {o['vanaf'].strftime('%d-%m-%Y')}: {_n(w)} weergaven, waarvan {_n(ge)} gesponsord "
                   f"({round(ge / w * 100)}%) en {_n(sp)} spontaan.")
    kl_sp = q("SELECT sum(klikken) AS k FROM li_dag WHERE weergaven_gesponsord = 0 OR weergaven_gesponsord IS NULL")[0]["k"]
    kl_ge = q("SELECT sum(klikken) AS k FROM li_dag WHERE weergaven_gesponsord > 0")[0]["k"]
    w_ge_dagen = q("SELECT sum(weergaven) AS w FROM li_dag WHERE weergaven_gesponsord > 0")[0]["w"]
    w_sp_dagen = q("SELECT sum(weergaven) AS w FROM li_dag WHERE weergaven_gesponsord = 0 OR weergaven_gesponsord IS NULL")[0]["w"]
    if w_ge_dagen and w_sp_dagen and kl_ge is not None and kl_sp is not None:
        p_ge = round(int(kl_ge) / int(w_ge_dagen) * 100, 1)
        p_sp = round(int(kl_sp) / int(w_sp_dagen) * 100, 1)
        uit.append(f"Doorklikken: {str(p_ge).replace('.', ',')}% op dagen met gesponsorde posts, "
                   f"{str(p_sp).replace('.', ',')}% op dagen met alleen spontane weergaven. "
                   + ("Spontane posts worden dus naar verhouding vaker aangeklikt." if p_sp > p_ge else ""))
    camp = q("""SELECT min(datum) AS van, max(datum) AS tot, sum(weergaven_gesponsord) AS w FROM li_dag WHERE weergaven_gesponsord > 0""")[0]
    if camp["w"]:
        uit.append(f"Gesponsorde weergaven liepen van {camp['van'].strftime('%d-%m-%Y')} tot {camp['tot'].strftime('%d-%m-%Y')}.")
    top = q("SELECT datum, weergaven, klikken FROM li_dag WHERE weergaven > 0 ORDER BY weergaven DESC LIMIT 1")
    if top:
        uit.append(f"Drukste dag: {top[0]['datum'].strftime('%d-%m-%Y')} met {_n(top[0]['weergaven'])} weergaven en {top[0]['klikken'] or 0} klikken.")
    stil = q("""SELECT count(*) AS n FROM li_dag WHERE datum > %s::date - 90 AND coalesce(weergaven_spontaan, weergaven, 0) = 0""", (o["tot"],))[0]["n"]
    uit.append(f"In de laatste 90 dagen van de export waren er {stil} dagen zonder enige spontane weergave.")
    if o["posts"]:
        laatste = o["posts"][0]
        if laatste["aangemaakt"]:
            uit.append(f"Laatste bijdrage op de bedrijfspagina: {laatste['aangemaakt'].strftime('%d-%m-%Y')} "
                       f"({(dt.date.today() - laatste['aangemaakt']).days} dagen geleden), “{_kort(laatste['titel'], 70)}”.")
        beste = max(o["posts"], key=lambda p: p["weergaven"] or 0)
        uit.append(f"Best bekeken bijdrage: “{_kort(beste['titel'], 70)}” met {_n(beste['weergaven'])} weergaven "
                   f"en {beste['klikken'] or 0} klikken.")
    if o.get("heeft_volgers"):
        uit.append(f"Nieuwe volgers in deze periode: {_n(o['som']['nieuwe_volgers'])} (sinds het begin van de export: "
                   f"{_n(ooit['nieuwe_volgers'])}).")
    for r in o["verdelingen"]:
        rij = r["data"]["rijen"]
        wie = "Volgers" if r["soort"].startswith("pagina-volgers:") else "Bezoekers" if r["soort"].startswith("pagina-bezoekers:") else None
        if wie and rij and r["data"].get("totaal") and r["titel"] in ("Functie", "Branche", "Locatie"):
            uit.append(f"{wie}, {r['titel'].lower()}: grootste groep {rij[0][0]} ({round(rij[0][1] / r['data']['totaal'] * 100)}%).")
    return uit


# ---------- persoonlijk profiel (AggregateAnalytics_....xlsx) ----------

def _pct(v):
    """'12%' -> 12.0, '< 1%' -> 0.5."""
    t = str(v or "").strip()
    if t.startswith("<"):
        return 0.5
    g = _getal(t, float)
    return g


def verwerk_profiel(q, inhoud):
    import warnings

    import openpyxl
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        boek = openpyxl.load_workbook(io.BytesIO(inhoud), read_only=True, data_only=True)
    uit = []
    for blad in boek.worksheets:
        rijen = [list(r) for r in blad.iter_rows(values_only=True)]
        titel = blad.title.strip().upper()
        if titel == "ONTDEKKING":
            d = {str(r[0]).strip(): r[1] for r in rijen if r and r[0]}
            data = {"periode": d.get("Algehele prestaties") or d.get("Overall performance"),
                    "impressies": _getal(d.get("Impressies") or d.get("Impressions")),
                    "bereikt": _getal(d.get("Bereikte leden") or d.get("Members reached"))}
            q("""INSERT INTO li_momentopname (soort, titel, periode, data, bijgewerkt) VALUES ('profiel:ontdekking', 'Ontdekking', %s, %s::jsonb, now())
                 ON CONFLICT (soort) DO UPDATE SET periode = EXCLUDED.periode, data = EXCLUDED.data, bijgewerkt = now() RETURNING 1""",
              (data["periode"], json.dumps(data)))
            uit.append(("profiel_ontdekking", blad.title, 1, None))
        elif titel == "ENGAGEMENT":
            laatste, n = None, 0
            for r in rijen[1:]:
                d = _datum(r[0]) if r else None
                if not d:
                    continue
                imp, inter = _getal(r[1]), _getal(r[2]) if len(r) > 2 else None
                q("""INSERT INTO lp_dag (datum, impressies, interacties, bijgewerkt) VALUES (%s, %s, %s, now())
                     ON CONFLICT (datum) DO UPDATE SET impressies = EXCLUDED.impressies, interacties = EXCLUDED.interacties,
                       bijgewerkt = now() RETURNING 1""", (d, imp, inter))
                _meting(q, "impressies", d, imp, "linkedin")
                _meting(q, "interacties", d, inter, "linkedin")
                laatste = d if laatste is None or d > laatste else laatste
                n += 1
            uit.append(("profiel_dag", blad.title, n, laatste))
        elif titel == "VOLGERS":
            laatste, n = None, 0
            if rijen and rijen[0] and rijen[0][0]:
                kop = str(rijen[0][0])
                datum_tekst = kop.rsplit(" ", 1)[-1].rstrip(":") if " " in kop else ""
                d_totaal = _datum(datum_tekst)
                totaal = _getal(rijen[0][1]) if len(rijen[0]) > 1 else None
                if d_totaal and totaal:
                    _meting(q, "volgers", d_totaal, totaal, "linkedin")
            for r in rijen:
                d = _datum(r[0]) if r and r[0] else None
                if not d or len(r) < 2:
                    continue
                nieuw = _getal(r[1])
                q("""INSERT INTO lp_dag (datum, nieuwe_volgers, bijgewerkt) VALUES (%s, %s, now())
                     ON CONFLICT (datum) DO UPDATE SET nieuwe_volgers = EXCLUDED.nieuwe_volgers, bijgewerkt = now() RETURNING 1""",
                  (d, nieuw))
                _meting(q, "nieuwe_volgers", d, nieuw, "linkedin")
                laatste = d if laatste is None or d > laatste else laatste
                n += 1
            uit.append(("profiel_volgers", blad.title, n, laatste))
        elif titel.startswith("TOPBIJDRAGEN") or titel.startswith("TOP POSTS"):
            n = 0
            for r in rijen:
                if not r or not str(r[0] or "").startswith("http"):
                    continue
                # Links: url, datum, interacties. Rechts (vanaf kolom 5): url, datum, impressies.
                q("""INSERT INTO lp_posts (url, publicatie, interacties, bijgewerkt) VALUES (%s, %s, %s, now())
                     ON CONFLICT (url) DO UPDATE SET publicatie = EXCLUDED.publicatie, interacties = EXCLUDED.interacties,
                       bijgewerkt = now() RETURNING 1""", (str(r[0]).strip()[:600], _datum(r[1]), _getal(r[2])))
                n += 1
                if len(r) > 6 and str(r[4] or "").startswith("http"):
                    q("""INSERT INTO lp_posts (url, publicatie, impressies, bijgewerkt) VALUES (%s, %s, %s, now())
                         ON CONFLICT (url) DO UPDATE SET publicatie = EXCLUDED.publicatie, impressies = EXCLUDED.impressies,
                           bijgewerkt = now() RETURNING 1""", (str(r[4]).strip()[:600], _datum(r[5]), _getal(r[6])))
            uit.append(("profiel_posts", blad.title, n, None))
        elif titel.startswith("DEMOGRAF"):
            groep = "publiek" if "DOE" in titel.split()[-1] or "VIEW" in titel else "netwerk"
            per = {}
            for r in rijen[1:]:
                if not r or not r[0] or len(r) < 3:
                    continue
                per.setdefault(str(r[0]).strip(), []).append([str(r[1]).strip(), _pct(r[2]), str(r[2]).strip()])
            for cat, lijst in per.items():
                q("""INSERT INTO li_momentopname (soort, titel, periode, data, bijgewerkt) VALUES (%s, %s, NULL, %s::jsonb, now())
                     ON CONFLICT (soort) DO UPDATE SET titel = EXCLUDED.titel, data = EXCLUDED.data, bijgewerkt = now() RETURNING 1""",
                  (f"profiel:{groep}:{cat}"[:120], cat, json.dumps({"rijen": lijst}, ensure_ascii=False)))
            uit.append(("profiel_demografie", blad.title, sum(len(v) for v in per.values()), None))
    return uit


def post_titel(url):
    """Een leesbare titel uit de LinkedIn-url: martijnbuurman_opvallend-veel-keepers-...-ugcPost-123 -> 'opvallend veel keepers ...'."""
    import urllib.parse
    t = urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])
    if "_" in t:
        t = t.split("_", 1)[1]
    for knip in ("-share-", "-ugcPost-", "-activity-"):
        t = t.split(knip)[0]
    woorden = t.replace("-", " ").strip()
    return woorden[:1].upper() + woorden[1:] if woorden else url


PROFIELSOORTEN = {
    "profiel_ontdekking": "Profiel: impressies en bereikte leden (totaal)",
    "profiel_dag": "Profiel: impressies en interacties per dag",
    "profiel_volgers": "Profiel: volgers (totaal en nieuw per dag)",
    "profiel_posts": "Profiel: topbijdragen",
    "profiel_demografie": "Profiel: demografie van publiek en netwerk",
}
SOORTEN.update(PROFIELSOORTEN)
KEEPERWOORDEN = ("keeper", "goalie", "doelman", "zerohero", "keeperscongres", "doelvrouw", "sluitpost")
WEEKDAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def detail_profiel(q, dagen):
    tot = q("SELECT max(datum) AS t FROM lp_dag WHERE impressies IS NOT NULL")[0]["t"]
    o = {"tot": tot, "laatste_upload": laatste_upload(q, "profiel"), "heeft_data": bool(tot)}
    volgers = q("""SELECT waarde, datum FROM metingen WHERE kanaal = 'linkedin' AND sleutel = 'volgers' ORDER BY datum DESC LIMIT 1""")
    o["volgers"] = {"waarde": int(volgers[0]["waarde"]), "datum": volgers[0]["datum"]} if volgers else None
    if not tot:
        o.update(posts=[], demografie={}, inzichten=[], dagreeks=[], maanden=[])
        return o
    sel = "coalesce(sum(impressies), 0)::int AS impressies, coalesce(sum(interacties), 0)::int AS interacties, coalesce(sum(nieuwe_volgers), 0)::int AS nieuwe_volgers"
    o["som"] = q(f"SELECT {sel} FROM lp_dag WHERE datum > %s::date - %s AND datum <= %s", (tot, dagen, tot))[0]
    o["toen"] = q(f"SELECT {sel}, count(impressies) AS n FROM lp_dag WHERE datum > %s::date - 2 * %s AND datum <= %s::date - %s",
                  (tot, dagen, tot, dagen))[0]
    o["ooit"] = q(f"SELECT {sel}, min(datum) AS vanaf FROM lp_dag")[0]
    o["dagreeks"] = q("SELECT * FROM lp_dag WHERE datum > %s::date - %s AND datum <= %s ORDER BY datum", (tot, dagen, tot))
    o["maanden"] = q("""SELECT date_trunc('month', datum)::date AS maand, coalesce(sum(impressies), 0)::int AS impressies,
                               coalesce(sum(interacties), 0)::int AS interacties, coalesce(sum(nieuwe_volgers), 0)::int AS volgers
                        FROM lp_dag GROUP BY 1 ORDER BY 1""")
    posts = q("SELECT * FROM lp_posts ORDER BY coalesce(impressies, 0) DESC, coalesce(interacties, 0) DESC")
    for p in posts:
        p["titel"] = post_titel(p["url"])
        p["keeper"] = any(w in p["url"].lower() for w in KEEPERWOORDEN)
        p["ratio"] = round(p["interacties"] / p["impressies"] * 100, 2) if p["interacties"] is not None and p["impressies"] else None
    o["posts"] = posts
    ond = q("SELECT * FROM li_momentopname WHERE soort = 'profiel:ontdekking'")
    o["ontdekking"] = (json.loads(ond[0]["data"]) if isinstance(ond[0]["data"], str) else ond[0]["data"]) if ond else None
    demo = {}
    for r in q("SELECT * FROM li_momentopname WHERE soort LIKE 'profiel:publiek:%%' OR soort LIKE 'profiel:netwerk:%%' ORDER BY soort"):
        data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
        demo.setdefault(r["soort"].split(":")[1], []).append({"titel": r["titel"], "rijen": data["rijen"]})
    o["demografie"] = demo
    o["inzichten"] = _inzichten_profiel(q, o)
    return o


def _inzichten_profiel(q, o):
    uit, ooit, ond = [], o["ooit"], o.get("ontdekking")
    if ond and ond.get("impressies"):
        uit.append(f"Periode {ond['periode']}: {_n(ond['impressies'])} impressies bij {_n(ond['bereikt'])} verschillende leden, "
                   f"{_n(ooit['interacties'])} interacties ({str(round(ooit['interacties'] / ond['impressies'] * 100, 2)).replace('.', ',')}% van de impressies).")
    if o["volgers"]:
        uit.append(f"{_n(o['volgers']['waarde'])} volgers op {o['volgers']['datum'].strftime('%d-%m-%Y')}, "
                   f"{_n(ooit['nieuwe_volgers'])} nieuwe volgers sinds {ooit['vanaf'].strftime('%d-%m-%Y')}.")
    piek = q("SELECT datum, impressies FROM lp_dag WHERE impressies IS NOT NULL ORDER BY impressies DESC LIMIT 1")
    if piek:
        bij = [p for p in o["posts"] if p["publicatie"] and 0 <= (piek[0]["datum"] - p["publicatie"]).days <= 3]
        uit.append(f"Drukste dag: {piek[0]['datum'].strftime('%d-%m-%Y')} met {_n(piek[0]['impressies'])} impressies"
                   + (f", rond de post “{_kort(bij[0]['titel'], 70)}”." if bij else "."))
    met_imp = [p for p in o["posts"] if p["impressies"]]
    if met_imp:
        b = met_imp[0]
        uit.append(f"Meeste impressies: “{_kort(b['titel'], 70)}” ({b['publicatie'].strftime('%d-%m-%Y') if b['publicatie'] else ''}) "
                   f"met {_n(b['impressies'])} impressies.")
        k = [p["impressies"] for p in met_imp if p["keeper"]]
        a = [p["impressies"] for p in met_imp if not p["keeper"]]
        if len(k) >= 3 and len(a) >= 3:
            gk, ga = round(sum(k) / len(k)), round(sum(a) / len(a))
            uit.append(f"Posts over keepers en ZEROHERO in de toplijst halen gemiddeld {_n(gk)} impressies ({len(k)} posts), "
                       f"andere posts gemiddeld {_n(ga)} ({len(a)} posts).")
        per_dag = {}
        for p in met_imp:
            if p["publicatie"]:
                per_dag.setdefault(p["publicatie"].weekday(), []).append(p["impressies"])
        kandidaten = {d: sum(v) / len(v) for d, v in per_dag.items() if len(v) >= 3}
        if len(kandidaten) >= 2:
            beste = max(kandidaten, key=kandidaten.get)
            uit.append(f"Topposts die op {WEEKDAGEN[beste]} verschenen halen gemiddeld de meeste impressies "
                       f"({_n(kandidaten[beste])}, {len(per_dag[beste])} posts).")
    met_ratio = [p for p in o["posts"] if p["ratio"] is not None and (p["impressies"] or 0) >= 1000]
    if met_ratio:
        b = max(met_ratio, key=lambda p: p["ratio"])
        uit.append(f"Meeste interactie naar verhouding: “{_kort(b['titel'], 70)}” "
                   f"({str(b['ratio']).replace('.', ',')}% van de impressies).")
    for groep, label in (("publiek", "Wie je posts zag"), ("netwerk", "Je netwerk")):
        for d in o["demografie"].get(groep, []):
            if d["titel"] in ("Branche", "Functietitel") and d["rijen"]:
                r = d["rijen"][0]
                uit.append(f"{label}, {d['titel'].lower()}: vooral {r[0]} ({r[2]}).")
    return uit
