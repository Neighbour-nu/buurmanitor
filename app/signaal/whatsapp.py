"""WhatsApp: een export inlezen (geen directe koppeling).

In WhatsApp: open de chat, Exporteer chat (Android: Meer > Exporteren; iPhone: tik op de naam > Exporteer chat), kies
Zonder media. Dat geeft een .txt (Android) of een .zip met _chat.txt (iPhone). Die lees je in op Post > Chats, of je
mailt hem naar jezelf en laat een regel in Post hem inlezen ('WhatsApp-export inlezen'). Elke dag de export opnieuw
inlezen kan: berichten die er al waren, slaat Buurmanitor over.

Formaten (de datum volgt de taal van de telefoon):
  iPhone:  [19-11-2026 15:26:01] Naam: tekst          [11/19/26, 3:26:01 PM] Name: text
  Android: 19-11-2026 15:26 - Naam: tekst             19/11/2026, 15:26 - Name: text
Regels zonder datum horen bij het bericht erboven. Regels zonder 'Naam:' zijn meldingen van WhatsApp (versleuteling,
iemand toegevoegd) en gaan niet mee.
"""
import datetime as dt
import io
import re
import unicodedata
import zipfile

from ..kern import config
from . import verwerken

MAX_BYTES = 30_000_000
REGEL = re.compile(r"^\[?(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,4}),?\s+(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([AaPp]\.?\s?[Mm]\.?)?\]?\s*(?:-\s+)?(.*)$")
ONZICHTBAAR = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩﻿"), None)
MEDIA = re.compile(r"^<?(media weggelaten|media omitted|bijgevoegd:.*|attached:.*|afbeelding weggelaten|video weggelaten|"
                   r"audio weggelaten|sticker weggelaten|gif weggelaten|document weggelaten|image omitted|video omitted|"
                   r"audio omitted|sticker omitted|gif omitted|document omitted)>?$", re.I)
VOORVOEGSELS = re.compile(r"^(whatsapp[- ]chat (met|with|-)\s*|whatsapp chat - |whatsapp-chat met )", re.I)


class Fout(Exception):
    pass


def chatnaam_uit_bestand(naam):
    """'WhatsApp Chat - Fractie PRO.zip' of 'WhatsApp-chat met Anna.txt' geeft 'Fractie PRO' / 'Anna'. Pure functie."""
    naam = (naam or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    naam = re.sub(r"\.(txt|zip)$", "", naam, flags=re.I)
    naam = re.sub(r"\s*\(\d+\)$", "", naam)  # 'chat (1).zip' als je hem twee keer downloadt
    if naam.lower() in ("_chat", "chat", ""):
        return ""
    return VOORVOEGSELS.sub("", naam).strip(" -_")[:120]


def lees_bestand(bestandsnaam, data):
    """(chatnaam, tekst) uit een .txt of een .zip van een export. Fout als het geen WhatsApp-export is."""
    if len(data) > MAX_BYTES:
        raise Fout(f"{bestandsnaam} is te groot (meer dan {MAX_BYTES // 1_000_000} MB). Exporteer zonder media.")
    naam = chatnaam_uit_bestand(bestandsnaam)
    if data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise Fout(f"{bestandsnaam} is geen goed zip-bestand.") from e
        txt = [i for i in z.infolist() if i.filename.lower().endswith(".txt") and not i.filename.startswith("__MACOSX")]
        txt.sort(key=lambda i: (not i.filename.lower().endswith("_chat.txt"), i.filename))
        if not txt:
            raise Fout(f"In {bestandsnaam} staat geen chat (.txt).")
        if txt[0].file_size > MAX_BYTES:
            raise Fout(f"De chat in {bestandsnaam} is te groot.")
        naam = naam or chatnaam_uit_bestand(txt[0].filename)
        data = z.read(txt[0])
    for codering in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return naam, data.decode(codering)
        except UnicodeDecodeError:
            continue
    raise Fout(f"{bestandsnaam} kan ik niet lezen.")


def _volgorde(kandidaten):
    """Dag-maand of maand-dag? Kijk of er een eerste getal boven de 12 zit (dan dag-maand) of een tweede (maand-dag)."""
    eerste = [int(a) for a, b, _ in kandidaten if len(a) <= 2]
    tweede = [int(b) for a, b, _ in kandidaten if len(a) <= 2]
    if any(x > 12 for x in tweede) and not any(x > 12 for x in eerste):
        return "md"
    return "dm"


def ontleed(tekst, eigen_namen=()):
    """De berichten uit een export: [{tijd, van, van_mij, tekst}], oudste eerst. Pure functie."""
    regels = tekst.translate(ONZICHTBAAR).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    treffers = [(r, REGEL.match(r)) for r in regels]
    volgorde = _volgorde([(m.group(1), m.group(2), m.group(3)) for _, m in treffers if m][:500])
    eigen = {n.strip().lower() for n in eigen_namen if n and n.strip()}
    uit, huidig = [], None
    for r, m in treffers:
        tijd = _tijd(m, volgorde) if m else None
        if tijd is None:
            if huidig is not None and r.strip():
                huidig["tekst"] += "\n" + r
            continue
        rest = m.group(8)
        van, sep, inhoud = rest.partition(": ")
        if not sep or len(van) > 80:
            huidig = None  # melding van WhatsApp zelf
            continue
        van = van.strip()
        inhoud = "[bijlage]" if MEDIA.match(inhoud.strip()) else inhoud
        if inhoud.strip() in ("Dit bericht is verwijderd", "This message was deleted", "Je hebt dit bericht verwijderd",
                              "You deleted this message", "null"):
            huidig = None
            continue
        mij = van.lower() in eigen
        huidig = {"tijd": tijd, "van": "jij" if mij else van, "van_mij": mij, "tekst": inhoud}
        uit.append(huidig)
    for b in uit:
        b["tekst"] = b["tekst"].strip()[:8000] or "[leeg]"
    return uit


def _tijd(m, volgorde):
    a, b, j, uur, minuut, sec, ampm = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7)
    try:
        if len(a) == 4:  # 2026-11-19
            jaar, maand, dag = int(a), int(b), int(j)
        else:
            dag, maand = (int(a), int(b)) if volgorde == "dm" else (int(b), int(a))
            jaar = int(j) + (2000 if len(j) == 2 else 0)
        u = int(uur)
        if ampm:
            pm = ampm.lower().startswith("p")
            u = (u % 12) + (12 if pm else 0)
        return dt.datetime(jaar, maand, dag, u, int(minuut), int(sec or 0), tzinfo=config.TZ)
    except ValueError:
        return None


