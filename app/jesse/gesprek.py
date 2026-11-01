"""Praten met Jesse over je socials. Jesse kent de cijfers per kanaal, de laatste posts met hun resultaten, de
concepten die klaarstaan en de belangrijkste keepers- en politieke koppen van Vera. Hij denkt mee over wat werkt en
schrijft voorstellen voor posts. Die verschijnen als kaartje om te kopiëren; Jesse post, reageert of liket nooit zelf.
"""
import datetime as dt
import json
import re

from .. import personas
from ..kern import claude, config
from ..kern.web import datum_lang
from . import socials

TAG = re.compile(r"<post>\s*(\{.*?\})\s*</post>", re.S)
KANAAL_NAMEN = {"linkedin": "LinkedIn", "instagram": "Instagram", "facebook": "Facebook", "threads": "Threads", "x": "X", "bluesky": "Bluesky"}
VOOR = {"zerohero": "ZEROHERO", "persoonlijk": "persoonlijk", "politiek": "PRO Helmond", "fbim": "FB/IM"}
UITLEG = """# Wat je voor Martijn doet
Je denkt mee over zijn socials: wat werkte, wat niet, wat hij kan posten en wanneer. Noem cijfers alleen als ze
hieronder staan; verzin geen cijfers. Ontbreekt iets (bijvoorbeeld omdat een kanaal niet gekoppeld is of de export
oud is), zeg dat dan.

Een voorstel voor een post zet je als één regel aan het eind van je antwoord (Martijn ziet het als kaartje om te kopiëren):
<post>{"kanaal": "linkedin|instagram|threads|facebook", "voor": "zerohero|persoonlijk|politiek|fbim", "tekst": "..."}</post>
Schrijf in de stem van Martijn: helder, kort, op taalniveau B2, Nederlands tenzij hij iets anders vraagt. Gebruik nooit
gedachtestreepjes; gebruik een dubbele punt, komma, haakjes of een nieuwe zin. Zet hashtags alleen waar ze op dat
kanaal gebruikelijk zijn (Instagram ruim, LinkedIn een paar, Threads weinig). Verzin geen feiten, citaten of cijfers; laat
een [plek] open als Martijn iets moet aanvullen.
ZEROHERO gaat over keepers en de keeper-mindset in alle keeperssporten. Politieke posts (PRO Helmond) zijn zakelijk en
voorzichtig: geen aanvallen op personen, en bij gevoelige onderwerpen eerst context. Op politieke kanalen reageer je
nooit namens hem; je schrijft hooguit een voorstel dat hij zelf plaatst.
Zeg nooit dat je iets gepost, ingepland of gestuurd hebt: dat doet Martijn zelf."""


def voorstellen_uit(tekst):
    """De postvoorstellen uit het antwoord. Geeft (tekst zonder de regels, lijst). Pure functie."""
    uit = []
    for m in TAG.finditer(tekst):
        try:
            v = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(v, dict):
            continue
        t = str(v.get("tekst") or "").replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").strip()
        kanaal = str(v.get("kanaal") or "").lower()
        if not t or kanaal not in KANAAL_NAMEN:
            continue
        voor = str(v.get("voor") or "").lower()
        uit.append({"kanaal": kanaal, "voor": voor if voor in VOOR else "", "tekst": t[:5000]})
    return TAG.sub("", tekst).strip(), uit


def _n(v):
    return "?" if v is None else f"{v:,}".replace(",", ".") if isinstance(v, int) else str(v)


def context(c, nu=None):
    nu = nu or dt.datetime.now(config.TZ)
    o7, o30 = socials.overzicht(7), socials.overzicht(30)
    delen = [f"Het is {datum_lang(nu.date())} {nu.year}, {nu:%H.%M} uur."]
    if o7["kanalen"] is None:
        delen.append("De Doellijn (werkruimte ZEROHERO) draait hier niet; je hebt dus geen cijfers of posts. Zeg dat.")
    else:
        regels = []
        for k7, k30 in zip(o7["kanalen"], o30["kanalen"] or o7["kanalen"]):
            if not k7["heeft_cijfers"]:
                regels.append(f"- {k7['volledig']}: nog geen cijfers")
                continue
            r = ", ".join(f"{x['label'].lower()} {_n(x['waarde'])}" + (f" ({'+' if (x['delta'] or 0) >= 0 else ''}{_n(x['delta'])})" if x["delta"] is not None else "")
                          + (f", gemeten {x['peil']:%d-%m}" if x.get("peil") and x["soort"] == "stand" else "")
                          for x in k7["regels"] if x["waarde"] is not None)
            r30 = ", ".join(f"{x['label'].lower()} {_n(x['waarde'])}" for x in k30["regels"] if x["waarde"] is not None and x["soort"] == "periode")
            regels.append(f"- {k7['volledig']}: laatste 7 dagen {r or 'geen cijfers'}" + (f"; laatste 30 dagen {r30}" if r30 else ""))
        delen.append("# Kanalen (verschil met de periode ervoor tussen haakjes)\n" + "\n".join(regels))
        posts = o7["posts"] or []
        delen.append("# Posts van de laatste twee weken\n" + ("\n".join(
            f"- {p['tijd']:%d-%m} {p['naam']}: {(p['tekst'] or '(tekst niet beschikbaar)')[:160]} · weergaven {_n(p['weergaven'])}, interacties {_n(p['interacties'])}"
            for p in posts[:25] if p["tijd"]) or "(geen)"))
        if o7["pijplijn"]:
            delen.append("# Concepten in de Doellijn die klaarstaan om te posten\n" + "\n".join(
                f"- [{p['id']}] {p['titel']}" + (f" (LinkedIn-tekst: {p['linkedin'][:200]})" if p["linkedin"] else "") for p in o7["pijplijn"]))
    if o7["vermeldingen"]:
        delen.append("# PRO Helmond of Martijn in het nieuws (laatste twee weken)\n" + "\n".join(
            f"- {v['tijd']:%d-%m} {v['titel']} ({v['bron']})" for v in o7["vermeldingen"]))
    try:
        from ..vera import nieuws  # pylint: disable=import-outside-toplevel
        koppen = nieuws.koppen(c, nu, 8)
        if koppen:
            delen.append("# Belangrijk nieuws van vandaag (van Vera), als inspiratie\n" + "\n".join(
                f"- [{nieuws.SOORT_NAMEN[b['soort']]}] {b['titel']} ({b['bron']})" for b in koppen))
    except Exception:  # noqa: BLE001  (nieuws is een extra)
        pass
    return "\n\n".join(delen)


def antwoord(c, gesprek_id, nu=None):
    historie = c.execute("SELECT rol, tekst FROM gesprek_berichten WHERE gesprek_id = %s ORDER BY id", (gesprek_id,)).fetchall()
    systeem = "\n\n".join([personas.systeemtekst(personas.get("jesse")), UITLEG, context(c, nu)])
    tekst = claude.gesprek(c, "jesse", systeem, [{"role": x["rol"], "content": x["tekst"]} for x in historie if (x["tekst"] or "").strip()][-20:],
                           max_tokens=2500)
    tekst = tekst.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    tekst, voorstellen = voorstellen_uit(tekst)
    if not tekst:
        tekst = "Hier is mijn voorstel." if voorstellen else "(Geen antwoord ontvangen. Probeer het nog eens.)"
    c.execute("INSERT INTO gesprek_berichten (gesprek_id, rol, tekst) VALUES (%s, 'assistant', %s)", (gesprek_id, tekst))
    for v in voorstellen[:6]:
        c.execute("INSERT INTO jesse_voorstellen (gesprek_id, kanaal, voor, tekst) VALUES (%s, %s, %s, %s)",
                  (gesprek_id, v["kanaal"], v["voor"], v["tekst"]))
    c.execute("UPDATE gesprekken SET bijgewerkt = now() WHERE id = %s", (gesprek_id,))
    return tekst
