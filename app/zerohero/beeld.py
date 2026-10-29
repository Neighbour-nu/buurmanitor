"""Uitgelicht beeld in ZEROHERO-huisstijl (1800 x 1200).

Zwarte achtergrond (Athletic Black), vier groene hoekhaken (Heroic Green, arm 100 px, dikte 26 px,
48 px van de rand), het witte woordmerk linksboven en daaronder de sterkste zin of een staafgrafiek.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

B, H = 1800, 1200
ZWART = (28, 28, 28)
GROEN = (112, 219, 130)
GRIJS = (237, 237, 237)
ZACHT = (150, 150, 150)
MAP = os.path.join(os.path.dirname(__file__), "fonts")


def _font(naam, grootte):
    return ImageFont.truetype(os.path.join(MAP, naam), grootte)


def _hoekjes(d):
    arm, dik, rand = 100, 26, 48
    for x, y, sx, sy in [(rand, rand, 1, 1), (B - rand, rand, -1, 1), (rand, H - rand, 1, -1), (B - rand, H - rand, -1, -1)]:
        d.rectangle([min(x, x + sx * arm), min(y, y + sy * dik), max(x, x + sx * arm), max(y, y + sy * dik)], fill=GROEN)
        d.rectangle([min(x, x + sx * dik), min(y, y + sy * arm), max(x, x + sx * dik), max(y, y + sy * arm)], fill=GROEN)


def _woordmerk(img):
    wm = Image.open(os.path.join(MAP, "woordmerk.png")).convert("RGBA")
    breedte = 420
    wm = wm.resize((breedte, int(wm.height * breedte / wm.width)), Image.LANCZOS)
    img.paste(wm, (150, 140), wm)


def _regels(tekst, font, max_breedte, d):
    woorden, regels, huidig = tekst.split(), [], ""
    for w in woorden:
        test = (huidig + " " + w).strip()
        if d.textlength(test, font=font) <= max_breedte:
            huidig = test
        else:
            if huidig:
                regels.append(huidig)
            huidig = w
    if huidig:
        regels.append(huidig)
    return regels


def _zin(img, d, zin, sport):
    zin = (zin or "").strip().strip('"').strip()
    vak_l, vak_r, vak_t, vak_o = 150, B - 150, 300, H - 230
    for grootte in (120, 108, 96, 86, 76, 68, 60):
        f = _font("Anton.woff", grootte)
        regels = _regels(zin.upper(), f, vak_r - vak_l, d)
        hoogte = len(regels) * grootte * 1.12
        if hoogte <= vak_o - vak_t:
            break
    y = vak_t + ((vak_o - vak_t) - hoogte) / 2
    for i, r in enumerate(regels):
        d.text((vak_l, y + i * grootte * 1.12), r, font=f, fill=GROEN if i == 0 else GRIJS)
    _voet(d, sport)


def _voet(d, sport):
    f = _font("BarlowCondensed-SemiBold.woff", 40)
    tekst = ("KEEPERSNIEUWS" + (" · " + sport.upper() if sport else ""))
    d.text((150, H - 175), tekst, font=f, fill=ZACHT)
    f2 = _font("BarlowCondensed-SemiBold.woff", 40)
    d.text((B - 150 - d.textlength("ZEROHERO.ONE", font=f2), H - 175), "ZEROHERO.ONE", font=f2, fill=ZACHT)


def _grafiek(img, d, g, sport):
    titel = str(g.get("titel") or "")[:90]
    eenheid = str(g.get("eenheid") or "")
    reeksen = [r for r in (g.get("reeksen") or []) if isinstance(r, dict)][:8]
    waarden = [float(r.get("waarde") or 0) for r in reeksen]
    if not reeksen or max(waarden) <= 0:
        return _zin(img, d, titel, sport)
    ft = _font("Anton.woff", 72)
    regels = _regels(titel.upper(), ft, B - 300, d)[:2]
    for i, r in enumerate(regels):
        d.text((150, 290 + i * 82), r, font=ft, fill=GRIJS)
    top = 290 + len(regels) * 82 + 50
    onder = H - 240
    links, rechts = 150, B - 150
    n = len(reeksen)
    ruimte = (rechts - links) / n
    staaf = min(170, ruimte * 0.62)
    hoogste = max(waarden)
    fl = _font("BarlowCondensed-SemiBold.woff", 38)
    fw = _font("Anton.woff", 52)
    for i, (r, w) in enumerate(zip(reeksen, waarden)):
        cx = links + ruimte * (i + 0.5)
        h = (onder - top - 110) * (w / hoogste)
        kleur = GROEN if r.get("accent", i == 0) else GRIJS
        d.rectangle([cx - staaf / 2, onder - 60 - h, cx + staaf / 2, onder - 60], fill=kleur)
        wt = (f"{w:g}" if w != int(w) else f"{int(w)}") + eenheid
        d.text((cx - d.textlength(wt, font=fw) / 2, onder - 60 - h - 74), wt, font=fw, fill=kleur)
        label = str(r.get("label") or "")[:18]
        d.text((cx - d.textlength(label, font=fl) / 2, onder - 48), label, font=fl, fill=GRIJS)
    _voet(d, sport)


def maak_beeld(kernzin="", grafiek=None, sport=""):
    img = Image.new("RGB", (B, H), ZWART)
    d = ImageDraw.Draw(img)
    _hoekjes(d)
    _woordmerk(img)
    if isinstance(grafiek, dict) and grafiek.get("reeksen"):
        _grafiek(img, d, grafiek, sport)
    else:
        _zin(img, d, kernzin, sport)
    uit = io.BytesIO()
    img.save(uit, "PNG", optimize=True)
    return uit.getvalue()
