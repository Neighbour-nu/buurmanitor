"""Beschikbaarheid delen: Clara zet Martijns afspraken als blok 'Bezet' in zijn andere agenda's.

Zo zien collega's van de raad, Upper Spire en IV-professional in hun eigen Outlook of Google wanneer hij er niet is,
zonder dat hij een aparte synchronisatiedienst nodig heeft.

Spelregels:
- Alleen afspraken die Martijns tijd bezet houden: uit agenda's die meelezen en 'bezet voor mij' zijn (dus niet die
  van Sanne of Brigit), niet vrij, niet afgewezen, geen onbeantwoorde uitnodiging, geen hele dag, niet verborgen,
  en niet uit de openbare raadsagenda.
- Elk doel is de standaardagenda ('Standaard inplannen') van een account met lezen en schrijven.
- Clara wijzigt en verwijdert alleen blokken die ze zelf maakte; die zijn herkenbaar aan MARKER en staan in
  agenda_spiegel. Een blok van Clara wordt zelf nooit doorgezet (geen kringloop).
- Staat de afspraak al in het doel (uitgenodigd op beide adressen), dan komt er geen extra blok.
- Uit is uit: zet Martijn het doorzetten (of één doel) uit, dan haalt Clara haar blokken daar weer weg.
- Per doel kiest Martijn wat erin staat: 'Bezet', de titel (privé: alleen hij ziet hem) of alle details (titel en plek,
  niet privé). Dat laatste is voor de privé-agenda die Brigit met details mag zien: zij ziet dan ook waar hij is als
  hij voor Upper Spire of IV-professional weg is. Verandert de keuze, dan past Clara de bestaande blokken aan.
"""
import datetime as dt
import json

from ..kern import config, meldingen
from . import analyse, bronnen, kluis

MARKER = "[clara-spiegel]"
OMSCHRIJVING = MARKER + " Doorgezet door Buurmanitor: op dit moment ben ik bezet."
STANDAARD = {"spiegel_aan": False, "spiegel_titel": "bezet", "spiegel_raad": False}
DETAILS = (("bezet", '"Bezet"'), ("titel", "Titel, privé (alleen jij ziet hem)"), ("details", "Alle details: titel en plek"))
MAX_PER_RONDE = 150  # wijzigingen per ronde (elke vijf minuten); de eerste keer gaat het dus in een paar rondes


def omschrijving(zichtbaar=False, herkomst=""):
    if zichtbaar and herkomst:
        return f"{MARKER} Doorgezet door Buurmanitor uit de agenda {herkomst}."
    return OMSCHRIJVING


def is_spiegel(a):
    return MARKER in (a.get("omschrijving") or "")


def _van_andere_dienst(a):
    """Blokken van een andere synchronisatiedienst nemen we niet over (anders nemen de twee elkaars blokken over)."""
    return "calendarbridge" in f"{a.get('omschrijving') or ''} {a.get('titel') or ''}".lower()


def rol(k):
    """Kan Clara in dit account blokken zetten? Geeft (ja/nee, uitleg)."""
    if k["soort"] == "ics":
        return False, "agendalink: alleen lezen. De afspraken hieruit gaan wel naar je andere agenda's."
    if k["soort"] == "raad":
        return False, "openbare raadsagenda: alleen lezen. Vink aan om de vergaderingen in je andere agenda's te zetten."
    if k["soort"] not in ("google", "microsoft"):
        return False, ""
    if not k["actief"]:
        return False, "niet actief"
    if not k["schrijven"]:
        return False, ("gekoppeld met alleen lezen: de afspraken hieruit gaan wel naar je andere agenda's; om hier zelf "
                       "blokken te krijgen, koppel opnieuw met lezen en schrijven")
    if k["fout"]:
        return False, k["fout"]
    if not k["schrijfagenda"]:
        return False, "kies bij dit account een agenda onder 'Standaard inplannen'"
    return True, ""


def _agendanaam(k):
    return next((a["naam"] for a in (k["agendas"] or []) if a["id"] == k["schrijfagenda"]), k["schrijfagenda"])


def _sleutel(a):
    return f"{a['koppeling_id']}|{a['agenda_id']}|{a['extern_id']}"


def _bronnen(c, van, tot):
    """Alle afspraken in het venster die Martijns tijd bezet houden, één keer per afspraak."""
    from . import sync  # pylint: disable=import-outside-toplevel  (sync gebruikt deze module ook)
    rijen = c.execute("""SELECT i.*, k.soort, k.naam AS k_naam, k.agendas AS k_agendas FROM agenda_items i
                         JOIN agenda_koppelingen k ON k.id = i.koppeling_id
                         WHERE k.actief AND i.start < %s AND i.eind > %s ORDER BY i.start, k.id, i.id""", (tot, van)).fetchall()
    from . import reis  # pylint: disable=import-outside-toplevel
    regels = sync.verbergregels(c)
    gekozen = reis.keuzes(c)
    raad_mee = bool(sync.instellingen(c).get("spiegel_raad"))
    gezien, uit, raad = set(), [], []
    for r in rijen:
        a = dict(r)
        if is_spiegel(a) or _van_andere_dienst(a):
            continue
        if a["soort"] == "raad":
            if raad_mee:
                raad.append(a)
            continue
        if a["soort"] in ("google", "microsoft"):
            ag = next((x for x in (a["k_agendas"] or []) if x["id"] == a["agenda_id"]), None)
            if not ag or not ag.get("aan") or (ag.get("bezet") is False and not reis.gaat_mee(a, gekozen)):
                continue
        if not analyse.bezet(a) or a.get("antwoord") == "geen" or analyse.verborgen(a, regels):
            continue
        uid = (a.get("ical_uid") or "").lower()
        titel = ("titel", " ".join((a.get("titel") or "").lower().split()), a["start"], a["eind"])
        if (uid and (uid, a["start"]) in gezien) or titel in gezien:
            continue
        if uid:
            gezien.add((uid, a["start"]))
        gezien.add(titel)
        uit.append(a)
    # Een vergadering uit de openbare raadsagenda alleen als hij niet al (met een andere titel) in een eigen agenda staat.
    for a in raad:
        t = " ".join((a.get("titel") or "").lower().split())
        if not any((b.get("domein") == "politiek" and abs((b["start"] - a["start"]).total_seconds()) <= 45 * 60)
                   or (b["start"] == a["start"] and " ".join((b.get("titel") or "").lower().split()) == t) for b in uit):
            uit.append(a)
    return sorted(uit, key=lambda x: x["start"])


def detail(k, inst):
    """Wat er in de blokken in dit account staat: 'bezet', 'titel' of 'details'."""
    d = k.get("spiegel_detail") or ""
    return d if d in dict(DETAILS) else ("titel" if inst.get("spiegel_titel") == "titel" else "bezet")


def blok(a, soort):
    """Het blok voor een afspraak. Pure functie."""
    zichtbaar = soort == "details"
    plek = ((a.get("locatie") or "").strip() or ("Online" if a.get("online") else "")) if zichtbaar else ""
    titel = ((a.get("titel") or "").strip() or "Bezet") if soort in ("titel", "details") else "Bezet"
    return {"titel": titel,
            "start": a["start"], "eind": a["eind"], "voorlopig": a.get("status") == "voorlopig" or a.get("antwoord") == "voorlopig",
            "locatie": plek[:250], "zichtbaar": zichtbaar, "herkomst": a.get("k_naam") or ""}


def plan(c, nu=None):
    """Wat er in elk doel hoort te staan en wat er nu staat. Leest alleen de database (geen verzoeken naar buiten).
    Geeft een lijst met per account: k, kan, uitleg, agenda, wens {sleutel: blok}, staan {sleutel: rij}."""
    from . import sync  # pylint: disable=import-outside-toplevel
    nu = nu or dt.datetime.now(config.TZ)
    van = dt.datetime.combine(nu.date(), dt.time(0), tzinfo=config.TZ)
    tot = sync.venster(nu)[1]
    inst = sync.instellingen(c)
    lijst = [k for k in sync.koppelingen(c) if k["soort"] in ("google", "microsoft", "ics", "raad")]
    bron = _bronnen(c, van, tot) if any(rol(k)[0] for k in lijst) else []
    uit = []
    for k in lijst:
        kan, uitleg = rol(k)
        staan = {r["bron"]: r for r in c.execute(
            "SELECT * FROM agenda_spiegel WHERE doel_koppeling = %s AND eind > %s", (k["id"], van)).fetchall()}
        wens = {}
        if kan and inst.get("spiegel_aan") and k["spiegel"]:
            eigen = c.execute("""SELECT titel, start, eind, ical_uid, omschrijving FROM agenda_items
                                 WHERE koppeling_id = %s AND agenda_id = %s AND start < %s AND eind > %s""",
                              (k["id"], k["schrijfagenda"], tot, van)).fetchall()
            eigen = [e for e in eigen if not is_spiegel(e)]
            uids = {((e["ical_uid"] or "").lower(), e["start"]) for e in eigen if e["ical_uid"]}
            zelfde = {(e["titel"], e["start"], e["eind"]) for e in eigen}
            for a in bron:
                if (a["koppeling_id"], a["agenda_id"]) == (k["id"], k["schrijfagenda"]):
                    continue
                if ((a.get("ical_uid") or "").lower(), a["start"]) in uids or (a["titel"], a["start"], a["eind"]) in zelfde:
                    continue
                wens[_sleutel(a)] = blok(a, detail(k, inst))
        uit.append({"k": k, "kan": kan, "uitleg": uitleg, "agenda": _agendanaam(k) if kan else "", "wens": wens, "staan": staan})
    return uit


def _anders(rij, b):
    return ((rij["titel"], rij["start"], rij["eind"], rij["voorlopig"], rij.get("locatie") or "", bool(rij.get("zichtbaar")))
            != (b["titel"], b["start"], b["eind"], b["voorlopig"], b["locatie"], b["zichtbaar"]))


