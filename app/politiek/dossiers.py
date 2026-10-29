"""Dossierkaarten (M9): per onderwerp het hele verhaal op één plek.

Een dossier is een naam met zoektermen. Promonitor verzamelt daarmee wat het al weet: raadsstukken en nieuws
uit de Inbox, eigen raadsvragen, agendapunten en woordvoering, concepten, besluiten en acties uit de fractie,
en passages uit de kennisbank. Op verzoek vat Claude dat samen: stand van zaken, standpunt (alleen als dat
uit de bronnen blijkt), open vragen en mogelijke vervolgstappen.
"""
import datetime as dt
import json
import re
import threading
import traceback

from . import claude, kennisbank, nieuws, raadsinfo, whaller


def termen(tekst):
    """Zoektermen uit een veld: gescheiden door komma's of regels."""
    return [t.strip() for t in re.split(r"[,\n;]+", tekst or "") if len(t.strip()) >= 3][:20]


def _patronen(d):
    return [f"%{t}%" for t in termen(d["zoektermen"]) or [d["naam"]]]


def _datum(x):
    if isinstance(x, dt.datetime):
        return x.date()
    return x


def verzamel(c, d):
    """Alles wat Promonitor over dit dossier weet, als tijdlijn plus losse lijsten."""
    pat = _patronen(d)
    stukken = c.execute("""SELECT id, bron, soort, titel, nummer, datum, context, reden, document_url, pro, bronnaam,
                                  score, status, toegevoegd
                           FROM stukken
                           WHERE status <> 'genegeerd' AND soort NOT IN ('bijlage', 'besloten')
                             AND (bron <> 'nieuws' OR coalesce(score, 0) >= 40)
                             AND (titel ILIKE ANY(%s) OR coalesce(context, '') ILIKE ANY(%s)
                                  OR coalesce(onderwerp, '') ILIKE ANY(%s) OR coalesce(reden, '') ILIKE ANY(%s))
                           ORDER BY coalesce(datum, toegevoegd::date) DESC LIMIT 80""", (pat, pat, pat, pat)).fetchall()
    concepten = c.execute("""SELECT id, soort, titel, status, aangemaakt FROM concepten
                             WHERE titel ILIKE ANY(%s) OR coalesce(bron->>'onderwerp', '') ILIKE ANY(%s)
                             ORDER BY id DESC LIMIT 30""", (pat, pat)).fetchall()
    toew = c.execute("""SELECT soort, datum, onderwerp, woordvoerder, zit_erbij FROM toewijzingen
                        WHERE onderwerp ILIKE ANY(%s) ORDER BY datum DESC LIMIT 30""", (pat,)).fetchall()
    voortgang = c.execute("""SELECT onderwerp, gedaan, notitie FROM agenda_voortgang WHERE onderwerp ILIKE ANY(%s)""",
                          (pat,)).fetchall()
    acties = c.execute("""SELECT a.*, n.datum FROM fractie_acties a LEFT JOIN notulen n ON n.id = a.notulen_id
                          WHERE a.actie ILIKE ANY(%s) OR coalesce(a.punt, '') ILIKE ANY(%s)
                          ORDER BY a.id DESC LIMIT 40""", (pat, pat)).fetchall()
    besluiten = []
    laag = [p.strip("%").lower() for p in pat]
    for n in c.execute("SELECT id, datum, status, inhoud FROM notulen WHERE inhoud IS NOT NULL ORDER BY datum DESC").fetchall():
        for p in (n["inhoud"] or {}).get("punten", []):
            for b in p.get("besluiten") or []:
                tekst = (b + " " + (p.get("titel") or "")).lower()
                if any(t in tekst for t in laag):
                    besluiten.append({"datum": n["datum"], "notulen_id": n["id"], "punt": p.get("titel") or "", "besluit": b})
    zoekvraag = " or ".join(f'"{t}"' if " " in t else t for t in termen(d["zoektermen"]) or [d["naam"]])
    documenten = kennisbank.zoeken(lambda sql, p=None: c.execute(sql, p).fetchall(), zoekvraag, limiet=8)

    naam_van = {p["whaller"]: p["naam"] for p in c.execute("SELECT naam, whaller FROM personen WHERE whaller IS NOT NULL").fetchall()}
    tijdlijn = []
    for s in stukken:
        tijdlijn.append({"datum": _datum(s["datum"] or s["toegevoegd"]),
                         "soort": "Nieuws" if s["bron"] == "nieuws" else raadsinfo.SOORTEN.get(s["soort"], s["soort"]),
                         "titel": s["titel"], "url": s["document_url"], "pro": s["pro"],
                         "extra": s["reden"] or s["context"] or s["bronnaam"] or ""})
    for t in toew:
        namen = " en ".join(naam_van.get(x, x) for x in (t["woordvoerder"], t["zit_erbij"]) if x)
        tijdlijn.append({"datum": t["datum"], "soort": "Commissie" if t["soort"] == "commissie" else "Raad",
                         "titel": t["onderwerp"], "url": f"/agenda#p-{whaller.sleutel(t['onderwerp']).replace(' ', '-')[:60]}",
                         "pro": True, "extra": f"Woordvoering: {namen}" if namen else ""})
    for b in besluiten:
        tijdlijn.append({"datum": b["datum"], "soort": "Fractiebesluit", "titel": b["besluit"],
                         "url": f"/fractie/notulen/{b['notulen_id']}", "pro": True, "extra": b["punt"]})
    for k in concepten:
        tijdlijn.append({"datum": _datum(k["aangemaakt"]), "soort": "Concept", "titel": k["titel"],
                         "url": f"/concepten/{k['id']}", "pro": True, "extra": k["soort"].replace("_", " ")})
    tijdlijn.sort(key=lambda x: x["datum"] or dt.date.min, reverse=True)
    vragen = [s for s in stukken if s["soort"] == "vraag" and s["pro"]]
    return {"tijdlijn": tijdlijn, "stukken": stukken, "concepten": concepten, "toewijzingen": toew,
            "voortgang": voortgang, "acties": acties, "besluiten": besluiten, "documenten": documenten,
            "vragen": vragen}


