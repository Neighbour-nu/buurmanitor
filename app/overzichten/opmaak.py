"""De opmaak van een overzicht: mail (html en platte tekst) en Telegram. Pure functies, zodat ze makkelijk te testen zijn.

Een sectie: {"code", "titel", "items", "regel", "link": (tekst, url) of None, "alleen_eigen"}. Zie onderdelen.py voor de items.
"""
import html
import re

from ..kern.web import DAGEN, MAANDEN

GRIJS, TEKST, ROOD = "#5C5C5C", "#1A1A1A", "#C00000"


def e(t):
    return html.escape(str(t or ""), quote=True)


def datum_tekst(d):
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} {d.year}"


def invullen(tekst, datum, ontvanger=None):
    """{datum}, {dag}, {naam} en {voornaam} in onderwerp, inleiding en afsluiting. Onbekende {...} blijven staan."""
    naam = (ontvanger or {}).get("naam") or ""
    waarden = {"datum": datum_tekst(datum), "dag": DAGEN[datum.weekday()], "naam": naam, "voornaam": naam.split(" ")[0] if naam else ""}
    uit = re.sub(r"\{(datum|dag|naam|voornaam)\}", lambda m: waarden[m.group(1)], tekst or "")
    # "Goedemorgen ," als de naam ontbreekt: netjes maken.
    return re.sub(r"\s+([,.!])", r"\1", uit) if not naam else uit


def voor_ontvanger(secties, ontvanger):
    """Wat deze ontvanger te zien krijgt. Andere ontvangers dan Martijn zelf: geen onderdelen die alleen voor hem zijn, en
    geen links naar Buurmanitor (daar kunnen ze niet inloggen). Lege secties vallen weg."""
    eigen = (ontvanger or {}).get("rol") == "eigen"
    uit = []
    for s in secties:
        if s.get("alleen_eigen") and not eigen:
            continue
        items = [dict(i) for i in s.get("items") or []]
        if not eigen:
            for i in items:
                i.pop("intern", None)
        if not items and not s.get("regel"):
            continue
        uit.append({**s, "items": items, "link": s.get("link") if eigen else None})
    return uit


def _alinea(tekst):
    return "<br>".join(e(r) for r in (tekst or "").strip().splitlines())


def mail_html(o, secties, datum, ontvanger=None, voet=""):
    """De mail. o: het overzicht (kop, kleur, intro, afsluiting). Tabellen en inline stijl, zodat het in elk mailprogramma
    (ook op de telefoon) goed staat."""
    kleur = o.get("kleur") or "#FF0000"
    kop = o.get("kop") or o.get("naam") or "Overzicht"
    d = ['<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>',
         f'<body style="margin:0;padding:0;background:#F4F4F4"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
         f'style="background:#F4F4F4"><tr><td align="center" style="padding:16px 8px">',
         f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#FFFFFF;'
         f'font-family:Arial,Helvetica,sans-serif;color:{TEKST};font-size:15px;line-height:1.45">',
         f'<tr><td style="border-top:6px solid {e(kleur)};padding:20px 24px 8px">'
         f'<div style="font-size:22px;font-weight:bold;letter-spacing:.5px;color:{e(kleur)}">{e(kop)}</div>'
         f'<div style="color:{GRIJS};font-size:14px">{e(datum_tekst(datum).capitalize())}</div></td></tr>']
    intro = invullen(o.get("intro"), datum, ontvanger)
    if intro.strip():
        d.append(f'<tr><td style="padding:8px 24px">{_alinea(intro)}</td></tr>')
    if not secties:
        d.append(f'<tr><td style="padding:8px 24px;color:{GRIJS}">Geen nieuws, geen nieuwe stukken en niets dat vandaag je aandacht vraagt.</td></tr>')
    for s in secties:
        if s["code"] == "samenvatting":
            d.append(f'<tr><td style="padding:12px 24px"><div style="background:#F6F6F6;border-left:4px solid {e(kleur)};padding:10px 14px">'
                     f'<div style="font-weight:bold;margin-bottom:4px">{e(s["titel"])}</div>{_alinea(s.get("regel"))}</div></td></tr>')
            continue
        aantal = f' <span style="color:{GRIJS};font-weight:normal;font-size:13px">({len(s["items"])})</span>' if len(s["items"]) > 1 else ""
        d.append(f'<tr><td style="padding:16px 24px 4px"><div style="font-size:17px;font-weight:bold;border-bottom:2px solid {e(kleur)};'
                 f'padding-bottom:3px">{e(s["titel"])}{aantal}</div></td></tr><tr><td style="padding:0 24px">')
        if s.get("regel"):
            d.append(f'<p style="margin:8px 0">{_alinea(s["regel"])}</p>')
        for i in s["items"]:
            url = i.get("url") or i.get("intern") or ""
            titel = f'<a href="{e(url)}" style="color:{TEKST};font-weight:bold;text-decoration:underline">{e(i["titel"])}</a>' if url \
                else f'<b>{e(i["titel"])}</b>'
            voor = ""
            if i.get("label"):
                voor += f'<span style="color:{e(kleur)};font-weight:bold">{e(i["label"])}</span> '
            if i.get("wanneer"):
                voor += f'<span style="color:{GRIJS}">{e(i["wanneer"])}</span> '
            na = f' <span style="color:{GRIJS}">· {e(i["bron"])}</span>' if i.get("bron") else ""
            if i.get("let_op"):
                na += f' <b style="color:{ROOD}">! {e(i["let_op"])}</b>'
            onder = f'<div style="color:{GRIJS};font-size:14px">{e(i["tekst"])}</div>' if i.get("tekst") else ""
            d.append(f'<p style="margin:8px 0">{voor}{titel}{na}{onder}</p>')
        if s.get("link"):
            d.append(f'<p style="margin:6px 0 0;font-size:14px"><a href="{e(s["link"][1])}" style="color:{e(kleur)}">{e(s["link"][0])} &rsaquo;</a></p>')
        d.append("</td></tr>")
    slot = invullen(o.get("afsluiting"), datum, ontvanger)
    if slot.strip():
        d.append(f'<tr><td style="padding:16px 24px 4px">{_alinea(slot)}</td></tr>')
    if voet:
        d.append(f'<tr><td style="padding:16px 24px 20px;color:{GRIJS};font-size:12px;border-top:1px solid #E5E5E5">{voet}</td></tr>')
    d.append("</table></td></tr></table></body></html>")
    return "".join(d)


