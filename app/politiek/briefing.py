"""De ochtendbriefing: elke dag om 8.00 uur via Telegram en mail.

Inhoud: het belangrijkste nieuws van de afgelopen 24 uur, raadsinformatie (eerst wat PRO raakt),
de vergaderingen van vandaag en morgen, en op woensdag de Whaller-berichten voor volgende week.
"""
import datetime as dt
import html
import os
import re

from . import agenda, berichten, notubiz, whaller

# Adres van de werkruimte Politiek in Buurmanitor, voor links in briefings en overzichten.
BASIS = os.environ.get("POLITIEK_BASIS_URL", "https://buurmanitor.neighbour.nu/politiek").rstrip("/")


def e(t):
    return html.escape(t or "", quote=True)


def _instelling(c, sleutel, standaard):
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel,)).fetchone()
    return (r["waarde"] if r and r["waarde"] else standaard)


def gegevens(c, nu=None):
    nu = nu or dt.datetime.now()
    drempel = int(_instelling(c, "briefing_drempel", "60") or 60)
    nieuws = c.execute("""SELECT * FROM stukken WHERE bron = 'nieuws' AND score >= %s AND status <> 'genegeerd'
                            AND toegevoegd > now() - interval '24 hours'
                          ORDER BY score DESC, id DESC LIMIT 8""", (drempel,)).fetchall()
    raad = c.execute("""SELECT * FROM stukken WHERE bron = 'notubiz' AND soort NOT IN ('bijlage', 'besloten') AND status <> 'genegeerd'
                          AND (NOT gezien OR pro) AND coalesce(bijgewerkt, toegevoegd) > now() - interval '24 hours'
                          AND NOT (soort = 'vraag' AND pro)
                        ORDER BY pro DESC, soort = 'besloten', datum DESC NULLS LAST LIMIT 10""").fetchall()
    # Antwoorden op eigen raadsvragen die het afgelopen etmaal binnenkwamen.
    antwoorden = c.execute("""SELECT * FROM stukken WHERE bron = 'notubiz' AND soort = 'vraag' AND pro
                                AND bijgewerkt > now() - interval '24 hours'
                                AND (coalesce((extra->>'beantwoord')::boolean, false) OR context LIKE 'beantwoord%%')
                              ORDER BY nummer""").fetchall()
    besloten = {r["extern_id"] for r in c.execute("SELECT extern_id FROM stukken WHERE soort = 'besloten'").fetchall()}
    try:
        vergaderingen = notubiz.evenementen(nu.date(), nu.date() + dt.timedelta(days=1))
        for v in vergaderingen:
            v["alleen_leden"] = v.get("alleen_leden") or f"leden-{v['id']}" in besloten
    except Exception:  # noqa: BLE001 - de briefing gaat ook zonder agenda
        vergaderingen = None
    # Deadlines van mijn agendapunten: vandaag, morgen en wat al verlopen is.
    try:
        inst = {r["sleutel"]: r["waarde"] or "" for r in c.execute("SELECT sleutel, waarde FROM instellingen").fetchall()}
        personen = c.execute("""SELECT naam, whaller, rol FROM personen
                                 WHERE actief AND coalesce(whaller, '') <> ''""").fetchall()
        verg, _ = agenda.overzicht(c, nu.date(), nu.date() + dt.timedelta(days=21), inst, personen)
        deadlines = agenda.mijn_deadlines(verg, inst.get("agenda_ik", ""), nu.date(), dagen=1)
    except Exception:  # noqa: BLE001 - de briefing gaat ook zonder agenda
        deadlines = []
    plaatsdag = _instelling(c, "whaller_plaatsdag", "woensdag").lower()
    whaller_dag = whaller.DAGEN[nu.weekday()] == plaatsdag
    return {"nu": nu, "nieuws": nieuws, "raad": raad, "vergaderingen": vergaderingen, "whaller_dag": whaller_dag,
            "antwoorden": antwoorden, "deadlines": deadlines,
            "soorten": {"raadsvoorstel": "Raadsvoorstel", "rib": "RIB", "vraag": "Raadsvraag", "motie": "Motie",
                        "toezegging": "Toezegging", "ingekomen": "Ingekomen", "besloten": "Alleen raadsleden"}}