OPDRACHT = """Maak een dossierkaart voor dit onderwerp, voor een raadslid van de oppositie (PRO Helmond).
Gebruik alleen wat hieronder staat. Verzin geen feiten, data of standpunten. Schrijf op taalniveau B2, zonder gedachtestreepjes.

Antwoord met alleen één blok ```json:
{"samenvatting": "drie tot vijf zinnen: waar gaat dit dossier over en waarom doet het ertoe voor Helmonders",
 "stand_van_zaken": "waar staat het nu: laatste stukken, besluiten en wat eraan komt (met data)",
 "standpunt": "het standpunt van PRO Helmond zoals dat uit de bronnen blijkt (bijdragen, fractiebesluiten, programma); leeg als het niet blijkt",
 "coalitie": "wat het college of de coalitie heeft beloofd of besloten, met bron; leeg als onbekend",
 "open_vragen": ["vragen die nog open staan of die we zouden moeten stellen"],
 "kansen": ["kansen voor PRO Helmond: bewaken, vragen, motie, samenwerking"],
 "volgende_stappen": ["concrete vervolgstap met het passende instrument"],
 "bronnen": ["de belangrijkste bronnen die je gebruikte"]}"""


def analyseer(c, did, vertrouwelijk=False):
    d = c.execute("SELECT * FROM dossiers WHERE id = %s", (did,)).fetchone()
    claude.schrijfplafond(c)
    v = verzamel(c, d)
    delen = [f"# Dossier: {d['naam']}\nZoektermen: {d['zoektermen'] or d['naam']}"]
    if d["standpunt"]:
        delen.append(f"# Standpunt zoals Martijn het vastlegde\n{d['standpunt']}")
    if d["notitie"]:
        delen.append(f"# Notities van Martijn\n{d['notitie']}")
    if v["tijdlijn"]:
        delen.append("# Tijdlijn (nieuwste eerst)\n" + "\n".join(
            f"- {t['datum'] or '?'} | {t['soort']} | {t['titel']}" + (f" | {t['extra'][:300]}" if t["extra"] else "")
            for t in v["tijdlijn"][:60]))
    if v["acties"]:
        delen.append("# Acties uit de fractie\n" + "\n".join(
            f"- {a['actie']} ({a['wie'] or '?'}, status {a['status']})" for a in v["acties"][:20]))
    if v["voortgang"]:
        delen.append("# Notities bij agendapunten\n" + "\n".join(
            f"- {r['onderwerp']}: {r['notitie']}" for r in v["voortgang"] if r["notitie"]))
    for k in c.execute("""SELECT soort, titel, tekst FROM concepten WHERE status = 'klaar' AND id = ANY(%s)
                          ORDER BY id DESC LIMIT 3""", ([k["id"] for k in v["concepten"]] or [0],)).fetchall():
        tekst = re.sub(r"^#+\s*", "", (k["tekst"] or "")[:4000], flags=re.M)  # geen koppen binnen een kop
        delen.append(f"# Concept ({k['soort']}): {k['titel']}\n{tekst}")
    kb = kennisbank.context(lambda sql, p=None: c.execute(sql, p).fetchall(),
                            d["naam"] + " " + (d["zoektermen"] or ""), maximum=20000, documenten=8,
                            vertrouwelijk=vertrouwelijk)
    if kb:
        delen.append("# Uit de kennisbank (programma, coalitieakkoord, begroting, bijdragen, raadsstukken)\n" + kb)
    delen.append("# Opdracht\n" + OPDRACHT)
    model = nieuws.instelling(c, "claude_model_schrijven", "claude-sonnet-5-5")
    profiel = nieuws.instelling(c, "nieuws_profiel", nieuws.STANDAARD_PROFIEL)
    systeem = (f"Je bent de dossierhouder van Promonitor, de politieke cockpit van een raadslid in Helmond.\n\n{profiel}\n\n"
               f"Vandaag is het {whaller.lang(dt.date.today())}.")
    antwoord = claude.vraag(c, "dossier", systeem, "\n\n".join(delen), model, max_tokens=4000, timeout=300)
    analyse = claude.json_uit(antwoord)
    analyse = json.loads(json.dumps(analyse, ensure_ascii=False).replace(" — ", ", ").replace("—", ", ").replace(" – ", ", "))
    c.execute("""UPDATE dossiers SET inzicht = %s, analyse_status = 'klaar', analyse_fout = NULL, analyse_tijd = now(),
                   model = %s WHERE id = %s""", (json.dumps(analyse, ensure_ascii=False), model, did))


