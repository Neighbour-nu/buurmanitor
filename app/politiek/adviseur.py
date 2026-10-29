"""Adviseur (M7): een sparringpartner voor politieke keuzes, strategie en bijdragen.

Elk gesprek heeft een rol (strateeg, advocaat van de duivel, procesdeskundige, inwonersblik, rekenaar).
Bij elke vraag krijgt Claude het profiel van PRO Helmond, de agenda van de komende weken, wat er nieuw is
in de Inbox, wat Martijn bewaarde, en passages uit de kennisbank die bij de vraag passen.
"""
import datetime as dt
import threading
import traceback

from . import claude, kennisbank, nieuws, notubiz, raadsinfo, schrijven, whaller

ROLLEN = {
    "strateeg": ("Strateeg", "Je denkt mee over politieke strategie: waar liggen kansen voor een oppositiepartij, welke "
                 "volgorde, met wie samenwerken, hoe zet je een onderwerp op de agenda, wat levert het op bij inwoners en pers."),
    "duivel": ("Advocaat van de duivel", "Je zoekt bewust de zwakke plekken: hoe reageert de coalitie, welke tegenargumenten "
               "komen er, waar kan een argument tegen PRO Helmond worden gebruikt, wat ontbreekt er. Eerlijk en scherp, nooit flauw."),
    "proces": ("Procesdeskundige", "Je kent de werkwijze van een gemeenteraad: het Reglement van Orde, moties en amendementen, "
               "technische en schriftelijke vragen (artikel 40), termijnen, de rol van griffie, agendacommissie en commissies. "
               "Zeg erbij als iets per gemeente verschilt en Martijn het moet nagaan bij de griffie."),
    "inwoner": ("Inwonersblik", "Je kijkt door de ogen van Helmonders: wat merken inwoners ervan, wie wordt geraakt, hoe leg je "
                "het uit in gewone taal, wat zou een inwoner van Brandevoort, de binnenstad of Helmond-Noord ervan vinden."),
    "rekenaar": ("Rekenaar", "Je rekent na: begroting, investeringen, kapitaallasten, dekking, reserves en risico's. Laat je "
                 "berekening zien en zeg welke cijfers je uit de stukken haalt en welke je aanneemt."),
}

REGELS = """Antwoord in het Nederlands, helder en to the point. Geef een advies of een keuze, geen opsomming van alles wat kan.
Feiten alleen uit wat je hieronder krijgt of algemeen bekende kennis; zeg het als je iets niet zeker weet of als Martijn
het moet nagaan. Gebruik geen gedachtestreepjes. Stel aan het eind hooguit één wedervraag als dat helpt."""


def _achtergrond(c, vraag, vertrouwelijk=False):
    delen = []
    try:
        vandaag = dt.date.today()
        ev = notubiz.evenementen(vandaag, vandaag + dt.timedelta(days=21))
        if ev:
            delen.append("## Agenda komende drie weken\n" + "\n".join(
                f"- {whaller.lang(v['start'].date())} {v['start']:%H.%M}: {v['naam']}" for v in ev[:25]))
    except Exception:  # noqa: BLE001
        pass
    bewaard = c.execute("""SELECT soort, titel, datum FROM stukken WHERE status = 'bewaard'
                           ORDER BY coalesce(bijgewerkt, toegevoegd) DESC LIMIT 15""").fetchall()
    if bewaard:
        delen.append("## Wat Martijn bewaarde in de Inbox\n" + "\n".join(
            f"- {raadsinfo.SOORTEN.get(r['soort'], r['soort'])}: {r['titel']} ({r['datum'] or ''})" for r in bewaard))
    recent = c.execute("""SELECT soort, titel, reden FROM stukken WHERE NOT gezien AND soort NOT IN ('bijlage')
                            AND (bron <> 'nieuws' OR score >= 60)
                          ORDER BY pro DESC, coalesce(score, 60) DESC, id DESC LIMIT 15""").fetchall()
    if recent:
        delen.append("## Nieuw in de Inbox\n" + "\n".join(
            f"- {raadsinfo.SOORTEN.get(r['soort'], r['soort'])}: {r['titel']}" + (f" ({r['reden']})" if r["reden"] else "")
            for r in recent))
    kb = kennisbank.context(lambda sql, p=None: c.execute(sql, p).fetchall(), vraag, maximum=16000,
                            voorkeur=schrijven.VOORKEUR, vertrouwelijk=vertrouwelijk)
    if kb:
        delen.append("## Uit de kennisbank\n" + kb)
    return "\n\n".join(delen)


def systeem(c, rol, achtergrond):
    naam, uitleg = ROLLEN.get(rol, ROLLEN["strateeg"])
    profiel = nieuws.instelling(c, "nieuws_profiel", nieuws.STANDAARD_PROFIEL)
    return (f"Je bent de adviseur van Martijn Buurman in Promonitor. Je rol in dit gesprek: {naam}. {uitleg}\n\n"
            f"{profiel}\n\n{REGELS}\n\nVandaag is het {whaller.lang(dt.date.today())}.\n\n"
            f"# Achtergrond bij de eerste vraag in dit gesprek\n{achtergrond}")


def antwoord(c, gesprek_id):
    g = c.execute("SELECT * FROM gesprekken WHERE id = %s", (gesprek_id,)).fetchone()
    berichten = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gesprek_id,)).fetchall()
    claude.schrijfplafond(c)
    if not g["achtergrond"]:
        eerste = next((b["tekst"] for b in berichten if b["rol"] == "user"), "")
        c.execute("UPDATE gesprekken SET achtergrond = %s WHERE id = %s", (_achtergrond(c, eerste, g["vertrouwelijk"]), gesprek_id))
        g = c.execute("SELECT * FROM gesprekken WHERE id = %s", (gesprek_id,)).fetchone()
    model = nieuws.instelling(c, "claude_model_schrijven", "claude-sonnet-5-5")
    tekst = claude.gesprek(c, "adviseur", systeem(c, g["rol"], g["achtergrond"]),
                           [{"role": b["rol"], "content": b["tekst"]} for b in berichten][-30:], model,
                           max_tokens=3000, timeout=300)
    tekst = schrijven._geen_gedachtestreepjes(tekst)  # pylint: disable=protected-access
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    c.execute("UPDATE gesprekken SET status = 'klaar', fout = NULL, bijgewerkt = now() WHERE id = %s", (gesprek_id,))


def start(pool, gesprek_id):
    def werk():
        with pool.connection() as c:
            try:
                antwoord(c, gesprek_id)
            except Exception as e:  # noqa: BLE001
                c.execute("UPDATE gesprekken SET status = 'fout', fout = %s, bijgewerkt = now() WHERE id = %s",
                          (f"{e}\n\n{traceback.format_exc()[-800:]}", gesprek_id))
    threading.Thread(target=werk, daemon=True).start()