def telegram(g):
    """Korte versie voor Telegram (HTML, onder 4.000 tekens)."""
    regels = [f"<b>Promonitor | {e(whaller.lang(g['nu'].date()).capitalize())}</b>"]
    if g.get("deadlines"):
        regels.append("\n<b>Jouw deadlines</b>")
        for d in g["deadlines"]:
            wanneer = "verlopen" if d["verlopen"] else ("vandaag" if d["datum"] == g["nu"].date() else "morgen")
            regels.append(f"• {wanneer}: {e(d['naam'])} bij {e(d['punt']['onderwerp'])}")
    if g["nieuws"]:
        regels.append("\n<b>Nieuws</b>")
        for s in g["nieuws"]:
            regels.append(f"• <b>{s['score']}</b> <a href=\"{e(s['document_url'])}\">{e(s['titel'])}</a> ({e(s['bronnaam'])})"
                          + (f"\n  <i>{e(s['reden'])}</i>" if s["reden"] else ""))
    if g.get("antwoorden"):
        regels.append("\n<b>Antwoord op onze vragen</b>")
        for s in g["antwoorden"]:
            url = (s["extra"] or {}).get("antwoord_url") or s["document_url"]
            regels.append(f"• <a href=\"{e(url)}\">{e(s['titel'])}</a>")
    if g["raad"]:
        regels.append("\n<b>Raadsinformatie</b>")
        for s in g["raad"]:
            teken = "PRO · " if s["pro"] else ("! " if s["soort"] == "besloten" else "")
            regels.append(f"• {teken}{e(g['soorten'].get(s['soort'], s['soort']))}: "
                          f"<a href=\"{e(s['document_url'])}\">{e(s['titel'])}</a>")
    if g["vergaderingen"]:
        regels.append("\n<b>Vandaag en morgen</b>")
        for v in g["vergaderingen"]:
            regels.append(f"• {whaller.DAGEN[v['start'].weekday()]} {v['start']:%H.%M}: {e(v['naam'])}"
                          + (" (! alleen met inloggen)" if v.get("alleen_leden") else ""))
    if g["whaller_dag"]:
        regels.append(f"\n<b>Whaller</b>\nDe berichten voor volgende week staan klaar: {BASIS}/whaller")
    if len(regels) == 1:
        regels.append("\nGeen belangrijk nieuws en geen nieuwe raadsstukken sinds gisteren.")
    regels.append(f"\nInbox: {BASIS}/inbox")
    tekst = "\n".join(regels)
    return tekst if len(tekst) < 4000 else tekst[:3900] + "\n…"


