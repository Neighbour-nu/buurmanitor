"""De vaste Whaller-berichten: fractie-, commissie- en raadsvergadering (bijlage D van de beschrijving).

Alles hier is zonder database en zonder netwerk, zodat het te testen is. main.py haalt de agenda's
uit Notubiz (notubiz.py), de woordvoerders uit de database en de vaste links uit de instellingen.

Het resultaat is HTML die je met 'Kopieer met opmaak' in de editor van Whaller plakt, plus een
checklist voor wat alleen in Whaller zelf kan (map, event, vastpinnen).
"""
import datetime as dt
import difflib
import html
import re

DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september",
           "oktober", "november", "december"]
RAAD_VOORAF = re.compile(r"hooravond|informatieavond|hoorzitting|beeldvormend|presentatie", re.I)


def d(datum):
    """6-10-2026, zonder voorloopnullen, zoals in jullie berichten."""
    return f"{datum.day}-{datum.month}-{datum.year}"


def lang(datum):
    return f"{DAGEN[datum.weekday()]} {datum.day} {MAANDEN[datum.month - 1]} {datum.year}"


def e(tekst):
    return html.escape(tekst or "", quote=False)


def a(url, tekst=None):
    if not url:
        return e(tekst or "")
    return f'<a href="{html.escape(url)}">{e(tekst or url)}</a>'


def sleutel(onderwerp):
    """Onderwerp vergelijkbaar maken: zonder hoofdletters, leestekens, tijden en 'Hamerstuk:'."""
    s = (onderwerp or "").lower()
    s = re.sub(r"^hamerstuk:\s*", "", s)
    s = re.sub(r"\(?\d{1,2}[.:]\d{2}\s*-\s*\d{1,2}[.:]\d{2}\s*(uur)?\)?", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def zoek_toewijzing(onderwerp, toewijzingen, soort=None):
    """Laatste toewijzing voor dit onderwerp; eerst exact, dan op gelijkenis (bijvoorbeeld als de
    titel in de raad iets anders is dan in de commissie)."""
    k = sleutel(onderwerp)
    kandidaten = [t for t in toewijzingen if not soort or t["soort"] == soort]
    exact = [t for t in kandidaten if t["sleutel"] == k]
    if not exact:
        beste = difflib.get_close_matches(k, list({t["sleutel"] for t in kandidaten}), n=1, cutoff=0.82)
        exact = [t for t in kandidaten if beste and t["sleutel"] == beste[0]]
    if not exact:
        return None
    return max(exact, key=lambda t: (t["datum"], t.get("id", 0)))


def vermelding(login):
    return f"@{login}" if login else "@?"


# ---------- commissie ----------

def commissie_punten(avond, toewijzingen):
    """De agendapunten van een commissieavond die in het bericht komen, met voorgestelde namen."""
    uit = []
    for c in avond["commissies"]:
        for p in c["agenda"]:
            if p["standaard"] or not p["sectie"] or p["diepte"] > 0:
                continue
            t = zoek_toewijzing(p["onderwerp"], toewijzingen, "commissie")
            vast = t and t["datum"] == avond["start"].date()
            uit.append({**p, "commissie": c["naam"], "zaal": c["plek"],
                        "woordvoerder": t["woordvoerder"] if t else "", "zit_erbij": t["zit_erbij"] if t else "",
                        "bron": "vastgelegd" if vast else ("eerder" if t else "")})
    return uit


def commissie_bericht(avond, punten, inst, map_url=""):
    datum = avond["start"].date()
    regels = [f"<h1><strong>Commissievergadering {d(datum)}</strong></h1>"]
    dag = DAGEN[datum.weekday()]
    if dag != (inst.get("raad_dag") or "dinsdag"):
        regels.append(f'<p><span style="color:hsl(0,75%,60%);"><strong>Let op, dit is een {dag}!</strong></span></p>')
    regels.append("<p>Bijdragen uiterlijk zondagavond plaatsen in deze map:</p>")
    regels.append(f"<p>{a(map_url or inst.get('rood_map_commissie'), map_url or '[map ' + datum.isoformat() + ']')}</p>")
    for c in avond["commissies"]:
        eigen = [p for p in punten if p["commissie"] == c["naam"]]
        if not eigen:
            continue
        regels.append(f"<p><strong>{e(c['naam'])}{' (' + e(c['plek']) + ')' if c['plek'] else ''}</strong></p>")
        sectie = None
        for p in eigen:
            if p["sectie"] != sectie:
                if sectie is not None:
                    regels.append("</ul>")
                sectie = p["sectie"]
                regels.append(f"<p><strong>{e(sectie)}</strong></p><ul>")
            tijd = f" - ({p['tijd']} uur)" if p["tijd"] else ""
            namen = vermelding(p["woordvoerder"]) + (f" &amp; {vermelding(p['zit_erbij'])}" if p["zit_erbij"] else "")
            regels.append(f"<li>{e(p['onderwerp'])}{tijd} | {namen}</li>")
        regels.append("</ul>")
    regels.append("<p>Link naar Notubiz:</p>")
    regels.append(f"<p>{a(avond['url'])}</p>")
    return "\n".join(regels)


# ---------- raad ----------

def raad_woordvoerder(onderwerp, toewijzingen, raadsleden):
    """In de raad spreekt een raadslid. Eerst een vastgelegde raadstoewijzing, anders de commissie:
    de woordvoerder als die raadslid is, anders de tweede naam als die raadslid is."""
    t = zoek_toewijzing(onderwerp, toewijzingen, "raad")
    if t and t["woordvoerder"]:
        return t["woordvoerder"], "raad"
    t = zoek_toewijzing(onderwerp, toewijzingen, "commissie")
    if t:
        for login in (t["woordvoerder"], t["zit_erbij"]):
            if login and login in raadsleden:
                return login, "commissie"
    return "", ""


def raad_indeling(verg, toewijzingen, raadsleden):
    """Agendapunten in rubrieken: bijzonder (voor de hamerstukken), hamer, bespreek, besloten."""
    bijzonder, hamer, bespreek, besloten = [], [], [], []
    for p in verg["agenda"]:
        s = (p["sectie"] or "").lower()
        if p["diepte"] > 0:
            doel = bespreek if "bespreek" in s else hamer if "hamer" in s else besloten if "beslot" in s else None
            if doel:  # deelpunten (zoals 9a en 9b) onder het laatste hoofdpunt
                doel[-1]["sub"].append(p["onderwerp"])
            continue
        if p["standaard"] or p["onderwerp"].lower().startswith("moties vreemd"):
            continue
        item = {**p, "sub": []}
        if "hamer" in s:
            hamer.append(item)
        elif "bespreek" in s:
            item["woordvoerder"], item["bron"] = raad_woordvoerder(p["onderwerp"], toewijzingen, raadsleden)
            bespreek.append(item)
        elif "beslot" in s:
            besloten.append(item)
        elif not s:
            bijzonder.append(item)
    return {"bijzonder": bijzonder, "hamer": hamer, "bespreek": bespreek, "besloten": besloten}


def _lijst(items, met_naam=False):
    if not items:
        return ""
    regels = ["<ol>"]
    for i in items:
        naam = f" ({vermelding(i.get('woordvoerder'))})" if met_naam else ""
        sub = "".join(f"<li>{e(s)}</li>" for s in i.get("sub", []))
        regels.append(f"<li>{e(i['onderwerp'])}{naam}" + (f"<ol>{sub}</ol>" if sub else "") + "</li>")
    regels.append("</ol>")
    return "\n".join(regels)


def raad_bericht(verg, indeling, inst, invoer, vooraf=(), map_url=""):
    datum = verg["start"].date()
    regels = [f"<h1><strong>{e(verg['naam'] or 'Raadsvergadering')} {d(datum)}</strong></h1>"]
    dag = DAGEN[datum.weekday()]
    if dag != (inst.get("raad_dag") or "dinsdag"):
        regels.append(f'<p><span style="color:hsl(0,75%,60%);"><strong>Let op, dit is een {dag}!</strong></span></p>')
    for v in vooraf:
        regels.append(f"<p>Vooraf om {v['start']:%H.%M} uur: {e(v['naam'].lower() if v['naam'] else '')}"
                      f"{' in de ' + e(v['plek']) if v['plek'] else ''}.</p>")
    regels.append("<p>Bijdragen uiterlijk zondagavond plaatsen in deze map:</p>")
    regels.append(f"<p>{a(map_url or inst.get('rood_map_raad'), map_url or '[map ' + datum.isoformat() + ']')}</p>")
    extra = [{"onderwerp": r, "sub": []} for r in invoer.get("bijzonder", [])]
    regels.append("<p><strong>Bijzondere agendapunten voor PRO Helmond</strong></p>")
    regels.append(_lijst(indeling["bijzonder"] + extra))
    regels.append("<p><strong>Hamerstukken</strong></p>")
    regels.append(_lijst(indeling["hamer"]))
    regels.append("<p><strong>Bespreekstukken</strong></p>")
    regels.append(_lijst(indeling["bespreek"], met_naam=True))
    regels.append("<p><strong>Moties vreemd aan de orde van de dag</strong></p>")
    regels.append(_lijst([{"onderwerp": r, "sub": []} for r in invoer.get("moties", [])]))
    regels.append("<p><strong>Besloten agendapunten</strong></p>")
    regels.append(_lijst(indeling["besloten"]))
    regels.append("<p>Link naar Notubiz:</p>")
    regels.append(f"<p>{a(verg['url'])}</p>")
    return "\n".join(r for r in regels if r)


# ---------- fractie ----------

def fractie_datum(week_maandag, inst):
    dag = (inst.get("fractie_dag") or "maandag").lower()
    return week_maandag + dt.timedelta(days=DAGEN.index(dag) if dag in DAGEN else 0)


def fractie_bericht(datum, inst, invoer, vergaderingen):
    """vergaderingen: lijst van (label, whaller_url) voor punt 4, bijvoorbeeld de commissie van die week."""
    def sub(regels):
        regels = [r for r in regels if r.strip()]
        return ("<ol>" + "".join(f"<li>{e(r)}</li>" for r in regels) + "</ol>") if regels else ""

    tijd = (invoer.get("tijd") or "").strip()
    standaard_tijd = (inst.get("fractie_tijd") or "19.00").strip()
    delen = [f'<h2><span style="color:rgb(91,183,91);"><strong>#fractievergadering</strong></span>&nbsp;{d(datum)}</h2>']
    if tijd and tijd.replace(":", ".") != standaard_tijd.replace(":", "."):
        delen.append(f"<p><strong>Let op: we beginnen om {e(tijd.replace(':', '.'))} uur.</strong></p>")
    delen += ["<p><strong>Afwezig</strong>:&nbsp;</p>", "<p><strong>Later:</strong>&nbsp;</p>",
              "<p><strong>Agenda:</strong></p>", "<ol>"]
    delen.append(f"<li>Mededelingen{sub(invoer.get('mededelingen', []))}</li>")
    delen.append(f"<li>{a(inst.get('groen_map_notulen'), 'Notulen vorige fractievergadering')}</li>")
    delen.append(f"<li>Ingebrachte agendapunten{sub(invoer.get('ingebracht', []))}</li>")
    for label, url, punten in vergaderingen:
        delen.append(f"<li>{a(url, label)}{sub(punten)}</li>")
    delen.append(f"<li>Overige politieke zaken{sub(invoer.get('overig', []))}</li>")
    org = invoer.get("organisatorisch")
    delen.append(f"<li>Organisatorisch{sub(org if org is not None else ['Kaartjes te versturen deze week'])}</li>")
    delen.append(f"<li>{a(inst.get('groen_map_agenda'), 'Agenda / Bijeenkomsten')}"
                 f"{sub(['Terugblik bijgewoonde bijeenkomsten en activiteiten'])}</li>")
    delen.append("<li>Rondvraag</li></ol>")
    formulier = inst.get("groen_formulier")
    if formulier:
        delen.append(f"<p>Wil je een item toevoegen aan de agenda? Vul dit formulier in:&nbsp;{a(formulier)}</p>")
    return "\n".join(delen)


# ---------- checklists ----------

def _tijd_plus(tijd, uren):
    u, m = (int(x) for x in tijd.replace(":", ".").split("."))
    totaal = u * 60 + m + uren * 60
    return f"{totaal // 60:02d}.{totaal % 60:02d}"


def checklist(soort, datum, inst, invoer=None):
    invoer = invoer or {}
    if soort == "fractie":
        tijd = (invoer.get("tijd") or inst.get("fractie_tijd") or "19.00").replace(":", ".")
        return [
            f"Plaats in de groene sfeer ({inst.get('groen_sfeer') or 'groene sfeer'}). De tag fractievergadering komt vanzelf uit #fractievergadering.",
            f"Voeg vóór het plaatsen het event toe: Fractievergadering {d(datum)}, {lang(datum)} van {tijd} tot {_tijd_plus(tijd, 2)} uur, "
            f"{inst.get('fractie_locatie') or 'Huis voor de Stad, ruimte De Fabriek'}, met de knoppen voor aanwezigheid. Achteraf kan dat niet meer.",
            "Pin het bericht vast en maak de pin van het vorige fractiebericht los.",
        ]
    map_sleutel = "rood_map_commissie" if soort == "commissie" else "rood_map_raad"
    titel = "Commissievergadering" if soort == "commissie" else "Raadsvergadering"
    return [
        f"Maak in Whaller de map {datum.isoformat()} aan in {'Commissievergaderingen' if soort == 'commissie' else 'Raadsvergaderingen'}"
        f" en zet de link hieronder bij 'Link naar de map'. Dan staat die goed in het bericht.",
        f"Plaats in de rode sfeer ({inst.get('rood_sfeer') or 'rode sfeer'}).",
        f"Voeg vóór het plaatsen het event toe: {titel} {d(datum)}, {lang(datum)} van "
        f"{(inst.get('raad_tijd') or '19.30-22.30').replace('-', ' tot ')} uur, {inst.get('raad_locatie') or 'Huis voor de Stad'}. Achteraf kan dat niet meer.",
        "Pin het bericht vast en maak de pin van het vorige bericht los.",
        "Zet daarna de link van het geplaatste bericht hieronder; dan komt die in het fractiebericht.",
    ]


def raad_vooraf(evenementen, verg):
    """Bijeenkomsten voor de hele raad op dezelfde dag, vóór de raadsvergadering (zoals een hooravond)."""
    return [v for v in evenementen if v["soort"] == "overig" and v["start"] and verg["start"]
            and v["start"].date() == verg["start"].date() and v["start"] < verg["start"]
            and RAAD_VOORAF.search(v["naam"] or "")]
