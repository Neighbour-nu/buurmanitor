"""Dashboard (M13): weekvooruitblik, maandoverzicht en kosten.

De vooruitblik gaat op zondagavond via Telegram en mail, het maandoverzicht op de eerste werkdag van de maand.
Beide staan ook altijd op het scherm Dashboard.
"""
import datetime as dt
import html
import os

from . import agenda, berichten, whaller

# Adres van de werkruimte Politiek in Buurmanitor, voor links in briefings en overzichten.
BASIS = os.environ.get("POLITIEK_BASIS_URL", "https://buurmanitor.neighbour.nu/politiek").rstrip("/")
MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
           "november", "december"]


def e(t):
    return html.escape(str(t or ""), quote=True)


def _inst(c):
    return {r["sleutel"]: r["waarde"] or "" for r in c.execute("SELECT sleutel, waarde FROM instellingen").fetchall()}


def _voornaam(c, inst):
    r = c.execute("SELECT naam FROM personen WHERE whaller = %s", (inst.get("agenda_ik", ""),)).fetchone()
    return r["naam"].split()[0] if r else ""


def week(c, van=None):
    """Vooruitblik op de komende zeven dagen."""
    van = van or dt.date.today()
    tot = van + dt.timedelta(days=7)
    inst = _inst(c)
    ik, voornaam = inst.get("agenda_ik", ""), _voornaam(c, inst)
    personen = c.execute("SELECT naam, whaller, rol FROM personen WHERE actief AND coalesce(whaller, '') <> ''").fetchall()
    vergaderingen, fout = [], ""
    try:
        alle, _ = agenda.overzicht(c, van, tot, inst, personen)
        vergaderingen = [v for v in alle if v["start"] and van <= v["start"].date() <= tot]
    except Exception as ex:  # noqa: BLE001
        fout = f"Notubiz was niet bereikbaar ({ex})."
    mijn_punten = [p for v in vergaderingen for p in v["punten"] if agenda.van_mij(p, ik)]
    deadlines = agenda.mijn_deadlines(vergaderingen, ik, van, dagen=7)
    acties = c.execute("""SELECT * FROM fractie_acties WHERE status <> 'afgerond' AND %s <> '' AND wie ILIKE %s
                          ORDER BY gewijzigd DESC""", (voornaam, f"%{voornaam}%")).fetchall()
    open_acties = c.execute("SELECT status, count(*) AS n FROM fractie_acties WHERE status <> 'afgerond' GROUP BY status").fetchall()
    vragen = c.execute("""SELECT titel, extra FROM stukken WHERE bron = 'notubiz' AND soort = 'vraag' AND pro
                            AND NOT coalesce((extra->>'beantwoord')::boolean, context LIKE 'beantwoord%%')""").fetchall()
    laat = [v for v in vragen if (v["extra"] or {}).get("verwacht") and v["extra"]["verwacht"] < van.isoformat()]
    stemadvies = c.execute("""SELECT id, datum, status FROM stemadviezen WHERE datum BETWEEN %s AND %s""", (van, tot)).fetchall()
    raden = [v for v in vergaderingen if v["soort"] == "raad" and not v["alleen_leden"]]
    zonder_advies = [v for v in raden if not any(s["datum"] == v["start"].date() for s in stemadvies)]
    return {"van": van, "tot": tot, "vergaderingen": vergaderingen, "mijn_punten": mijn_punten, "deadlines": deadlines,
            "acties": acties, "open_acties": {r["status"]: r["n"] for r in open_acties}, "vragen_open": len(vragen),
            "vragen_laat": laat, "zonder_advies": zonder_advies, "fout": fout, "voornaam": voornaam}