def platte_tekst(o, secties, datum, ontvanger=None, voet=""):
    regels = [(o.get("kop") or o.get("naam") or "Overzicht").upper(), datum_tekst(datum).capitalize(), ""]
    intro = invullen(o.get("intro"), datum, ontvanger).strip()
    if intro:
        regels += [intro, ""]
    if not secties:
        regels += ["Geen nieuws, geen nieuwe stukken en niets dat vandaag je aandacht vraagt.", ""]
    for s in secties:
        regels.append(s["titel"].upper())
        if s.get("regel"):
            regels.append(s["regel"])
        for i in s["items"]:
            delen = [x for x in (i.get("label"), i.get("wanneer"), i.get("titel")) if x]
            regel = "- " + " ".join(delen) + (f" ({i['bron']})" if i.get("bron") else "") + (f" ! {i['let_op']}" if i.get("let_op") else "")
            url = i.get("url") or i.get("intern")
            regels.append(regel + (f"\n  {url}" if url else "") + (f"\n  {i['tekst']}" if i.get("tekst") else ""))
        if s.get("link"):
            regels.append(f"{s['link'][0]}: {s['link'][1]}")
        regels.append("")
    slot = invullen(o.get("afsluiting"), datum, ontvanger).strip()
    if slot:
        regels += [slot, ""]
    if voet:
        regels.append(html.unescape(re.sub(r"<[^>]+>", "", voet)))
    return "\n".join(regels).strip() + "\n"


def telegram(o, secties, datum):
    """Korte versie (platte tekst, onder 3.500 tekens) voor Telegram: titels zonder toelichting."""
    regels = [f"{o.get('kop') or o.get('naam')} | {datum_tekst(datum)}"]
    for s in secties:
        regels.append(f"\n{s['titel']}")
        if s.get("regel"):
            regels.append(s["regel"])
        for i in s["items"]:
            regels.append("• " + " ".join(x for x in (i.get("label"), i.get("wanneer"), i.get("titel")) if x)
                          + (f" ! {i['let_op']}" if i.get("let_op") else ""))
    if len(regels) == 1:
        regels.append("\nNiets nieuws sinds gisteren.")
    tekst = "\n".join(regels)
    return tekst if len(tekst) < 3500 else tekst[:3400] + "\n…"
