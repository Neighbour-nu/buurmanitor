"""Herhalende taken: een reeks (bijvoorbeeld elke maandag en donderdag 9.00-10.00, van 1 november tot 31 januari)
waar Buurmanitor losse taken van maakt, elk met een eigen afspraak in de agenda. Zo rond je elke keer apart af, of
schuif of verwijder je één keer zonder de rest te raken.

De taken worden steeds voor de komende weken gemaakt (HORIZON_DAGEN); de planner vult elke paar uur aan, tot de
einddatum van de reeks. Wat je zelf weghaalt, komt niet terug: de reeks onthoudt tot welke dag hij al gemaakt heeft.
"""
import calendar
import datetime as dt
import json

from ..kern import config, planner
from ..kern.web import DAGEN

HORIZON_DAGEN = 56  # zo ver vooruit staan de taken (en hun afspraken) klaar
DAGCODES = ("ma", "di", "wo", "do", "vr", "za", "zo")
MAX_PER_KEER = 120  # nooit meer taken tegelijk maken dan dit (bescherming tegen een vergissing)


def datums(r, van, tot):
    """De dagen in [van, tot] waarop de reeks een taak geeft. Pure functie.
    soort 'week': op de gekozen weekdagen, elke 'interval' weken (gerekend vanaf de week van de begindatum).
    soort 'maand': elke 'interval' maanden op de dag van de begindatum (bestaat die dag niet, dan de laatste dag)."""
    begin, eind = r["begindatum"], r.get("einddatum")
    van, tot = max(van, begin), min(tot, eind) if eind else tot
    interval = max(1, int(r.get("interval") or 1))
    uit = []
    if van > tot:
        return uit
    if r.get("soort") == "maand":
        j, m = begin.year, begin.month
        while True:
            dag = dt.date(j, m, min(begin.day, calendar.monthrange(j, m)[1]))
            if dag > tot:
                break
            if dag >= van:
                uit.append(dag)
            m += interval
            j, m = j + (m - 1) // 12, (m - 1) % 12 + 1
        return uit
    dagen = {DAGCODES.index(d) for d in r.get("dagen") or [] if d in DAGCODES} or {begin.weekday()}
    week0 = begin - dt.timedelta(days=begin.weekday())
    d = van
    while d <= tot:
        if d.weekday() in dagen and ((d - week0).days // 7) % interval == 0:
            uit.append(d)
        d += dt.timedelta(days=1)
    return uit


def beschrijf(r):
    """Het patroon in gewone woorden. Pure functie."""
    interval = max(1, int(r.get("interval") or 1))
    if r.get("soort") == "maand":
        wat = (f"elke {interval} maanden" if interval > 1 else "elke maand") + f" op de {r['begindatum'].day}e"
    else:
        namen = [DAGEN[DAGCODES.index(d)] for d in DAGCODES if d in (r.get("dagen") or [])] or [DAGEN[r["begindatum"].weekday()]]
        lijst = namen[0] if len(namen) == 1 else ", ".join(namen[:-1]) + " en " + namen[-1]
        wat = ("elke " if interval == 1 else f"om de {interval} weken op ") + lijst
        if set(r.get("dagen") or []) == {"ma", "di", "wo", "do", "vr"} and interval == 1:
            wat = "elke werkdag"
        if len(r.get("dagen") or []) == 7 and interval == 1:
            wat = "elke dag"
    tijd = ""
    if r.get("starttijd") or r.get("eindtijd"):
        tijd = ", " + (f"{r['starttijd']:%H.%M}" if r.get("starttijd") else "") + (f"-{r['eindtijd']:%H.%M}" if r.get("eindtijd") else "")
    periode = f", vanaf {r['begindatum']:%d-%m-%Y}" + (f" tot en met {r['einddatum']:%d-%m-%Y}" if r.get("einddatum") else "")
    return wat + tijd + periode


def uit_formulier(f, vandaag):
    """De velden van een reeks uit het formulier, gecontroleerd. Geeft (velden, fout). Pure functie."""
    def datum(w):
        try:
            return dt.date.fromisoformat(str(w or "").strip()[:10]) if str(w or "").strip() else None
        except ValueError:
            return None

    def tijd(w):
        try:
            return dt.time.fromisoformat(str(w or "").strip()[:5]) if str(w or "").strip() else None
        except ValueError:
            return None
    dagen = [d for d in DAGCODES if d in (f.getlist("dagen") if hasattr(f, "getlist") else f.get("dagen") or [])]
    soort = "maand" if f.get("soort") == "maand" else "week"
    try:
        interval = max(1, min(int(f.get("interval") or 1), 12))
    except ValueError:
        interval = 1
    prio = str(f.get("prio") or "middel")
    status = str(f.get("status") or "gepland")
    v = {"titel": str(f.get("titel") or "").strip()[:160], "beschrijving": str(f.get("beschrijving") or "").strip()[:4000],
         "prio": prio if prio in ("hoog", "middel", "laag") else "middel",
         "status": status if status in ("nieuw", "triage", "gepland", "actief") else "gepland",
         "soort": soort, "dagen": dagen, "interval": interval, "begindatum": datum(f.get("begindatum")) or vandaag,
         "einddatum": datum(f.get("einddatum")), "starttijd": tijd(f.get("starttijd")), "eindtijd": tijd(f.get("eindtijd")),
         "doel": str(f.get("doel") or "")}
    if not v["titel"]:
        return v, "Geef de herhalende taak een korte titel."
    if soort == "week" and not dagen:
        return v, "Kies op welke dagen de taak terugkomt."
    if v["einddatum"] and v["einddatum"] < v["begindatum"]:
        return v, "De einddatum ligt voor de begindatum."
    if v["starttijd"] and v["eindtijd"] and v["eindtijd"] <= v["starttijd"]:
        return v, "De eindtijd ligt voor de begintijd."
    return v, ""


def _rij(r):
    return dict(r, dagen=r["dagen"] if isinstance(r["dagen"], list) else json.loads(r["dagen"] or "[]"))


def aanvullen(c, rid, vandaag=None, agenda=True):
    """Maak de taken van een reeks voor de komende weken (vanaf de dag na wat al gemaakt is). Geeft het aantal nieuwe."""
    from .web import agenda_bijwerken, nieuw  # pylint: disable=import-outside-toplevel
    vandaag = vandaag or dt.datetime.now(config.TZ).date()
    r = c.execute("SELECT * FROM takenreeksen WHERE id = %s AND actief", (rid,)).fetchone()
    if not r:
        return 0
    r = _rij(r)
    van = max(vandaag, (r["gemaakt_tot"] + dt.timedelta(days=1)) if r["gemaakt_tot"] else r["begindatum"])
    tot = vandaag + dt.timedelta(days=HORIZON_DAGEN)
    lijst = datums(r, van, tot)[:MAX_PER_KEER]
    al = {x["startdatum"] for x in c.execute("SELECT startdatum FROM takenbord WHERE reeks_id = %s AND startdatum >= %s", (rid, van)).fetchall()}
    for dag in lijst:
        if dag in al:
            continue
        tid = nieuw(c, {"titel": r["titel"], "beschrijving": r["beschrijving"], "prio": r["prio"], "status": r["status"],
                        "startdatum": dag, "einddatum": dag, "starttijd": r["starttijd"], "eindtijd": r["eindtijd"], "doel": r["doel"]})
        c.execute("UPDATE takenbord SET reeks_id = %s WHERE id = %s", (rid, tid))
        if agenda:
            agenda_bijwerken(c, tid)
    klaar_tot = lijst[-1] if len(lijst) == MAX_PER_KEER else min(tot, r["einddatum"]) if r["einddatum"] else tot
    c.execute("UPDATE takenreeksen SET gemaakt_tot = greatest(coalesce(gemaakt_tot, %s), %s) WHERE id = %s", (klaar_tot, klaar_tot, rid))
    if r["einddatum"] and klaar_tot >= r["einddatum"]:
        c.execute("UPDATE takenreeksen SET actief = false, klaar = true WHERE id = %s", (rid,))
    return len([d for d in lijst if d not in al])


def toekomst_weg(c, rid, vanaf):
    """De taken van een reeks vanaf een dag weghalen (met hun afspraken), behalve wat al afgerond is. Geeft het aantal."""
    from .web import _weg_uit_agenda  # pylint: disable=import-outside-toplevel
    from ..clara import bronnen as agenda_bronnen, kluis  # pylint: disable=import-outside-toplevel
    rijen = c.execute("""SELECT * FROM takenbord WHERE reeks_id = %s AND status <> 'afgerond' AND coalesce(startdatum, einddatum) >= %s""",
                      (rid, vanaf)).fetchall()
    for t in rijen:
        try:
            _weg_uit_agenda(c, t)
        except (agenda_bronnen.BronFout, kluis.KluisFout):
            pass
        c.execute("DELETE FROM takenbord WHERE id = %s", (t["id"],))
    return len(rijen)


def opslaan(c, velden, rid=None, vandaag=None):
    """Een reeks maken of wijzigen. Bij een wijziging gaan de nog open taken vanaf vandaag weg en komen ze opnieuw,
    volgens het nieuwe patroon. Geeft (rid, aantal gemaakt)."""
    vandaag = vandaag or dt.datetime.now(config.TZ).date()
    w = velden | {"dagen": json.dumps(velden["dagen"])}
    if rid:
        toekomst_weg(c, rid, vandaag)
        c.execute("""UPDATE takenreeksen SET titel = %(titel)s, beschrijving = %(beschrijving)s, prio = %(prio)s, status = %(status)s,
                     soort = %(soort)s, dagen = %(dagen)s, interval = %(interval)s, begindatum = %(begindatum)s, einddatum = %(einddatum)s,
                     starttijd = %(starttijd)s, eindtijd = %(eindtijd)s, doel = %(doel)s, actief = true, klaar = false,
                     gemaakt_tot = %(gemaakt_tot)s WHERE id = %(id)s""", w | {"id": rid, "gemaakt_tot": vandaag - dt.timedelta(days=1)})
    else:
        rid = c.execute("""INSERT INTO takenreeksen (titel, beschrijving, prio, status, soort, dagen, interval, begindatum, einddatum,
                                                    starttijd, eindtijd, doel)
                           VALUES (%(titel)s, %(beschrijving)s, %(prio)s, %(status)s, %(soort)s, %(dagen)s, %(interval)s, %(begindatum)s,
                                   %(einddatum)s, %(starttijd)s, %(eindtijd)s, %(doel)s) RETURNING id""", w).fetchone()["id"]
    return rid, aanvullen(c, rid, vandaag)


def stoppen(c, rid, vandaag=None):
    """De reeks stopt vandaag: open taken vanaf vandaag gaan weg (met hun afspraken); wat al was, blijft."""
    vandaag = vandaag or dt.datetime.now(config.TZ).date()
    n = toekomst_weg(c, rid, vandaag)
    c.execute("UPDATE takenreeksen SET actief = false, einddatum = least(coalesce(einddatum, %s), %s) WHERE id = %s",
              (vandaag - dt.timedelta(days=1), vandaag - dt.timedelta(days=1), rid))
    return n


def overzicht(c, vandaag=None):
    """De reeksen voor het scherm: patroon, periode, eerstvolgende keer en hoeveel er open staan."""
    vandaag = vandaag or dt.datetime.now(config.TZ).date()
    uit = []
    for r in c.execute("""SELECT r.*, (SELECT min(startdatum) FROM takenbord t WHERE t.reeks_id = r.id AND t.status <> 'afgerond'
                                     AND t.startdatum >= %s) AS volgende,
                                  (SELECT count(*) FROM takenbord t WHERE t.reeks_id = r.id AND t.status <> 'afgerond') AS open,
                                  (SELECT count(*) FROM takenbord t WHERE t.reeks_id = r.id AND t.status = 'afgerond') AS af
                           FROM takenreeksen r WHERE r.actief OR r.einddatum >= %s - 30 ORDER BY r.actief DESC, r.titel""",
                       (vandaag, vandaag)).fetchall():
        r = _rij(r)
        uit.append(r | {"patroon": beschrijf(r)})
    return uit


@planner.taak("takenreeksen", elke=dt.timedelta(hours=6))
def taak_reeksen(c):
    nieuw_ = sum(aanvullen(c, r["id"]) for r in c.execute("SELECT id FROM takenreeksen WHERE actief").fetchall())
    return f"{nieuw_} taken uit herhalende taken gemaakt"
