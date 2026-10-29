"""Kennisbank: documenten inlezen (md, txt, docx, pdf, of een zip daarvan) en doorzoeken."""
import html
import io
import os
import re
import zipfile

SOORTEN = {
    "bron": "Bron (beleid, begroting, programma)",
    "bijdrage": "Bijdrage (eigen tekst)",
    "column": "Column",
    "notulen": "Notulen",
    "pers": "Pers en social media",
    "raadsstuk": "Raadsstuk uit Notubiz",
    "overig": "Overig",
}
# Mapnamen in een zip die de soort bepalen: die uit het project Politiek en die uit de Whaller-boxen.
# Per map, van diep naar ondiep, geldt de eerste regel die past: (stukje mapnaam, soort, vertrouwelijk).
# Soort None betekent: overslaan.
MAP_REGELS = [
    ("sjablonen en logo", None, False),
    ("notulen", "notulen", True),
    ("besluitenlijst", "notulen", True),
    ("actielijst", "notulen", True),
    ("strategie", "overig", True),
    ("handleiding", "overig", False),
    ("agenda en bijeenkomsten", "overig", False),
    ("pers en social", "pers", False),
    ("column", "column", False),
    ("raadsvragen", "bijdrage", False),
    ("commissievergadering", "bijdrage", False),
    ("raadsvergadering", "bijdrage", False),
    ("begroting", "bijdrage", False),
    ("bijdragen", "bijdrage", False),
    ("bronnen", "bron", False),
    ("fractievergadering", "overig", True),  # de groene box zelf: intern
]
LEESBAAR = (".md", ".markdown", ".txt", ".docx", ".pdf", ".xlsx", ".pptx")
NIET_LEESBAAR_MELDEN = (".doc", ".xls", ".ppt", ".odt", ".pages", ".rtf")


def _tekst_uit(naam, inhoud):
    ext = os.path.splitext(naam.lower())[1]
    if ext in (".md", ".markdown", ".txt"):
        for codering in ("utf-8", "cp1252", "latin-1"):
            try:
                return inhoud.decode(codering)
            except UnicodeDecodeError:
                continue
    if ext == ".docx":
        import docx  # python-docx
        d = docx.Document(io.BytesIO(inhoud))
        delen = [p.text for p in d.paragraphs]
        for t in d.tables:
            for rij in t.rows:
                delen.append(" | ".join(c.text.strip() for c in rij.cells))
        return "\n".join(delen)
    if ext == ".pdf":
        from pypdf import PdfReader
        lezer = PdfReader(io.BytesIO(inhoud))
        return "\n\n".join(f"[pagina {i}]\n" + (p.extract_text() or "") for i, p in enumerate(lezer.pages, 1))
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(inhoud), read_only=True, data_only=True)
        delen = []
        for blad in wb.worksheets:
            delen.append(f"[blad {blad.title}]")
            for i, rij in enumerate(blad.iter_rows(values_only=True)):
                if i >= 5000:
                    delen.append("[afgekapt na 5000 rijen]")
                    break
                cellen = [str(c).strip() for c in rij if c is not None and str(c).strip()]
                if cellen:
                    delen.append(" | ".join(cellen))
        return "\n".join(delen)
    if ext == ".pptx":
        from pptx import Presentation
        delen = []
        for i, dia in enumerate(Presentation(io.BytesIO(inhoud)).slides, 1):
            delen.append(f"[dia {i}]")
            for vorm in dia.shapes:
                if vorm.has_text_frame and vorm.text_frame.text.strip():
                    delen.append(vorm.text_frame.text)
        return "\n".join(delen)
    return ""


# Koppen die niets over de inhoud zeggen; dan is de bestandsnaam een betere titel.
GENERIEK = re.compile(r"^(bijdrage|stemverklaring|technische vragen|amendement|motie|spreektekst|"
                      r"schriftelijke vragen)?\s*(pro helmond)?$", re.I)
KOP = re.compile(r"^[ \t]*(?:#{1,3}[ \t]+([^\n]+)|\*\*([^*\n][^*]{1,250}?)\*\*)", re.M)