def start(pool, did, vertrouwelijk=False):
    def werk():
        with pool.connection() as c:
            try:
                analyseer(c, did, vertrouwelijk)
            except Exception as e:  # noqa: BLE001
                c.execute("UPDATE dossiers SET analyse_status = 'fout', analyse_fout = %s WHERE id = %s",
                          (f"{e}\n\n{traceback.format_exc()[-800:]}", did))
    threading.Thread(target=werk, daemon=True).start()


def voorstellen(c, bestaand, maximum=8):
    """Onderwerpen die vaak terugkomen in de Inbox (afgelopen 60 dagen) en nog geen dossier zijn."""
    rijen = c.execute("""SELECT lower(onderwerp) AS o, count(*) AS n FROM stukken
                         WHERE coalesce(onderwerp, '') <> '' AND toegevoegd > now() - interval '60 days'
                           AND status <> 'genegeerd' AND coalesce(score, 60) >= 40
                         GROUP BY 1 HAVING count(*) >= 2 ORDER BY 2 DESC LIMIT 40""").fetchall()
    bekend = [b.lower() for b in bestaand]
    uit = []
    for r in rijen:
        if any(r["o"] in b or b in r["o"] for b in bekend):
            continue
        uit.append({"onderwerp": r["o"], "aantal": r["n"]})
        if len(uit) >= maximum:
            break
    return uit