def bijwerken(c, nu=None):
    """Eén ronde: blokken maken, verschuiven en weghalen. Geeft een korte uitslag, of None als er niets te doen was."""
    from . import sync  # pylint: disable=import-outside-toplevel
    nu = nu or dt.datetime.now(config.TZ)
    c.execute("DELETE FROM agenda_spiegel WHERE eind < %s", (nu - dt.timedelta(days=1),))  # verleden blijft gewoon staan
    budget, uitslag, status = MAX_PER_RONDE, [], {}
    for p in plan(c, nu):
        k, wens, staan = p["k"], p["wens"], p["staan"]
        weg = [s for s, r in staan.items() if s not in wens or r["doel_agenda"] != k["schrijfagenda"]]
        nieuw = [s for s in wens if s not in staan or staan[s]["doel_agenda"] != k["schrijfagenda"]]
        wijzig = [s for s in wens if s in staan and s not in weg and _anders(staan[s], wens[s])]
        if not (weg or nieuw or wijzig) or budget <= 0:
            if p["kan"] or staan:
                status[str(k["id"])] = f"{len(staan)} blokken" + (", nog niet alles bijgewerkt" if (weg or nieuw or wijzig) else "")
            continue
        if not p["kan"] and not (k["actief"] and k["schrijven"] and not k["fout"]):
            # (zonder standaardagenda kan weghalen nog wel; bij een fout of alleen lezen niet)
            status[str(k["id"])] = f"{len(staan)} blokken, maar Clara kan er nu niet bij ({p['uitleg']})"
            continue
        google = k["soort"] == "google"
        blok_maak = bronnen.google_blok if google else bronnen.ms_blok
        blok_weg = bronnen.google_verwijder if google else bronnen.ms_verwijder
        gedaan = {"nieuw": 0, "gewijzigd": 0, "weg": 0}
        try:
            tok = sync.toegang(c, k)
            for s in weg:
                if budget <= 0:
                    break
                try:
                    blok_weg(tok, staan[s]["doel_agenda"], staan[s]["extern_id"])
                except bronnen.NietGevonden:
                    pass  # al weg (bijvoorbeeld met de hand verwijderd)
                c.execute("DELETE FROM agenda_spiegel WHERE doel_koppeling = %s AND bron = %s", (k["id"], s))
                gedaan["weg"] += 1
                budget -= 1
            for s in wijzig + nieuw:
                if budget <= 0:
                    break
                b = wens[s]
                extern = staan[s]["extern_id"] if s in wijzig else None
                extra = {"locatie": b["locatie"], "zichtbaar": b["zichtbaar"], "herkomst": b["herkomst"]}
                try:
                    extern = blok_maak(tok, k["schrijfagenda"], b["titel"], b["start"], b["eind"], b["voorlopig"], extern_id=extern, **extra)
                except bronnen.NietGevonden:  # met de hand verwijderd: opnieuw zetten
                    extern = blok_maak(tok, k["schrijfagenda"], b["titel"], b["start"], b["eind"], b["voorlopig"], **extra)
                c.execute("""INSERT INTO agenda_spiegel (doel_koppeling, bron, doel_agenda, extern_id, titel, start, eind, voorlopig,
                                                         locatie, zichtbaar)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (doel_koppeling, bron) DO UPDATE SET doel_agenda = EXCLUDED.doel_agenda,
                               extern_id = EXCLUDED.extern_id, titel = EXCLUDED.titel, start = EXCLUDED.start,
                               eind = EXCLUDED.eind, voorlopig = EXCLUDED.voorlopig, locatie = EXCLUDED.locatie,
                               zichtbaar = EXCLUDED.zichtbaar, bijgewerkt = now()""",
                          (k["id"], s, k["schrijfagenda"], extern, b["titel"], b["start"], b["eind"], b["voorlopig"],
                           b["locatie"], b["zichtbaar"]))
                gedaan["gewijzigd" if s in wijzig else "nieuw"] += 1
                budget -= 1
        except (bronnen.BronFout, kluis.KluisFout) as e:  # één doel mag de andere niet ophouden
            uitslag.append(f"{k['naam']}: {e}")
            status[str(k["id"])] = f"fout: {e}"
            meldingen.melden(c, "belangrijk", "clara", f"Beschikbaarheid naar {k['naam']} lukt niet", str(e), "/clara/koppelingen#beschikbaarheid",
                             sleutel=f"clara-spiegel-{k['id']}-{nu:%Y-%m-%d}")
            continue
        n = c.execute("SELECT count(*) AS n FROM agenda_spiegel WHERE doel_koppeling = %s AND eind > %s", (k["id"], nu)).fetchone()["n"]
        status[str(k["id"])] = f"{n} blokken"
        delen = [f"{v} {w}" for w, v in gedaan.items() if v]
        if delen:
            uitslag.append(f"{k['naam']}: " + ", ".join(delen))
    c.execute("""INSERT INTO instellingen (sleutel, waarde) VALUES ('clara_spiegel_status', %s)
                 ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()""",
              (json.dumps({"tijd": nu.isoformat(), "doelen": status}),))
    return ("beschikbaarheid: " + "; ".join(uitslag)) if uitslag else None


def status(c):
    r = c.execute("SELECT waarde, bijgewerkt FROM instellingen WHERE sleutel = 'clara_spiegel_status'").fetchone()
    if not r:
        return {}
    try:
        return {**json.loads(r["waarde"]), "bijgewerkt": r["bijgewerkt"]}
    except ValueError:
        return {}