def _schoon(kop):
    kop = re.sub(r"[\\*_]+", "", kop)
    return re.sub(r"\s+", " ", kop).strip(" :")


def _naam_uit_bestand(naam):
    basis = os.path.splitext(os.path.basename(naam))[0]
    basis = re.sub(r"^\d{4}-\d{2}(-\d{2})?-", "", basis)
    basis = re.sub(r"[-_]+", " ", basis).strip()
    return basis[:1].upper() + basis[1:]


def _titel(naam, tekst):
    """De eerste kop van het document, of de bestandsnaam als die kop niets zegt (zoals 'PRO Helmond')."""
    if naam.lower().endswith((".md", ".markdown", ".txt", ".docx")):
        m = KOP.search(tekst[:5000])
        if m:
            kop = _schoon(m.group(1) or m.group(2))
            if kop and len(kop) < 160 and not GENERIEK.match(kop):
                return kop
    return _naam_uit_bestand(naam)


def _regel_voor(pad):
    """(soort, vertrouwelijk) volgens de mapnamen in het pad, of None als geen regel past."""
    for deel in reversed(pad.split("/")[:-1]):
        d = deel.lower()
        for stukje, soort, vertrouwelijk in MAP_REGELS:
            if stukje in d:
                return soort, vertrouwelijk
    return None


def _stuk(pad, inhoud, soort, vertrouwelijk=False):
    tekst = _tekst_uit(pad, inhoud).replace("\x00", "")
    return {"pad": pad, "titel": _titel(pad, tekst), "soort": soort, "tekst": tekst,
            "woorden": len(tekst.split()), "vertrouwelijk": vertrouwelijk}


