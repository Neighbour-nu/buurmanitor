"""Iris, de coördinator. In fase 0 kan Iris al praten, maar ziet ze alleen wat de portal zelf weet:
de gezondheid van de server, de meldingen, de taken en wat er op de planning staat."""
import datetime as dt

from . import personas
from .kern import claude, config, gezondheid, meldingen
from .kern.web import datum_lang

PLANNING = """Stand van de bouw:
- Klaar: de kern van Buurmanitor, de werkruimtes Politiek (Promonitor) en ZEROHERO (de Doellijn), en Clara (agenda).
- Volgende fases: Bram (mail, Teams, WhatsApp en Signal), Noor (taken), Vera (nieuws), Jesse (socials).
Mail, taken, nieuws en socials kun je dus nog niet zien. Zeg dat eerlijk als ernaar gevraagd wordt. Voor de agenda
is Clara er; voor politiek en ZEROHERO de werkruimtes."""


def context(c):
    nu = dt.datetime.now(config.TZ)
    delen = [f"Het is {datum_lang(nu.date())}, {nu:%H.%M} uur.", PLANNING]
    open_ = meldingen.open_meldingen(c, 10)
    if open_:
        delen.append("Open meldingen:\n" + "\n".join(f"- [{m['niveau']}] {m['titel']}" for m in open_))
    agenda = _agenda(c, nu)
    if agenda:
        delen.append(agenda)
    g, s = gezondheid.geheugen(), gezondheid.schijf()
    if g and s:
        delen.append(f"Server: geheugen {g['procent']} procent in gebruik, schijf {s['procent']} procent vol.")
    b = gezondheid.backups()
    if b:
        delen.append("Back-ups: " + "; ".join(
            f"{x['naam']} {'in orde' if x['ok'] else 'te oud of ontbreekt'}" for x in b))
    return "\n\n".join(delen)


def _agenda(c, nu):
    """Wat Clara vandaag ziet, kort, zodat Iris de dagstart kan maken."""
    from .clara import analyse, sync  # pylint: disable=import-outside-toplevel
    if not sync.koppelingen(c, alleen_actief=True):
        return "Agenda: nog geen agenda's gekoppeld bij Clara."
    begin = dt.datetime.combine(nu.date(), dt.time(0), tzinfo=config.TZ)
    vandaag = sync.afspraken(c, begin, begin + dt.timedelta(days=1))
    regels = [f"- {'hele dag' if a['heledag'] else a['start'].astimezone(config.TZ).strftime('%H.%M')} {a['titel']} ({a['koppeling']})"
              for a in sorted(vandaag, key=lambda a: (not a["heledag"], a["start"])) if a["antwoord"] != "afgewezen"]
    week = sync.afspraken(c, nu, nu + dt.timedelta(days=7))
    conf = analyse.conflicten(week)
    uit = ["Agenda vandaag (van Clara):"] + (regels or ["- geen afspraken"])
    if conf:
        uit.append(f"Potentiële overlap komende week: {len(conf)} keer (details bij Clara).")
    return "\n".join(uit)


def antwoord(c, gesprek_id):
    berichten = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id",
                          (gesprek_id,)).fetchall()
    systeem = personas.systeemtekst(personas.get("iris")) + "\n\n# Wat je nu weet\n" + context(c)
    tekst = claude.gesprek(c, "iris", systeem, [{"role": b["rol"], "content": b["tekst"]} for b in berichten if (b["tekst"] or "").strip()][-30:],
                           max_tokens=1500)
    tekst = tekst.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst
