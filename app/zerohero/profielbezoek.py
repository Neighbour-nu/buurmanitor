"""Profielbezoekers van LinkedIn (Premium): maandelijks een screenshot van 'Wie heeft uw profiel bekeken'.

Er is geen export. De cockpit leest uit de screenshot: de gekozen periode, het aantal profielbezoekers en de verandering
die LinkedIn erbij zet. Claude leest af (zelfde sleutel als bij de SSI), jij controleert, de screenshot wordt bewaard.
Bij een periode van hooguit 31 dagen telt het aantal ook als meting 'profielweergaven' van kanaal linkedin, zodat het
op het dashboard per periode wordt opgeteld. Langere periodes worden alleen als stand bewaard.
"""
import datetime as dt
import re

from . import ssi

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS lp_profielbezoek (
        datum date NOT NULL, periode_dagen int NOT NULL, periode text, bezoekers int, verandering numeric,
        vergelijking text, screenshot_id int, bron text, bijgewerkt timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (datum, periode_dagen))""",
]

VELDEN = [
    ("periode", "Gekozen periode (zoals op de knop, bijv. Afgelopen 28 dagen)", False),
    ("bezoekers", "Aantal profielbezoekers", True),
    ("verandering", "Verandering in % (min voor een daling)", True),
    ("vergelijking", "Vergeleken met (zoals LinkedIn het noemt)", False),
]

PROMPT = """Dit is een screenshot van de LinkedIn-pagina Profielbezoekers (Wie heeft uw profiel bekeken), mogelijk in het Nederlands.
Geef ALLEEN een JSON-object terug, zonder uitleg, met deze sleutels:
periode (de tekst op de groene periodeknop, bijvoorbeeld "Afgelopen 365 dagen"), bezoekers (het grote getal boven
"Profielbezoekers", als geheel getal: 1.792 wordt 1792), verandering (het percentage, negatief als het pijltje omlaag
wijst of rood is: "▼ 11%" wordt -11), vergelijking (de tekst na het percentage, bijvoorbeeld "in vergelijking met de
voorgaande 7 dagen"). Gebruik null als iets niet op de screenshot staat."""


def dagen_uit(periode):
    t = (periode or "").lower()
    m = re.search(r"(\d+)\s*(dag|day|week|weken|weeks|maand|maanden|month)", t)
    if not m:
        return None
    n, eenheid = int(m.group(1)), m.group(2)
    return n * 7 if eenheid.startswith("we") else n * 30 if eenheid.startswith(("maand", "month")) else n


def lees(data, mime):
    gelezen, fout = ssi.lees(data, mime, PROMPT, VELDEN)
    if gelezen.get("bezoekers") is not None:
        gelezen["bezoekers"] = int(float(str(gelezen["bezoekers"]).replace(".", "").replace(",", ".")))
    return gelezen, fout


def opslaan(q, datum, velden, screenshot_id, bron):
    periode = (str(velden.get("periode") or "").strip()) or None
    n = dagen_uit(periode)
    if not n:
        raise ValueError("Uit de periode kon geen aantal dagen worden gehaald. Schrijf bijvoorbeeld: Afgelopen 28 dagen.")
    bezoekers = ssi._num(velden.get("bezoekers"))
    if bezoekers is None:
        raise ValueError("Vul het aantal profielbezoekers in.")
    verandering = ssi._num(velden.get("verandering"))
    q("""INSERT INTO lp_profielbezoek (datum, periode_dagen, periode, bezoekers, verandering, vergelijking, screenshot_id, bron, bijgewerkt)
         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
         ON CONFLICT (datum, periode_dagen) DO UPDATE SET periode = EXCLUDED.periode, bezoekers = EXCLUDED.bezoekers,
           verandering = EXCLUDED.verandering, vergelijking = EXCLUDED.vergelijking,
           screenshot_id = coalesce(EXCLUDED.screenshot_id, lp_profielbezoek.screenshot_id), bron = EXCLUDED.bron, bijgewerkt = now()
         RETURNING 1""", (datum, n, periode, int(bezoekers), verandering, (str(velden.get("vergelijking") or "").strip() or None),
                          screenshot_id, bron))
    q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES ('linkedin', %s, %s, %s, 'screenshot')
         ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'screenshot', bijgewerkt = now()
         RETURNING 1""", (f"profielbezoekers_{n}d", datum, int(bezoekers)))
    if n <= 31:
        q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES ('linkedin', 'profielweergaven', %s, %s, 'screenshot')
             ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'screenshot', bijgewerkt = now()
             RETURNING 1""", (datum, int(bezoekers)))
    return n


def overzicht(q):
    rijen = q("SELECT * FROM lp_profielbezoek ORDER BY datum, periode_dagen")
    for r in rijen:
        r["per_dag"] = round(r["bezoekers"] / r["periode_dagen"], 1) if r["bezoekers"] is not None else None
    laatste = rijen[-1] if rijen else None
    nodig = not rijen or (dt.date.today() - max(r["datum"] for r in rijen)).days >= 30
    uit = {"rijen": rijen, "laatste": laatste, "nodig": nodig, "actief": ssi.actief(), "inzichten": []}
    if laatste:
        uit["inzichten"].append(f"{laatste['bezoekers']:,}".replace(",", ".") + f" profielbezoekers in {laatste['periode'].lower() if laatste['periode'] else str(laatste['periode_dagen']) + ' dagen'}, "
                                f"gemiddeld {str(laatste['per_dag']).replace('.', ',')} per dag.")
        zelfde = [r for r in rijen if r["periode_dagen"] == laatste["periode_dagen"] and r is not laatste]
        if zelfde:
            v = zelfde[-1]
            verschil = laatste["bezoekers"] - v["bezoekers"]
            uit["inzichten"].append(f"Vorige meting met dezelfde periode ({v['datum'].strftime('%d-%m-%Y')}): {v['bezoekers']}; "
                                    f"{'+' if verschil >= 0 else ''}{verschil}.")
        if laatste["periode_dagen"] > 31:
            uit["inzichten"].append("Kies voortaan bij voorkeur Afgelopen 28 dagen (of 30): dan sluiten de maandelijkse metingen op elkaar aan "
                                    "en telt het dashboard ze per periode op. Een meting over 365 dagen wordt alleen als stand bewaard.")
    return uit
