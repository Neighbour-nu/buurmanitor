"""Raadsinformatie ophalen (M2): raadsinformatiebrieven, raadsvragen en antwoorden, moties en
amendementen, toezeggingen, ingekomen stukken en de stukken bij komende vergaderingen.

Alles komt uit de openbare Notubiz-API van Helmond. Nieuwe en gewijzigde stukken komen in de Inbox;
de tekst van de belangrijkste soorten gaat ook de kennisbank in, zodat zoeken en de adviseur ze kennen.
"""
import datetime as dt
import io
import re

from psycopg.types.json import Jsonb

from . import notubiz

SOORTEN = {
    "nieuws": "Nieuws",
    "raadsvoorstel": "Raadsvoorstel",
    "rib": "Raadsinformatiebrief",
    "vraag": "Raadsvraag",
    "motie": "Motie of amendement",
    "toezegging": "Toezegging",
    "ingekomen": "Ingekomen stuk",
    "bijlage": "Bijlage bij een agendapunt",
    "besloten": "Alleen voor raadsleden",
}
BESLOTEN_UITLEG = "Stukken alleen te zien als je bent ingelogd in Notubiz."
TEKST_VAN = {"raadsvoorstel", "rib", "vraag", "motie"}   # van deze soorten gaat de tekst de kennisbank in
DATUMVELDEN = ("Datum", "Datum vraag", "Datum motie", "Datum vergadering", "Datum ontvangst")
TITELVELDEN = ("Titel", "Onderwerp")
PRO = re.compile(r"\bPRO\b|PRO Helmond|GroenLinks|PvdA", re.I)
RV = re.compile(r"^\s*(RV|raadsvoorstel)\b", re.I)


def _datum(tekst):
    try:
        return dt.datetime.strptime(tekst[:10], "%Y-%m-%d").date() if tekst else None
    except ValueError:
        return None


def stuk_uit_item(item):
    """Een Notubiz-module-item als stuk voor de Inbox."""
    v = item["velden"]
    soort = notubiz.MODULES[item["module"]]
    titel = next((v[k] for k in TITELVELDEN if v.get(k)), "") or (item["documenten"][0]["titel"] if item["documenten"] else "")
    datum = next((_datum(v[k]) for k in DATUMVELDEN if v.get(k)), None)
    # Bij vragen: het antwoord is het interessante document; anders het eerste.
    docs = sorted(item["documenten"], key=lambda d: 0 if d["label"].lower() == "document antwoord" else 1)
    fracties = v.get("Fractie(s)", "")
    extra = []
    if soort == "vraag":
        extra.append("beantwoord" if v.get("Datum antwoord") or any(d["label"].lower() == "document antwoord" for d in docs)
                     else f"antwoord verwacht {v.get('Verwachte datum afdoening', '')[:10]}".strip())
    if v.get("Type"):
        extra.append(v["Type"])
    if soort == "toezegging" and v.get("Tekst toezegging"):
        extra.append(v["Tekst toezegging"][:240])
    if v.get("Datum afdoening"):
        extra.append(f"afgedaan {v['Datum afdoening'][:10]}")
    extra_velden = None
    if soort == "vraag":
        antwoord = next((d for d in docs if d["label"].lower() == "document antwoord"), None)
        vraagdoc = next((d for d in docs if d is not antwoord), None)
        extra_velden = {"categorie": v.get("Categorie", ""), "datum_vraag": (v.get("Datum vraag") or "")[:10],
                        "verwacht": (v.get("Verwachte datum afdoening") or "")[:10],
                        "datum_antwoord": (v.get("Datum antwoord") or "")[:10],
                        "vraag_url": vraagdoc["url"] if vraagdoc else "", "antwoord_url": antwoord["url"] if antwoord else "",
                        "beantwoord": bool(antwoord or v.get("Datum antwoord"))}
    return {
        "extra": extra_velden,
        "extern_id": f"m{item['module']}-{item['id']}", "soort": soort, "titel": titel.strip(),
        "nummer": (v.get("Nummer") or v.get("Nummering") or "").strip(), "datum": datum,
        "gewijzigd": item["gewijzigd"], "fracties": fracties,
        "document_url": docs[0]["url"] if docs else "", "document_id": docs[0]["id"] if docs else None,
        "context": "; ".join(e for e in extra if e),
        "pro": bool(PRO.search(fracties) or re.search(r"\(PRO\)", titel)),
    }


def stukken_uit_vergadering(verg, soort_verg):
    """Raadsvoorstellen en bijlagen bij de agendapunten van een vergadering."""
    uit = []
    for p in verg["agenda"]:
        for d in p.get("documenten", []):
            if not d.get("id"):
                continue
            is_rv = bool(RV.match(d["titel"])) or "Voorstel" in (d.get("typen") or [])
            uit.append({
                "extern_id": f"doc-{d['id']}", "soort": "raadsvoorstel" if is_rv else "bijlage",
                "titel": d["titel"], "nummer": "", "datum": verg["start"].date() if verg["start"] else None,
                "gewijzigd": "", "fracties": "", "document_url": d["url"], "document_id": d["id"],
                "context": f"{verg['naam']} {verg['start']:%d-%m-%Y}, agendapunt {p['nummer']} {p['onderwerp']}".strip(),
                "pro": False, "vergadering": soort_verg,
            })
    return uit


def besloten_stuk(ev):
    """Een bijeenkomst waarvan Notubiz de stukken alleen aan ingelogde raadsleden laat zien."""
    return {"extern_id": f"leden-{ev['id']}", "soort": "besloten", "titel": ev["naam"],
            "nummer": "", "datum": ev["start"].date() if ev["start"] else None, "gewijzigd": "",
            "fracties": "", "document_url": ev.get("url") or notubiz.web_url(ev["type"], ev["id"]),
            "document_id": None, "pro": False,
            "context": f"{ev['start']:%d-%m-%Y %H.%M} uur{', ' + ev['plek'] if ev.get('plek') else ''}. {BESLOTEN_UITLEG}"}


def pdf_tekst(inhoud):
    from pypdf import PdfReader  # pylint: disable=import-outside-toplevel
    lezer = PdfReader(io.BytesIO(inhoud))
    return "\n\n".join(f"[pagina {i}]\n" + (p.extract_text() or "") for i, p in enumerate(lezer.pages, 1))


def verzamel(vandaag=None):
    """Alle stukken uit Notubiz (modules plus vergaderingen van twee weken terug tot acht weken vooruit)."""
    vandaag = vandaag or dt.date.today()
    stukken, fouten = [], []
    for module in notubiz.MODULES:
        try:
            stukken += [stuk_uit_item(notubiz.lees_item(i, module)) for i in notubiz.module_items(module)]
        except Exception as e:  # noqa: BLE001 - één module mag de rest niet tegenhouden
            fouten.append(f"module {module}: {e}")
    try:
        for ev in notubiz.evenementen(vandaag - dt.timedelta(days=14), vandaag + dt.timedelta(days=56)):
            try:
                if ev.get("alleen_leden"):
                    stukken.append(besloten_stuk(ev))
                elif ev["type"] == "assembly":
                    for verg in notubiz.commissieavond(ev["id"])["commissies"]:
                        stukken += stukken_uit_vergadering(verg, "commissie")
                elif ev["type"] == "meeting":
                    stukken += stukken_uit_vergadering(notubiz.vergadering(ev["id"]), ev["soort"])
            except Exception as e:  # noqa: BLE001
                if notubiz.niet_openbaar(e):  # alleen voor raadsleden: geen fout, wel een melding in de Inbox
                    stukken.append(besloten_stuk(ev))
                else:
                    fouten.append(f"{ev['naam']} {ev['start']:%d-%m-%Y}: {e}")
    except Exception as e:  # noqa: BLE001
        fouten.append(f"vergaderingen: {e}")
    # Hetzelfde document kan bij commissie en raad hangen: één keer, met de eerste context.
    gezien, uniek = set(), []
    for s in stukken:
        if s["extern_id"] not in gezien:
            gezien.add(s["extern_id"])
            uniek.append(s)
    return uniek, fouten