def mail(g):
    """Uitgebreidere versie voor de mail: (onderwerp, html, platte tekst)."""
    datum = whaller.lang(g["nu"].date())
    stijl = "font-family:Arial,Helvetica,sans-serif;color:#1A1A1A;max-width:680px"
    delen = [f'<div style="{stijl}"><h2 style="color:#FF0000;margin-bottom:2px">PROMONITOR</h2>'
             f'<p style="color:#5C5C5C;margin-top:0">Ochtendbriefing {e(datum)}</p>']

    def kop(t):
        delen.append(f'<h3 style="color:#007A00;border-bottom:2px solid #00AA00;padding-bottom:2px">{e(t)}</h3>')

    if g.get("deadlines"):
        kop("Jouw deadlines")
        delen.append("<ul>" + "".join(
            f'<li><b>{"verlopen" if d["verlopen"] else ("vandaag" if d["datum"] == g["nu"].date() else "morgen")}</b>: '
            f'{e(d["naam"])} bij {e(d["punt"]["onderwerp"])} '
            f'<span style="color:#5C5C5C">({e(d["punt"]["vergadering"])} {d["punt"]["datum"]:%d-%m})</span></li>'
            for d in g["deadlines"]) + f'</ul><p><a href="{BASIS}/agenda">Naar de Agenda</a></p>')
    if g["nieuws"]:
        kop("Nieuws")
        for s in g["nieuws"]:
            delen.append(f'<p><b style="color:#007A00">{s["score"]}</b> <a href="{e(s["document_url"])}">{e(s["titel"])}</a>'
                         f' <span style="color:#5C5C5C">({e(s["bronnaam"])})</span><br>'
                         f'<span style="color:#5C5C5C">{e(s["reden"] or "")}</span></p>')
    if g.get("antwoorden"):
        kop("Antwoord op onze vragen")
        for s in g["antwoorden"]:
            url = (s["extra"] or {}).get("antwoord_url") or s["document_url"]
            delen.append(f'<p><a href="{e(url)}">{e(s["titel"])}</a> <span style="color:#5C5C5C">'
                         f'(<a href="{BASIS}/vragen?toon=beantwoord">alle vragen</a>)</span></p>')
    if g["raad"]:
        kop("Raadsinformatie")
        for s in g["raad"]:
            label = ("PRO · " if s["pro"] else "") + g["soorten"].get(s["soort"], s["soort"])
            delen.append(f'<p><b>{e(label)}</b>: <a href="{e(s["document_url"])}">{e(s["titel"])}</a>'
                         + (f'<br><span style="color:#5C5C5C">{e(s["context"][:200])}</span>' if s["context"] else "") + "</p>")
    if g["vergaderingen"]:
        kop("Vandaag en morgen")
        delen.append("<ul>" + "".join(
            f'<li>{whaller.DAGEN[v["start"].weekday()]} {v["start"]:%H.%M} uur: <a href="{e(v["url"])}">{e(v["naam"])}</a>'
            + (" <b style=\"color:#D00000\">! alleen met inloggen</b>" if v.get("alleen_leden") else "") + "</li>"
            for v in g["vergaderingen"]) + "</ul>")
    if g["whaller_dag"]:
        kop("Whaller")
        delen.append(f'<p>De berichten voor de vergaderingen van volgende week staan klaar: '
                     f'<a href="{BASIS}/whaller">open Whaller in Promonitor</a>.</p>')
    if not (g["nieuws"] or g["raad"] or g["vergaderingen"] or g["whaller_dag"] or g.get("antwoorden") or g.get("deadlines")):
        delen.append("<p>Geen belangrijk nieuws en geen nieuwe raadsstukken sinds gisteren.</p>")
    delen.append(f'<p style="margin-top:24px"><a href="{BASIS}/inbox">Naar de Inbox</a> · '
                 f'<a href="{BASIS}/">Naar Vandaag</a></p></div>')
    platte = telegram(g).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    platte = html.unescape(re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2 (\1)", platte))
    return f"Promonitor: briefing {datum}", "\n".join(delen), platte


def versturen(c, nu=None):
    """Stuurt de briefing via Telegram en mail. Geeft een korte uitslag terug."""
    g = gegevens(c, nu)
    uitslag = []
    try:
        berichten.stuur_telegram(telegram(g), html=True)
        uitslag.append("Telegram verstuurd")
    except Exception as ex:  # noqa: BLE001
        uitslag.append(f"Telegram mislukt ({ex})")
    try:
        onderwerp, html_, platte = mail(g)
        berichten.stuur_mail(onderwerp, platte, html=html_)
        uitslag.append("mail verstuurd")
    except Exception as ex:  # noqa: BLE001
        uitslag.append(f"mail mislukt ({ex})")
    return f"{len(g['nieuws'])} nieuws, {len(g['raad'])} raadsstukken; " + ", ".join(uitslag)
