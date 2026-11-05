"""De indeling van een commissieavond uit de PDF van de fractie: wie is woordvoerder bij welk agendapunt.

Jeanette zet de namen in de PDF van de griffie (de agenda per commissie), naast het agendapunt, bijvoorbeeld
'Niek en Petra'. Vaak met Invullen en ondertekenen in Acrobat; de namen staan dan als losse tekstblokjes op
de pagina. Daarom lezen we niet alleen de tekst, maar ook waar die staat: een naam hoort bij het agendapunt
op dezelfde hoogte (of net erboven). Wat aan geen agendapunt hangt, zoals 'Sanae voorzitter', komt als
opmerking mee.

De eerste naam is de woordvoerder, de tweede zit erbij (zoals in het Whaller-bericht: @woordvoerder & @erbij).
Alles hier is zonder database en netwerk, zodat het te testen is. main.py koppelt het resultaat aan de
agendapunten uit Notubiz en aan de fractieleden.
"""
import datetime as dt
import difflib
import io
import re

from . import whaller

PUNT = re.compile(r"^\s*(\d{1,2}(?:\.[a-z])?)\.?\s+(\S.*)$")
DATUM = re.compile(r"\b(\d{1,2})-(\d{1,2})-(20\d{2})\b")
COMMISSIE = re.compile(r"^\s*(Commissie\s+[A-Z][\w-]*)\s*$")
SCHEIDING = re.compile(r"\s*(?:\ben\b|&|\+|,|/|;)\s*", re.I)
NAAM = r"[A-ZÀ-Ý][a-zà-ÿ'-]+(?:\s+(?:van|de|der|den|v\.?d\.?)\s+[A-ZÀ-Ý][a-zà-ÿ'-]+)?"
NAMEN = re.compile(rf"^{NAAM}(?:\s*(?:\ben\b|&|\+|,|/)\s*{NAAM}){{0,3}}$")
STAART = re.compile(rf"^(.*?(?:uur\)?|\)))\s+({NAAM}(?:\s*(?:\ben\b|&|\+|,|/)\s*{NAAM}){{0,3}})$")
VAST = re.compile(r"^(opening|vaststellen besluitenlijst|voorstel hamerstuk|sluiting|rondvraag)", re.I)
MAX_PAGINAS = 30


# ---------------------------------------------------------------- tekst met positie

def _keer(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def _lettertypen(res):
    from pypdf._cmap import build_char_map_from_dict  # pylint: disable=import-outside-toplevel
    uit = {}
    for naam, f in ((res or {}).get("/Font") or {}).items():
        try:
            _, _, codering, kaart = build_char_map_from_dict(200.0, f.get_object())
            uit[naam] = (codering, kaart)
        except Exception:  # noqa: BLE001 - een raar lettertype: dan zonder vertaling
            uit[naam] = ("charmap", {})
    return uit


def _tekst(ruw, font):
    """Bytes uit Tj/TJ naar tekst, met de codering en de ToUnicode-kaart van het lettertype."""
    codering, kaart = font or ("charmap", {})
    b = ruw.get_original_bytes() if hasattr(ruw, "get_original_bytes") else (ruw.encode("latin-1", "replace") if isinstance(ruw, str) else bytes(ruw))
    if isinstance(codering, str):
        try:
            t = b.decode(codering, "surrogatepass")
        except Exception:  # noqa: BLE001
            t = b.decode("utf-16-be" if codering == "charmap" else "charmap", "surrogatepass")
    else:
        t = "".join(codering.get(x, chr(x)) for x in b)
    return "".join(kaart.get(c, c) for c in t)


def _stroom(reader, inhoud, res, ctm, uit, bron, diepte=0):
    """Loopt een inhoudsstroom door en noteert (x, y, tekst, bron) per tekstopdracht. Formulieren (/Form XObjects,
    zoals de tekstblokjes van Invullen en ondertekenen) worden met hun eigen positie meegenomen."""
    from pypdf.generic import ContentStream  # pylint: disable=import-outside-toplevel
    if diepte > 8 or inhoud is None:
        return
    xobj = (res or {}).get("/XObject") or {}
    fonts = _lettertypen(res)
    m, stapel = ctm, []
    tm = lm = (1, 0, 0, 1, 0, 0)
    leading, font = 0.0, None

    def toon(stukken):
        t = "".join(stukken).strip()
        if t:
            p = _keer(tm, m)
            uit.append((round(p[4], 1), round(p[5], 1), re.sub(r"\s+", " ", t.replace("\ufeff", "")).strip(), bron))

    for args, op in ContentStream(inhoud, reader).operations:
        if op == b"q":
            stapel.append(m)
        elif op == b"Q":
            m = stapel.pop() if stapel else ctm
        elif op == b"cm" and len(args) == 6:
            m = _keer(tuple(float(a) for a in args), m)
        elif op == b"BT":
            tm = lm = (1, 0, 0, 1, 0, 0)
        elif op == b"Tm" and len(args) == 6:
            tm = lm = tuple(float(a) for a in args)
        elif op in (b"Td", b"TD") and len(args) == 2:
            tx, ty = float(args[0]), float(args[1])
            if op == b"TD":
                leading = -ty
            tm = lm = _keer((1, 0, 0, 1, tx, ty), lm)
        elif op == b"TL" and args:
            leading = float(args[0])
        elif op == b"T*":
            tm = lm = _keer((1, 0, 0, 1, 0, -leading), lm)
        elif op == b"Tf" and args:
            font = fonts.get(args[0])
        elif op == b"Tj" and args:
            toon([_tekst(args[0], font)])
        elif op in (b"'", b'"') and args:
            tm = lm = _keer((1, 0, 0, 1, 0, -leading), lm)
            toon([_tekst(args[-1], font)])
        elif op == b"TJ" and args:
            stukken = []
            for a in args[0]:
                if isinstance(a, (int, float)) or hasattr(a, "as_numeric"):
                    if float(a) < -250:
                        stukken.append(" ")
                else:
                    stukken.append(_tekst(a, font))
            toon(stukken)
        elif op == b"Do" and args:
            o = xobj.get(args[0])
            o = o.get_object() if o is not None else None
            if o is not None and o.get("/Subtype") == "/Form":
                fm = tuple(float(v) for v in o.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                _stroom(reader, o, o.get("/Resources") or res, _keer(fm, m), uit, "blok", diepte + 1)


def fragmenten(data):
    """Per pagina de tekststukken met hun positie: [[(x, y, tekst, bron), ...], ...]. bron is 'pagina' of
    'blok' (een los tekstblok, zoals toegevoegd met Invullen en ondertekenen)."""
    from pypdf import PdfReader  # pylint: disable=import-outside-toplevel
    reader = PdfReader(io.BytesIO(data))
    paginas = []
    for pagina in reader.pages[:MAX_PAGINAS]:
        uit = []
        _stroom(reader, pagina.get_contents(), pagina.get("/Resources"), (1, 0, 0, 1, 0, 0), uit, "pagina")
        for a in pagina.get("/Annots") or []:  # vrije tekst als opmerking (FreeText) telt ook
            a = a.get_object()
            if a.get("/Subtype") == "/FreeText" and a.get("/Contents") and a.get("/Rect"):
                r = [float(v) for v in a["/Rect"]]
                uit.append((round(r[0], 1), round(r[3] - 12, 1), re.sub(r"\s+", " ", str(a["/Contents"])).strip(), "blok"))
        paginas.append(uit)
    return paginas


# ---------------------------------------------------------------- lezen

def splits_namen(tekst):
    """'Niek en Petra' -> ['Niek', 'Petra']. Pure functie."""
    return [n.strip(" .") for n in SCHEIDING.split(tekst or "") if n.strip(" .")]


def _is_namen(t, voornamen):
    """Lijkt dit stukje tekst op namen? Met 'en' of '&' ertussen, of een voornaam van de fractie."""
    if not NAMEN.match(t):
        return False
    delen = [n.lower().split()[0] for n in splits_namen(t)]
    return len(delen) > 1 or (bool(voornamen) and delen[0] in voornamen)


def _plak_nummers(stukken):
    """Van boven naar beneden; een los agendanummer ('4.') wordt samengevoegd met de tekst ernaast."""
    rij = sorted(((x, y, t.replace("\ufeff", "").strip(), bron) for x, y, t, bron in stukken if t.strip()), key=lambda s: (-s[1], s[0]))
    uit = []
    for s in rij:
        v = uit[-1] if uit else None
        if v and v[3] == s[3] == "pagina" and abs(v[1] - s[1]) <= 2.5 and re.fullmatch(r"\d{1,2}(?:\.[a-z])?\.?", v[2]):
            uit[-1] = (v[0], v[1], f"{v[2]} {s[2]}", "pagina")
        else:
            uit.append(s)
    return uit


def lees(paginas, voornamen=()):
    """Uit de tekststukken: datum, en per agendapunt de namen. Pure functie.
    Geeft {"datum": date|None, "punten": [{"nummer", "onderwerp", "commissie", "namen"}], "opmerkingen": [...]}."""
    voornamen = {v.lower() for v in voornamen}
    datum, punten, opmerkingen, commissie = None, [], [], ""
    for stukken in paginas:
        regels, losse = [], []
        for x, y, t, bron in _plak_nummers(stukken):
            if datum is None and (m := DATUM.search(t)):
                try:
                    datum = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                except ValueError:
                    pass
            if bron == "blok" or _is_namen(t, voornamen):
                losse.append((x, y, t))
                continue
            if m := COMMISSIE.match(t):
                commissie = m.group(1)
            if m := PUNT.match(t):
                onderwerp = m.group(2).strip()
                if s := STAART.match(onderwerp):  # namen achter het onderwerp op dezelfde regel getypt
                    if _is_namen(s.group(2), voornamen):
                        onderwerp = s.group(1).strip()
                        losse.append((x + 300, y, s.group(2)))
                regels.append({"x": x, "y": y, "nummer": m.group(1), "onderwerp": onderwerp, "commissie": commissie, "namen": []})
        for x, y, t in losse:
            dichtbij = [r for r in regels if r["x"] < x and abs(r["y"] - y) <= 9]
            if not dichtbij:  # net onder de eerste regel van een langer agendapunt
                dichtbij = [r for r in regels if r["x"] < x and 0 < r["y"] - y <= 22]
            if dichtbij and (namen := splits_namen(t)) and NAMEN.match(t):
                r = min(dichtbij, key=lambda r: abs(r["y"] - y))
                r["namen"] += namen
            elif t not in opmerkingen:
                opmerkingen.append(t)
        punten += [r for r in regels if r["namen"]]
    gezien, uniek = set(), []
    for p in punten:  # het overzicht voorin en de agenda per commissie: één keer
        k = (p["commissie"], whaller.sleutel(p["onderwerp"]))
        if k not in gezien:
            gezien.add(k)
            uniek.append({k2: p[k2] for k2 in ("nummer", "onderwerp", "commissie", "namen")})
    return {"datum": datum, "punten": uniek, "opmerkingen": opmerkingen}


def lees_pdf(data, voornamen=()):
    return lees(fragmenten(data), voornamen)


# ---------------------------------------------------------------- koppelen

def _gelijk(a, b):
    ka, kb = whaller.sleutel(a), whaller.sleutel(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    if min(len(ka), len(kb)) >= 12 and (ka in kb or kb in ka):
        return 0.95
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def koppel(punten, pdf, personen, persoon_bij_naam):
    """Zet de namen uit de PDF bij de agendapunten uit Notubiz. Pure functie (persoon_bij_naam als parameter).
    Vult per punt 'pdf' in: {"namen", "woordvoerder", "zit_erbij", "onbekend"}. Geeft de PDF-punten die nergens
    pasten."""
    over = []
    gebruikt = set()
    for i, pp in enumerate(pdf.get("punten", [])):
        kandidaten = [(_gelijk(pp["onderwerp"], p["onderwerp"]), j) for j, p in enumerate(punten) if j not in gebruikt]
        score, j = max(kandidaten, default=(0.0, None))
        if j is None or score < 0.78 or VAST.match(pp["onderwerp"]):
            if not VAST.match(pp["onderwerp"]):
                over.append(pp)
            continue
        gebruikt.add(j)
        logins, onbekend = [], []
        for n in pp["namen"][:2]:
            wie = persoon_bij_naam(n, personen)
            logins.append(wie["whaller"] if wie else "")
            if not wie:
                onbekend.append(n)
        punten[j]["pdf"] = {"namen": pp["namen"], "woordvoerder": logins[0] if logins else "",
                            "zit_erbij": logins[1] if len(logins) > 1 else "", "onbekend": onbekend, "nr": i}
    return over