def synchroniseer(c, log=print):
    """Werkt de tabel stukken bij. c is een psycopg-verbinding (autocommit, dict_row).
    Geeft (nieuw, bijgewerkt, fouten) terug."""
    stukken, fouten = verzamel()
    grens = dt.date.today() - dt.timedelta(days=14)
    # Per groep (module of vergaderstukken) kijken of het de eerste keer is: een module die de eerste keer
    # mislukte, mag de tweede keer niet al zijn oude stukken als nieuw in de Inbox zetten.
    groepen = {g["groep"] for g in c.execute(
        "SELECT DISTINCT split_part(extern_id, '-', 1) AS groep FROM stukken WHERE bron = 'notubiz'").fetchall()}
    nieuw = bijgewerkt = 0
    for s in stukken:
        oud = c.execute("SELECT id, gewijzigd, extra FROM stukken WHERE extern_id = %s", (s["extern_id"],)).fetchone()
        if oud is None:
            # Bij de eerste keer is alles ouder dan twee weken al gezien; anders loopt de Inbox vol.
            eerste_keer = s["extern_id"].split("-", 1)[0] not in groepen
            gezien = eerste_keer and (s["datum"] is None or s["datum"] < grens)
            c.execute("""INSERT INTO stukken (bron, extern_id, soort, titel, nummer, datum, gewijzigd, fracties,
                           document_url, document_id, context, pro, gezien, extra)
                         VALUES ('notubiz', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                      (s["extern_id"], s["soort"], s["titel"], s["nummer"], s["datum"], s["gewijzigd"] or None,
                       s["fracties"], s["document_url"], s["document_id"], s["context"], s["pro"], gezien,
                       Jsonb(s["extra"]) if s.get("extra") else None))
            nieuw += 0 if gezien else 1
        elif s["gewijzigd"] and str(oud["gewijzigd"] or "")[:19] != s["gewijzigd"][:19]:
            # Gewijzigd, bijvoorbeeld omdat het antwoord op een vraag binnen is: opnieuw onder de aandacht.
            c.execute("""UPDATE stukken SET titel = %s, nummer = %s, datum = %s, gewijzigd = %s, fracties = %s,
                           document_url = %s, document_id = %s, context = %s, pro = %s, extra = %s,
                           gezien = false, bijgewerkt = now(), tekst_id = NULL
                         WHERE id = %s""",
                      (s["titel"], s["nummer"], s["datum"], s["gewijzigd"], s["fracties"], s["document_url"],
                       s["document_id"], s["context"], s["pro"], Jsonb(s["extra"]) if s.get("extra") else None, oud["id"]))
            bijgewerkt += 1
        elif s.get("extra") and oud["extra"] is None:
            # Stukken van voor de kolom 'extra': stil aanvullen, zonder ze opnieuw als nieuw te tonen.
            c.execute("UPDATE stukken SET extra = %s WHERE id = %s", (Jsonb(s["extra"]), oud["id"]))
    teksten(c, log)
    return nieuw, bijgewerkt, fouten


def teksten(c, log=print, maximum=40):
    """Haalt de tekst op van recente stukken en zet die in de kennisbank (soort 'raadsstuk')."""
    rijen = c.execute("""SELECT id, soort, titel, document_url, document_id FROM stukken
                         WHERE bron = 'notubiz' AND tekst_id IS NULL AND document_url <> '' AND soort = ANY(%s)
                           AND (datum IS NULL OR datum >= current_date - 60) AND coalesce(tekst_fout, '') = ''
                         ORDER BY datum DESC NULLS LAST LIMIT %s""", (list(TEKST_VAN), maximum)).fetchall()
    for r in rijen:
        try:
            tekst = pdf_tekst(notubiz.document_ophalen(r["document_url"])).replace("\x00", "")
            pad = f"notubiz/{r['document_id']}"
            d = c.execute("""INSERT INTO documenten (titel, soort, pad, tekst, woorden, bron_bestand)
                             VALUES (%s, 'raadsstuk', %s, %s, %s, %s)
                             ON CONFLICT (pad) DO UPDATE SET titel = EXCLUDED.titel, tekst = EXCLUDED.tekst,
                               woorden = EXCLUDED.woorden, bijgewerkt = now()
                             RETURNING id""",
                          (r["titel"], pad, tekst, len(tekst.split()), r["document_url"])).fetchone()
            c.execute("UPDATE stukken SET tekst_id = %s WHERE id = %s", (d["id"], r["id"]))
        except Exception as e:  # noqa: BLE001
            c.execute("UPDATE stukken SET tekst_fout = %s WHERE id = %s", (str(e)[:200], r["id"]))
            log(f"tekst {r['document_url']}: {e}")