def _zipnaam(info):
    """Zonder utf-8-vlag leest Python namen als cp437; Windows en Whaller schrijven vaak utf-8."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def lees_upload(bestandsnaam, inhoud, soort, vertrouwelijk=False):
    """Geeft (documenten, overgeslagen). Een zip mag mappen bevatten; de mapnamen bepalen dan
    de soort en of een stuk vertrouwelijk is (zie MAP_REGELS)."""
    overgeslagen = []
    if bestandsnaam.lower().endswith(".zip"):
        uit = []
        with zipfile.ZipFile(io.BytesIO(inhoud)) as z:
            for info in z.infolist():
                pad = _zipnaam(info)
                naam = os.path.basename(pad)
                if info.is_dir() or "__MACOSX" in pad or naam.startswith((".", "~$")):
                    continue
                regel = _regel_voor(pad)
                if regel and regel[0] is None:
                    continue  # bijvoorbeeld de map met sjablonen en logo's
                if pad.lower().endswith(NIET_LEESBAAR_MELDEN):
                    overgeslagen.append(f"{naam} (oud formaat)")
                    continue
                if not pad.lower().endswith(LEESBAAR):
                    continue  # afbeeldingen en dergelijke: stil overslaan
                if info.file_size > 25 * 1024 * 1024:
                    overgeslagen.append(f"{naam} (te groot)")
                    continue
                s, v = regel if regel else (soort, False)
                try:
                    uit.append(_stuk(pad, z.read(info), s, v or vertrouwelijk))
                except Exception:  # noqa: BLE001 - een kapot bestand mag de rest niet tegenhouden
                    overgeslagen.append(f"{naam} (niet te lezen)")
        return uit, overgeslagen
    if not bestandsnaam.lower().endswith(LEESBAAR):
        return [], [f"{bestandsnaam} (formaat niet ondersteund)"]
    try:
        return [_stuk(bestandsnaam, inhoud, soort, vertrouwelijk)], []
    except Exception:  # noqa: BLE001
        return [], [f"{bestandsnaam} (niet te lezen)"]


def _markeer(fragment):
    # Inhoudsopgaven uit pdf's hebben lange rijen puntjes; die maken een fragment onleesbaar.
    fragment = re.sub(r"(?:\s*[.·…]){4,}\s*", " … ", fragment or "")
    veilig = html.escape(fragment)
    return veilig.replace("[[[", "<mark>").replace("]]]", "</mark>")


def zoeken(q, zoek, soort="", limiet=25):
    rijen = q("""
        WITH z AS (SELECT websearch_to_tsquery('dutch', %s) AS vraag)
        SELECT d.id, d.titel, d.soort, d.pad,
               ts_rank(d.zoek, z.vraag) AS rang,
               ts_headline('dutch', left(d.tekst, 200000), z.vraag,
                 'StartSel=[[[, StopSel=]]], MaxFragments=3, MaxWords=28, MinWords=10, FragmentDelimiter=" … "') AS fragment
        FROM documenten d, z
        WHERE d.zoek @@ z.vraag AND (%s = '' OR d.soort = %s)
        ORDER BY rang DESC LIMIT %s""", (zoek, soort, soort, limiet))
    for r in rijen:
        r["fragment_html"] = _markeer(r["fragment"])
    return rijen


def context(q, vraag, maximum=24000, documenten=8, uitsluiten=(), voorkeur=(), alleen=(), fragmenten=5,
            vertrouwelijk=False):
    """Tekstfragmenten uit de kennisbank die passen bij een vraag, voor Claude.
    vertrouwelijk: alleen met True gaan vertrouwelijk gemarkeerde stukken mee (Martijn kiest dat per keer);
    standaard blijven ze buiten alles wat naar Claude gaat.
    Zoekt met OF tussen de woorden (ruimer dan het zoekscherm) en geeft per document de beste passages.
    voorkeur: stukjes van paden die altijd mee moeten als ze iets opleveren (zoals het verkiezingsprogramma)."""
    woorden = [w for w in re.findall(r"[\wÀ-ÿ'-]{4,}", vraag or "") if w.lower() not in STOPWOORDEN][:30]
    if not woorden:
        return ""
    zoekvraag = " or ".join(dict.fromkeys(w.lower() for w in woorden))
    rijen = q("""
        WITH z AS (SELECT websearch_to_tsquery('dutch', %s) AS vraag)
        SELECT d.id, d.titel, d.soort, d.pad, d.vertrouwelijk,
               ts_rank_cd(d.zoek, z.vraag) * CASE WHEN d.pad LIKE ANY(%s) THEN 3 ELSE 1 END AS rang,
               ts_headline('dutch', left(d.tekst, 400000), z.vraag,
                 %s) AS fragment
        FROM documenten d, z
        WHERE d.zoek @@ z.vraag AND NOT (d.id = ANY(%s)) AND (cardinality(%s::int[]) = 0 OR d.id = ANY(%s::int[]))
          AND (%s OR NOT d.vertrouwelijk)
        ORDER BY rang DESC LIMIT %s""",
          (zoekvraag, [f"%{v}%" for v in voorkeur] or ["#"],
           f'MaxFragments={int(fragmenten)}, MaxWords=60, MinWords=25, FragmentDelimiter=" [...] ", StartSel="", StopSel=""',
           list(uitsluiten) or [0], list(alleen), list(alleen), bool(vertrouwelijk), documenten))
    delen, totaal = [], 0
    for r in rijen:
        stuk = f"### {r['titel']} ({SOORTEN.get(r['soort'], r['soort'])}{', vertrouwelijk' if r['vertrouwelijk'] else ''})\n{r['fragment']}"
        if totaal + len(stuk) > maximum:
            break
        delen.append(stuk)
        totaal += len(stuk)
    if any(r["vertrouwelijk"] for r in rijen[:len(delen)]):
        delen.insert(0, VERTROUWELIJK_REGEL)
    return "\n\n".join(delen)


VERTROUWELIJK_REGEL = ("Let op: stukken met de markering vertrouwelijk zijn alleen achtergrond voor Martijn. Citeer ze niet, "
                       "noem ze niet en neem er niets uit over in tekst die naar buiten kan gaan.")


STOPWOORDEN = {"over", "deze", "voor", "zijn", "wordt", "worden", "naar", "maar", "ook", "niet", "heeft", "hebben", "door",
               "waar", "wat", "welke", "kunnen", "moet", "meer", "geen", "zich", "onze", "jouw", "mijn", "wij", "zij",
               "raadsvoorstel", "voorstel", "gemeente", "helmond", "bijlage", "uur", "commissie", "raad"}
