"""Iris, de coördinator. In fase 0 kan Iris al praten, maar ziet ze alleen wat de portal zelf weet:
de gezondheid van de server, de meldingen, de taken en wat er op de planning staat."""
import datetime as dt
import json

from . import personas
from .kern import claude, config, gezondheid, meldingen
from .kern.web import datum_lang

PLANNING = """Stand van de bouw:
- Klaar: de kern van Buurmanitor, de werkruimtes Politiek (Promonitor) en ZEROHERO (de Doellijn), en het hele team:
  Clara (agenda), Bram (mail uit Gmail en Outlook), Noor (het takenbord en de planning van taken), Vera (nieuws:
  keepers, lokale en landelijke politiek, overig) en Jesse (luistert mee op de socials: reacties op Martijns posts,
  en wat er over keepers en Helmond gezegd wordt).
- Ook klaar: het CRM (menu CRM): relaties uit de mail, kansen per activiteit (ZEROHERO, Upper Spire, IV-professional,
  politiek) en projecten met taken op het takenbord. Jij kunt kansen en projecten voorstellen (zie hieronder).
- Volgende stap: Bram verder (Teams, WhatsApp en Signal).
Teams, WhatsApp en Signal kun je dus nog niet zien. Zeg dat eerlijk als ernaar gevraagd wordt.
Voor de agenda is Clara er, voor mail Bram, voor taken Noor (onder Taken), voor nieuws Vera (onder Nieuws), voor
socials Jesse (onder Socials), voor politiek en ZEROHERO de werkruimtes."""


def context(c):
    nu = dt.datetime.now(config.TZ)
    delen = [f"Het is {datum_lang(nu.date())}, {nu:%H.%M} uur.", PLANNING]
    open_ = meldingen.open_meldingen(c, 10)
    if open_:
        delen.append("Open meldingen:\n" + "\n".join(f"- [{m['niveau']}] {m['titel']}" for m in open_))
    agenda = _agenda(c, nu)
    if agenda:
        delen.append(agenda)
    post = _post(c)
    if post:
        delen.append(post)
    delen.append(_taken(c, nu))
    delen.append(_nieuws(c, nu))
    delen.append(_socials(c))
    crm = _crm(c, nu)
    if crm:
        delen.append(crm)
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


def _post(c):
    """Wat Bram ziet, kort: de berichten die om actie vragen (één regel per bericht, zoals Bram ze samenvat)."""
    from .bram import sync, web  # pylint: disable=import-outside-toplevel
    if not sync.koppelingen(c, alleen_actief=True):
        return "Post: nog geen mailaccounts gekoppeld bij Bram."
    rijen = c.execute("""SELECT b.*, k.naam AS koppeling FROM post_berichten b JOIN post_koppelingen k ON k.id = b.koppeling_id
                         WHERE b.status = 'open' AND k.actief ORDER BY b.ontvangen DESC""").fetchall()
    actie = [r for r in rijen if web.groep(r) == "actie"]
    uit = [f"Post (van Bram): {len(rijen)} berichten in de inboxen, {len(actie)} vragen om actie."]
    uit += [f"- {r['van_naam']}: {r['regel'] or r['onderwerp']} ({r['koppeling']}, mail #{r['id']})" for r in actie[:10]]
    overig = [r for r in rijen if web.groep(r) != "actie"][:15]
    if overig:
        uit.append("Andere mails in de inbox:")
        uit += [f"- {r['van_naam']}: {r['onderwerp']} ({r['koppeling']}, mail #{r['id']})" for r in overig]
    return "\n".join(uit)


def _taken(c, nu):
    """Wat Noor ziet, kort: wat vandaag om aandacht vraagt en op wie Martijn wacht."""
    from .taken import noor  # pylint: disable=import-outside-toplevel
    o = noor.vandaag_overzicht(c, nu)
    if not o["open"]:
        return "Taken (van Noor): geen open taken."
    tel = ", ".join(f"{n} {s}" for s, n in o["tel"].items() if n)
    uit = [f"Taken (van Noor): {o['open']} open ({tel})."]
    uit += [f"- {t['titel']} ({t['reden']}, prio {t['prio']})" for t in o["lijst"]]
    if o["wachtend"]:
        uit.append("Martijn wacht op: " + ", ".join(o["wachtend"]) + ".")
    return "\n".join(uit)


