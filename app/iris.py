"""Iris, de coördinator. In fase 0 kan Iris al praten, maar ziet ze alleen wat de portal zelf weet:
de gezondheid van de server, de meldingen, de taken en wat er op de planning staat."""
import datetime as dt

from . import personas
from .kern import claude, config, gezondheid, meldingen
from .kern.web import datum_lang

PLANNING = """Stand van de bouw:
- Fase 0 (nu): de kern van Buurmanitor: inloggen, meldingen, planner, gezondheid en gesprekken met jou (Iris).
- Fase 1 en 2: Promonitor (politiek) en de Doellijn (ZEROHERO) verhuizen één op één naar deze portal.
- Fase 3 tot en met 7: Clara (agenda), Bram (mail, Teams, WhatsApp en Signal), Noor (taken), Vera (nieuws), Jesse (socials).
Tot die fases klaar zijn, kun je agenda's, mail, taken, nieuws en socials nog niet zien. Zeg dat eerlijk als ernaar
gevraagd wordt, en verwijs voor politiek naar Promonitor en voor ZEROHERO naar de Doellijn."""


def context(c):
    nu = dt.datetime.now(config.TZ)
    delen = [f"Het is {datum_lang(nu.date())}, {nu:%H.%M} uur.", PLANNING]
    open_ = meldingen.open_meldingen(c, 10)
    if open_:
        delen.append("Open meldingen:\n" + "\n".join(f"- [{m['niveau']}] {m['titel']}" for m in open_))
    g, s = gezondheid.geheugen(), gezondheid.schijf()
    if g and s:
        delen.append(f"Server: geheugen {g['procent']} procent in gebruik, schijf {s['procent']} procent vol.")
    b = gezondheid.backups()
    if b:
        delen.append("Back-ups: " + "; ".join(
            f"{x['naam']} {'in orde' if x['ok'] else 'te oud of ontbreekt'}" for x in b))
    return "\n\n".join(delen)


def antwoord(c, gesprek_id):
    berichten = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id",
                          (gesprek_id,)).fetchall()
    systeem = personas.systeemtekst(personas.get("iris")) + "\n\n# Wat je nu weet\n" + context(c)
    tekst = claude.gesprek(c, "iris", systeem, [{"role": b["rol"], "content": b["tekst"]} for b in berichten][-30:],
                           max_tokens=1500)
    tekst = tekst.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst
