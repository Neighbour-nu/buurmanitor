"""Kansen en projecten: fases, gewogen waarde, de volgende stap als taak, en sjablonen. Waar het kan zonder database
(pure functies), zodat het te testen is."""
import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation

# Fases per soort activiteit: (code, naam, uitkomst). uitkomst: '' (nog open), 'gewonnen' of 'verloren'.
FASES = {
    "commercieel": (("signaal", "Signaal", ""), ("contact", "Contact", ""), ("voorstel", "Voorstel", ""),
                    ("onderhandeling", "Onderhandeling", ""), ("gewonnen", "Gewonnen", "gewonnen"), ("verloren", "Verloren", "verloren")),
    "initiatief": (("idee", "Idee", ""), ("verkennen", "Verkennen", ""), ("partners", "Partners zoeken", ""),
                   ("voorbereiding", "Voorbereiding", ""), ("gestart", "Gestart", "gewonnen"), ("gestopt", "Gestopt", "verloren")),
}
STANDAARD_KANS = {"signaal": 10, "contact": 25, "voorstel": 50, "onderhandeling": 75, "gewonnen": 100, "verloren": 0}
PROJECTSTATUS = (("idee", "Idee"), ("gepland", "Gepland"), ("actief", "Actief"), ("afgerond", "Afgerond"), ("gestopt", "Gestopt"))
BRONNEN = ("mail", "nieuws", "social", "handmatig")


def fases(soort):
    return FASES.get(soort, FASES["commercieel"])


def fase_naam(soort, code):
    return next((n for c, n, _ in fases(soort) if c == code), code)


def uitkomst(soort, code):
    return next((u for c, _, u in fases(soort) if c == code), "")


def eerste_fase(soort):
    return fases(soort)[0][0]


def bedrag(tekst):
    """'€ 1.250,50' of '1250.5' -> Decimal('1250.50'); leeg of onzin -> None. Pure functie."""
    t = re.sub(r"[€\s]", "", str(tekst or ""))
    if not t:
        return None
    if "," in t:  # Nederlands: punt voor duizendtallen, komma voor centen
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):  # 12.500: een punt als duizendtal
        t = t.replace(".", "")
    try:
        return Decimal(t).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def datum(tekst):
    try:
        return dt.date.fromisoformat(str(tekst or "")[:10])
    except ValueError:
        return None


def geheel(tekst, laag=None, hoog=None):
    try:
        n = int(str(tekst or "").strip())
    except ValueError:
        return None
    if laag is not None and n < laag or hoog is not None and n > hoog:
        return None
    return n


def gewogen(k):
    """Waarde maal kans (voor de prognose). Pure functie."""
    if k.get("waarde") is None:
        return Decimal(0)
    kans = k.get("kans")
    if kans is None:
        kans = STANDAARD_KANS.get(k.get("fase"), 0)
    return (Decimal(k["waarde"]) * kans / 100).quantize(Decimal("0.01"))


def achter(k, vandaag):
    """Is de volgende stap over datum (en de kans nog open)? Pure functie."""
    return bool(not k.get("gesloten") and k.get("volgende_datum") and k["volgende_datum"] < vandaag)


def sjabloon_regels(tekst):
    """De vaste taken van een sjabloon uit het tekstvak: één taak per regel, met daarachter het aantal dagen voor (-) of
    na (+) de projectdatum, bijvoorbeeld 'Locatie vastleggen | -120'. Zonder getal: op de projectdatum. Pure functie."""
    uit = []
    for regel in str(tekst or "").splitlines():
        regel = regel.strip(" -•\t")
        if not regel:
            continue
        titel, _, dagen = regel.rpartition("|") if "|" in regel else (regel, "", "")
        n = geheel(dagen.strip().replace("+", ""), -3650, 3650)
        uit.append({"titel": (titel if "|" in regel else regel).strip()[:200], "dagen": n if n is not None else 0})
    return [t for t in uit if t["titel"]][:100]


def sjabloon_tekst(taken):
    """Andersom: de taken van een sjabloon als tekst voor het tekstvak. Pure functie."""
    return "\n".join(f"{t['titel']} | {t['dagen']:+d}" if t.get("dagen") else t["titel"] for t in taken or [])


def taken_uit_sjabloon(taken, projectdatum):
    """De taken voor een nieuw project: met deadline = projectdatum + dagen (zonder projectdatum: zonder deadline).
    Pure functie."""
    return [{"titel": t["titel"], "einddatum": projectdatum + dt.timedelta(days=t["dagen"]) if projectdatum else None}
            for t in taken]


def taken_als_sjabloon(taken, projectdatum):
    """Een sjabloon maken van de taken van een project: dagen ten opzichte van de projectdatum. Pure functie."""
    uit = []
    for t in sorted(taken, key=lambda t: (t.get("einddatum") or dt.date.max, t["titel"])):
        dagen = (t["einddatum"] - projectdatum).days if projectdatum and t.get("einddatum") else 0
        uit.append({"titel": t["titel"], "dagen": dagen})
    return uit


# ---------------------------------------------------------------- database

def activiteiten(c, alleen_actief=True):
    return c.execute("SELECT * FROM crm_activiteiten" + (" WHERE actief" if alleen_actief else "") + " ORDER BY volgorde, naam").fetchall()


def soort_van(c, code):
    r = c.execute("SELECT soort FROM crm_activiteiten WHERE code = %s", (code,)).fetchone()
    return r["soort"] if r else "commercieel"


def tijdlijn(c, soort, titel="", tekst="", url="", kans_id=None, project_id=None, bericht_id=None):
    c.execute("""INSERT INTO crm_tijdlijn (kans_id, project_id, soort, titel, tekst, url, bericht_id) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
              (kans_id, project_id, soort, titel[:300], tekst[:4000], url[:1000], bericht_id))


def volgende_stap_taak(c, k):
    """De volgende stap van een kans als taak op het takenbord (bij Noor): maken, bijwerken of afronden. Geeft het
    taak-id of None."""
    open_ = not k.get("gesloten") and (k.get("volgende_stap") or "").strip()
    t = c.execute("SELECT * FROM takenbord WHERE id = %s", (k["taak_id"],)).fetchone() if k.get("taak_id") else None
    if not open_:
        if t and t["status"] != "afgerond":
            c.execute("UPDATE takenbord SET status = 'afgerond', afgerond = now(), bijgewerkt = now() WHERE id = %s", (t["id"],))
            agenda([t["id"]])
        return k.get("taak_id")
    titel = f"{k['volgende_stap'].strip()[:150]} ({k['titel'][:60]})"
    oms = f"Volgende stap voor de kans '{k['titel']}'. Zie /crm/kans/{k['id']}."
    if t and t["status"] != "afgerond":
        c.execute("""UPDATE takenbord SET titel = %s, einddatum = %s, beschrijving = %s, bijgewerkt = now() WHERE id = %s""",
                  (titel, k.get("volgende_datum"), oms, t["id"]))
        if (t["titel"], t["einddatum"]) != (titel, k.get("volgende_datum")):
            agenda([t["id"]])
        return t["id"]
    tid = c.execute("""INSERT INTO takenbord (titel, beschrijving, status, prio, einddatum, kans_id, project_id)
                       VALUES (%s, %s, 'gepland', 'middel', %s, %s, %s) RETURNING id""",
                    (titel, oms, k.get("volgende_datum"), k["id"], k.get("project_id"))).fetchone()["id"]
    c.execute("UPDATE crm_kansen SET taak_id = %s WHERE id = %s", (tid, k["id"]))
    agenda([tid])
    return tid


def sjabloon_toepassen(c, project, sjabloon):
    """De vaste taken van een sjabloon bij een project op het takenbord zetten. Geeft het aantal."""
    ids = []
    for t in taken_uit_sjabloon(sjabloon["taken"] or [], project.get("datum")):
        ids.append(c.execute("""INSERT INTO takenbord (titel, beschrijving, status, prio, einddatum, project_id)
                                VALUES (%s, %s, 'gepland', 'middel', %s, %s) RETURNING id""",
                             (t["titel"], f"Uit het sjabloon '{sjabloon['naam']}' voor {project['titel']}.", t["einddatum"],
                              project["id"])).fetchone()["id"])
    agenda(ids)
    return len(ids)


def agenda(tids, weg=None):
    """De deadlines van deze taken (ook) in de agenda zetten of bijwerken, op de achtergrond (zoals bij Noor)."""
    from ..taken import web as taken_web  # pylint: disable=import-outside-toplevel
    taken_web.agenda_op_achtergrond(tids, weg)


def json_dump(x):
    return json.dumps(x, ensure_ascii=False, default=str)