def sleutel(naam):
    s = unicodedata.normalize("NFKC", naam).strip().lower()
    return "whatsapp:" + re.sub(r"\s+", " ", s)[:150]


def naar_berichten(chatnaam, berichten):
    """In de vorm van verwerken.opslaan. Een vaste 'ts' per bericht (tijd + volgnummer binnen dezelfde minuut van dezelfde
    afzender), zodat dezelfde export morgen opnieuw inlezen niets dubbel maakt. Pure functie."""
    afzenders = {b["van"] for b in berichten if not b["van_mij"]}
    soort = "groep" if len(afzenders) > 1 else "persoon"
    teller, uit = {}, []
    for b in berichten:
        basis = int(b["tijd"].timestamp()) * 1000
        k = (basis, b["van"])
        n = teller.get(k, 0)
        teller[k] = n + 1
        uit.append({"sleutel": sleutel(chatnaam), "soort": soort, "chatnaam": chatnaam, "bron": "whatsapp", "archief_standaard": True,
                    "ts": basis + n, "van": b["van"], "van_mij": b["van_mij"], "tekst": b["tekst"], "tijd": b["tijd"]})
    return uit


def eigen_namen(c):
    return [n.strip() for n in (verwerken.instelling(c, "whatsapp_naam", "") or "").split(",") if n.strip()]


def importeer(c, bestandsnaam, data, chatnaam=""):
    """Een export inlezen. Geeft een korte uitslag. Een chat die je eerder verwijderde, blijft weg."""
    naam_bestand, tekst = lees_bestand(bestandsnaam, data)
    naam = (chatnaam or naam_bestand).strip()[:120]
    if not naam:
        raise Fout(f"Uit {bestandsnaam} kan ik de naam van de chat niet halen. Geef hem op bij het inlezen.")
    berichten = ontleed(tekst, eigen_namen(c))
    if not berichten:
        raise Fout(f"In {bestandsnaam} vond ik geen WhatsApp-berichten. Is het een export van een chat (zonder media)?")
    rijen = naar_berichten(naam, berichten)
    voor = c.execute("SELECT id, aantal FROM signal_chats WHERE sleutel = %s", (rijen[0]["sleutel"],)).fetchone()
    nieuw_chat, bewaard = verwerken.opslaan(c, rijen)
    ch = c.execute("SELECT * FROM signal_chats WHERE sleutel = %s", (rijen[0]["sleutel"],)).fetchone()
    # 'aantal' telt bij inlezen elk bericht in het bestand; zet het op wat er werkelijk is (of was, als niets bewaard wordt).
    echt = c.execute("SELECT count(*) AS n FROM signal_berichten WHERE chat_id = %s", (ch["id"],)).fetchone()["n"]
    c.execute("UPDATE signal_chats SET aantal = %s WHERE id = %s", (max(echt, voor["aantal"] if voor else 0, len(rijen) if not echt else 0), ch["id"]))
    if ch["verborgen"]:
        return f"{naam}: deze chat had je verwijderd, dus niets ingelezen (terugzetten kan onder Verwijderde chats)."
    if not (ch["meelezen"] or ch["archief"]):
        return f"{naam}: niets bewaard, want meelezen en archief staan uit voor deze chat."
    return (f"{naam}: {len(rijen)} berichten in het bestand, {bewaard} nieuw" + (" (nieuwe chat, in het archief)" if nieuw_chat else "")
            + ("" if eigen_namen(c) else ". Tip: vul je eigen naam in, dan herkent Bram jouw berichten"))