def maand(c, jaar, nr):
    """Wat er in een maand gebeurde."""
    begin = dt.date(jaar, nr, 1)
    eind = dt.date(jaar + (nr == 12), nr % 12 + 1, 1)
    een = lambda sql, p=(): c.execute(sql, p).fetchone()  # noqa: E731
    vragen = c.execute("""SELECT titel, datum, context FROM stukken WHERE bron = 'notubiz' AND soort = 'vraag' AND pro
                          AND datum >= %s AND datum < %s ORDER BY datum""", (begin, eind)).fetchall()
    beantwoord = een("""SELECT count(*) AS n FROM stukken WHERE bron = 'notubiz' AND soort = 'vraag' AND pro
                         AND (extra->>'datum_antwoord') >= %s AND (extra->>'datum_antwoord') < %s""",
                     (begin.isoformat(), eind.isoformat()))["n"]
    concepten = c.execute("""SELECT soort, count(*) AS n FROM concepten WHERE status = 'klaar'
                             AND aangemaakt >= %s AND aangemaakt < %s GROUP BY soort ORDER BY 2 DESC""", (begin, eind)).fetchall()
    notulen = een("SELECT count(*) AS n FROM notulen WHERE datum >= %s AND datum < %s", (begin, eind))["n"]
    besluiten = 0
    for n in c.execute("SELECT inhoud FROM notulen WHERE datum >= %s AND datum < %s AND inhoud IS NOT NULL", (begin, eind)).fetchall():
        besluiten += sum(len(p.get("besluiten") or []) for p in (n["inhoud"] or {}).get("punten", []))
    acties_nieuw = een("SELECT count(*) AS n FROM fractie_acties WHERE aangemaakt >= %s AND aangemaakt < %s", (begin, eind))["n"]
    acties_klaar = een("SELECT count(*) AS n FROM fractie_acties WHERE afgerond >= %s AND afgerond < %s", (begin, eind))["n"]
    toezeggingen = een("""SELECT count(*) AS n FROM stukken WHERE soort = 'toezegging' AND NOT (context LIKE '%%afgedaan%%')""")["n"]
    raadsstukken = een("""SELECT count(*) AS n FROM stukken WHERE bron = 'notubiz' AND soort IN ('raadsvoorstel', 'rib')
                          AND toegevoegd >= %s AND toegevoegd < %s""", (begin, eind))["n"]
    nieuws = een("""SELECT count(*) AS gewogen, count(*) FILTER (WHERE score >= 60) AS relevant,
                           count(*) FILTER (WHERE status = 'genegeerd') AS genegeerd,
                           count(*) FILTER (WHERE status = 'genegeerd' AND score >= 60) AS te_hoog,
                           count(*) FILTER (WHERE status = 'bewaard') AS bewaard
                    FROM stukken WHERE bron = 'nieuws' AND score >= 0 AND toegevoegd >= %s AND toegevoegd < %s""", (begin, eind))
    whaller_berichten = een("""SELECT count(*) AS n FROM whaller_invoer
                               WHERE coalesce(gegevens->>'whaller_url', '') <> '' AND bijgewerkt >= %s AND bijgewerkt < %s""",
                            (begin, eind))["n"]
    return {"begin": begin, "eind": eind, "naam": f"{MAANDEN[nr - 1]} {jaar}", "vragen": vragen, "beantwoord": beantwoord,
            "concepten": concepten, "notulen": notulen, "besluiten": besluiten, "acties_nieuw": acties_nieuw,
            "acties_klaar": acties_klaar, "toezeggingen_open": toezeggingen, "raadsstukken": raadsstukken,
            "nieuws": nieuws, "whaller": whaller_berichten, "kosten": kosten(c, begin, eind)}


def kosten(c, begin, eind):
    """Kosten van Claude per dag en per onderdeel."""
    per_dag = {r["dag"]: float(r["eur"]) for r in c.execute(
        """SELECT (tijd AT TIME ZONE 'Europe/Amsterdam')::date AS dag, sum(kosten_eur) AS eur FROM claude_gebruik
           WHERE tijd >= %s AND tijd < %s GROUP BY 1""", (begin, eind)).fetchall()}
    dagen = []
    d = begin
    while d < eind and d <= dt.date.today():
        dagen.append({"dag": d, "eur": per_dag.get(d, 0.0)})
        d += dt.timedelta(days=1)
    per_onderdeel = c.execute("""SELECT onderdeel, count(*) AS keer, sum(kosten_eur) AS eur FROM claude_gebruik
                                 WHERE tijd >= %s AND tijd < %s GROUP BY 1 ORDER BY 3 DESC""", (begin, eind)).fetchall()
    totaal = sum(x["eur"] for x in dagen)
    return {"dagen": dagen, "per_onderdeel": per_onderdeel, "totaal": totaal, "hoogste": max([x["eur"] for x in dagen] or [0])}


def euro(x):
    return f"€ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------- versturen ----------

def week_tekst(w):
    """Telegram-tekst (HTML) van de weekvooruitblik."""
    r = [f"<b>Promonitor | Vooruitblik {e(whaller.d(w['van']))} tot {e(whaller.d(w['tot']))}</b>"]
    if w["vergaderingen"]:
        r.append("\n<b>Vergaderingen</b>")
        for v in w["vergaderingen"]:
            r.append(f"• {whaller.DAGEN[v['start'].weekday()]} {v['start']:%d-%m %H.%M}: {e(v['naam'])}"
                     + (" (! alleen met inloggen)" if v["alleen_leden"] else ""))
    if w["mijn_punten"]:
        r.append("\n<b>Jouw agendapunten</b>")
        for p in w["mijn_punten"]:
            r.append(f"• {e(p['onderwerp'])} ({e(p['vergadering'])} {p['datum']:%d-%m}): nog {len(p['open'])} stap(pen)")
    if w["deadlines"]:
        r.append("\n<b>Deadlines</b>")
        for d in w["deadlines"]:
            r.append(f"• {whaller.DAGEN[d['datum'].weekday()]} {d['datum']:%d-%m}: {e(d['naam'])} bij {e(d['punt']['onderwerp'])}"
                     + (" (verlopen)" if d["verlopen"] else ""))
    if w["acties"]:
        r.append(f"\n<b>Jouw open acties ({len(w['acties'])})</b>")
        for a in w["acties"][:8]:
            r.append(f"• {e(a['actie'])}" + (f" ({e(a['wanneer'])})" if a["wanneer"] else ""))
    if w["zonder_advies"]:
        r.append("\n<b>Nog geen stemadvies</b>")
        for v in w["zonder_advies"]:
            r.append(f"• {e(v['naam'])} {v['start']:%d-%m}: {BASIS}/stemadvies")
    if w["vragen_laat"]:
        r.append(f"\n<b>Raadsvragen over de termijn ({len(w['vragen_laat'])})</b>")
        for v in w["vragen_laat"][:5]:
            r.append(f"• {e(v['titel'])}")
    r.append(f"\nDashboard: {BASIS}/dashboard")
    t = "\n".join(r)
    return t if len(t) < 4000 else t[:3900] + "\n…"


def maand_tekst(m):
    k = m["kosten"]
    n = m["nieuws"]
    r = [f"<b>Promonitor | Maandoverzicht {e(m['naam'])}</b>",
         f"\n• Raadsvragen gesteld: {len(m['vragen'])}, beantwoord: {m['beantwoord']}",
         f"• Concepten: {sum(x['n'] for x in m['concepten'])}"
         + (" (" + ", ".join(f"{x['n']} {x['soort'].replace('_', ' ')}" for x in m["concepten"]) + ")" if m["concepten"] else ""),
         f"• Fractievergaderingen genotuleerd: {m['notulen']}, besluiten: {m['besluiten']}",
         f"• Acties: {m['acties_nieuw']} nieuw, {m['acties_klaar']} afgerond",
         f"• Whaller-berichten geplaatst: {m['whaller']}",
         f"• Nieuwe raadsvoorstellen en raadsinformatiebrieven: {m['raadsstukken']}",
         f"• Toezeggingen nog open: {m['toezeggingen_open']}",
         f"• Nieuws: {n['gewogen']} gewogen, {n['relevant']} relevant, {n['bewaard']} bewaard, {n['genegeerd']} genegeerd"
         + (f" (waarvan {n['te_hoog']} met een hoge score: het filter kan scherper)" if n["te_hoog"] else ""),
         f"• Kosten Claude: {euro(k['totaal'])}",
         f"\nDashboard: {BASIS}/dashboard"]
    return "\n".join(r)


def _mail_html(titel, telegram_tekst):
    import re  # pylint: disable=import-outside-toplevel
    body = telegram_tekst.replace("\n", "<br>")
    body = re.sub(r"(https://[^\s<]+)", r'<a href="\1">\1</a>', body)
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;color:#1A1A1A;max-width:680px">'
            f'<h2 style="color:#FF0000;margin-bottom:2px">PROMONITOR</h2><p style="color:#5C5C5C">{e(titel)}</p>{body}</div>')


def _stuur(titel, tekst):
    import re  # pylint: disable=import-outside-toplevel
    uitslag = []
    try:
        berichten.stuur_telegram(tekst, html=True)
        uitslag.append("Telegram verstuurd")
    except Exception as ex:  # noqa: BLE001
        uitslag.append(f"Telegram mislukt ({ex})")
    try:
        platte = html.unescape(re.sub(r"<[^>]+>", "", tekst))
        berichten.stuur_mail(f"Promonitor: {titel}", platte, html=_mail_html(titel, tekst))
        uitslag.append("mail verstuurd")
    except Exception as ex:  # noqa: BLE001
        uitslag.append(f"mail mislukt ({ex})")
    return ", ".join(uitslag)


def stuur_week(c):
    w = week(c, dt.date.today() + dt.timedelta(days=1))
    return _stuur(f"vooruitblik week {whaller.d(w['van'])}", week_tekst(w))


def stuur_maand(c):
    """Het overzicht van de vorige maand."""
    vandaag = dt.date.today()
    vorige = (vandaag.replace(day=1) - dt.timedelta(days=1))
    m = maand(c, vorige.year, vorige.month)
    return _stuur(f"maandoverzicht {m['naam']}", maand_tekst(m))


def eerste_werkdag(datum):
    d = datum.replace(day=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d