def _nieuws(c, nu):
    """Wat Vera ziet, kort: de belangrijkste koppen van vandaag, met bron."""
    from .vera import nieuws  # pylint: disable=import-outside-toplevel
    try:
        koppen = nieuws.koppen(c, nu, 6)
    except Exception:  # noqa: BLE001  (nieuws is een extra; het gesprek gaat altijd door)
        return ""
    if not koppen:
        return "Nieuws (van Vera): nog geen belangrijk nieuws vandaag."
    return "Nieuws (van Vera), de belangrijkste koppen:\n" + "\n".join(
        f"- [{nieuws.SOORT_NAMEN[b['soort']]}] {b['titel']} ({b['bron']}){': ' + b['waarom'] if b['waarom'] else ''}" for b in koppen)


def _socials(c):
    """Wat Jesse hoort, kort: reacties op Martijns posts en de interessantste berichten over keepers en Helmond."""
    from .jesse import luisteren  # pylint: disable=import-outside-toplevel
    try:
        per = luisteren.voor_vandaag(c, per=4)
    except Exception:  # noqa: BLE001  (socials zijn een extra; het gesprek gaat altijd door)
        return ""
    regels = [f"- [{luisteren.STROOM_NAMEN[s]}] {b['platformnaam']}, {b['auteur'] or 'onbekend'}: {b['titel'][:140]}"
              + (f" ({b['toon']})" if b["toon"] else "") + (f": {b['waarom']}" if b["waarom"] else "")
              for s, lijst in per.items() for b in lijst]
    return "Socials (van Jesse, die meeluistert):\n" + ("\n".join(regels) or "- niets bijzonders")


def _crm(c, nu):
    """Wat er in het CRM loopt, kort: open kansen per activiteit, volgende stappen die eraan komen, actieve projecten."""
    from .crm import model  # pylint: disable=import-outside-toplevel
    kansen = c.execute("""SELECT k.*, a.naam AS act, a.soort, o.naam AS org FROM crm_kansen k JOIN crm_activiteiten a ON a.code = k.activiteit
                          LEFT JOIN crm_organisaties o ON o.id = k.organisatie_id WHERE k.gesloten IS NULL
                          ORDER BY k.volgende_datum NULLS LAST LIMIT 25""").fetchall()
    projecten = c.execute("""SELECT p.titel, p.datum, p.status, a.naam AS act FROM crm_projecten p JOIN crm_activiteiten a ON a.code = p.activiteit
                             WHERE p.status IN ('gepland', 'actief') ORDER BY p.datum NULLS LAST LIMIT 12""").fetchall()
    if not kansen and not projecten:
        return "CRM: nog geen open kansen of projecten."
    uit = ["CRM, open kansen (titel, activiteit, fase, organisatie, waarde, volgende stap):"]
    uit += [f"- {k['titel']} ({k['act']}, {model.fase_naam(k['soort'], k['fase'])}{', ' + k['org'] if k['org'] else ''}"
            f"{', € ' + str(k['waarde']) if k['waarde'] is not None else ''})"
            + (f": {k['volgende_stap']}" + (f" op {k['volgende_datum']:%d-%m}" if k['volgende_datum'] else "") if k['volgende_stap'] else "")
            + (" [over datum]" if model.achter(k, nu.date()) else "") for k in kansen]
    if projecten:
        uit.append("Lopende projecten:")
        uit += [f"- {p['titel']} ({p['act']}, {p['status']}{', ' + p['datum'].strftime('%d-%m-%Y') if p['datum'] else ''})" for p in projecten]
    return "\n".join(uit)


def antwoord(c, gesprek_id):
    berichten = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id",
                          (gesprek_id,)).fetchall()
    from .crm import voorstel  # pylint: disable=import-outside-toplevel
    systeem = (personas.systeemtekst(personas.get("iris")) + "\n\n" + voorstel.uitleg_voor_iris(c)
               + "\n\n# Wat je nu weet\n" + context(c))
    tekst = claude.gesprek(c, "iris", systeem, [{"role": b["rol"], "content": b["tekst"]} for b in berichten if (b["tekst"] or "").strip()][-30:],
                           max_tokens=1500)
    tekst = tekst.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    tekst, voorstellen = voorstel.uit_antwoord(c, tekst)
    bid = c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s) RETURNING id",
                    (gesprek_id, tekst or "Hieronder staat mijn voorstel.")).fetchone()["id"]
    for v in voorstellen:
        c.execute("INSERT INTO crm_voorstellen (gesprek_id, bericht_id, gegevens) VALUES (%s, %s, %s)",
                  (gesprek_id, bid, json.dumps(v, ensure_ascii=False)))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst
