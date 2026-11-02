"""HTML-mail veilig tonen: opschonen op de server en in een afgeschermd kader (iframe) in de pagina.

Twee lagen:
1. Opschonen (hier): scripts, frames, objecten, formulieren, meta-refresh, base, externe stylesheets, event-attributen
   (onclick en dergelijke) en javascript:-links gaan eruit. Links krijgen target=_blank.
2. Het kader: sandbox zonder allow-scripts (er kan niets draaien), met een Content-Security-Policy die alleen inline
   opmaak toestaat en afbeeldingen uit de mail zelf. Afbeeldingen van buiten (die afzenders gebruiken om te zien dat je
   de mail opende) laden pas als je daarvoor kiest.
Ingesloten afbeeldingen (cid:) worden vervangen door de bijlage zelf (data:), als die klein genoeg is.
"""
import html
import re
from html.parser import HTMLParser

WEG_MET_INHOUD = {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "noscript", "template", "svg", "math", "title"}
WEG = {"base", "meta", "link", "form", "input", "button", "select", "textarea", "option"}
LEEG = {"br", "hr", "img", "col", "area", "wbr", "source", "track"}
URL_ATTR = {"href", "src", "background", "action", "poster", "srcset", "lowsrc", "dynsrc", "xlink:href"}
VEILIG_SCHEMA = re.compile(r"^\s*(https?:|mailto:|tel:|cid:|data:image/(png|jpe?g|gif|webp);|#)", re.I)
CSS_GEVAAR = re.compile(r"expression\s*\(|javascript:|behavior\s*:|-moz-binding|@import", re.I)


EXTERN = re.compile(r"^\s*https?:", re.I)
CSS_URL_EXTERN = re.compile(r"""url\(\s*['"]?\s*https?:[^)]*\)""", re.I)


class _Schoner(HTMLParser):
    def __init__(self, cid, beelden=True):
        super().__init__(convert_charrefs=False)
        self.uit, self.diepte_weg, self.cid, self.beelden, self.tegengehouden = [], 0, cid or {}, beelden, 0

    def _attrs(self, tag, attrs):
        goed = []
        for naam, waarde in attrs:
            naam = (naam or "").lower()
            waarde = waarde or ""
            if naam.startswith("on") or naam in ("formaction", "srcdoc", "http-equiv"):
                continue
            if naam in URL_ATTR:
                if naam == "srcset":
                    continue  # lastig te controleren; src blijft
                w = html.unescape(waarde).strip()
                if w.lower().startswith("cid:"):
                    w = self.cid.get(w[4:].strip("<>"), "")
                    if not w:
                        continue
                elif not VEILIG_SCHEMA.match(w):
                    continue
                if not self.beelden and naam in ("src", "background", "poster", "lowsrc", "dynsrc") and EXTERN.match(w):
                    self.tegengehouden += 1  # afbeelding van buiten: pas laden als Martijn dat wil
                    continue
                waarde = w
            if naam == "style" and CSS_GEVAAR.search(waarde):
                continue
            if naam == "style" and not self.beelden and CSS_URL_EXTERN.search(waarde):
                self.tegengehouden += 1
                waarde = CSS_URL_EXTERN.sub("none", waarde)
            goed.append((naam, waarde))
        if tag == "a":
            goed = [(n, w) for n, w in goed if n not in ("target", "rel")] + [("target", "_blank"), ("rel", "noopener noreferrer")]
        return goed

    def _tag(self, tag, attrs, eind=""):
        a = "".join(f' {n}="{html.escape(w, quote=True)}"' for n, w in attrs)
        self.uit.append(f"<{tag}{a}{eind}>")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in WEG_MET_INHOUD:
            if tag not in LEEG:
                self.diepte_weg += 1
            return
        if self.diepte_weg or tag in WEG:
            return
        self._tag(tag, self._attrs(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.diepte_weg or tag in WEG or tag in WEG_MET_INHOUD:
            return
        self._tag(tag, self._attrs(tag, attrs), " /")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in WEG_MET_INHOUD:
            self.diepte_weg = max(0, self.diepte_weg - 1)
            return
        if self.diepte_weg or tag in WEG or tag in LEEG:
            return
        self.uit.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.diepte_weg:
            self.uit.append(data)

    def handle_entityref(self, name):
        if not self.diepte_weg:
            self.uit.append(f"&{name};")

    def handle_charref(self, name):
        if not self.diepte_weg:
            self.uit.append(f"&#{name};")

    def handle_comment(self, data):
        pass  # ook conditionele Outlook-opmerkingen weg

    def handle_decl(self, decl):
        pass

    def unknown_decl(self, data):
        pass


def schoon(h, cid=None, beelden=True):
    """De HTML van een mail, opgeschoond. cid: {content-id: data-url} voor ingesloten afbeeldingen. Met beelden=False
    gaan afbeeldingen van buiten er al op de server uit (ook als achtergrond in de opmaak). Pure functie."""
    return schoon_met_telling(h, cid, beelden)[0]


def schoon_met_telling(h, cid=None, beelden=True):
    """Als schoon, plus het aantal tegengehouden afbeeldingen van buiten. Pure functie."""
    s = _Schoner(cid, beelden)
    s.feed(h or "")
    s.close()
    uit = "".join(s.uit)
    teller = [s.tegengehouden]

    def stijl(m):
        blok = m.group(0)
        if CSS_GEVAAR.search(blok):
            return ""
        if not beelden and CSS_URL_EXTERN.search(blok):
            teller[0] += 1
            return CSS_URL_EXTERN.sub("none", blok)
        return blok
    return re.sub(r"(?is)<style[^>]*>.*?</style>", stijl, uit), teller[0]


def heeft_externe_beelden(h):
    """Staan er afbeeldingen van buiten in (img, achtergrond of css-url)? Pure functie."""
    return bool(re.search(r"""(?i)(?:src|background)\s*=\s*["']\s*https?:|url\(\s*['"]?\s*https?:""", h or ""))


def document(h, beelden=False):
    """Het volledige document voor het afgeschermde kader, met de beveiligingsregels erin. Pure functie."""
    beleid = ("default-src 'none'; style-src 'unsafe-inline'; font-src data:; "
              + ("img-src data: https: http:;" if beelden else "img-src data:;"))
    return ('<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{beleid}">'
            '<base target="_blank"><meta name="referrer" content="no-referrer">'
            '<style>html,body{margin:0;padding:0}body{font:15px/1.5 Barlow,Arial,sans-serif;color:#1A1A1A;overflow-wrap:anywhere}'
            'img{max-width:100%;height:auto}table{max-width:100%!important}a{color:#14427E}'
            '@media (max-width:640px){table[width],td[width]{width:100%!important}img[width]{width:auto!important}}</style></head><body>'
            + h + "</body></html>")


LINK = re.compile(r"(https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]])")


def linkify(tekst):
    """Platte tekst als veilige HTML, met klikbare links. Pure functie."""
    delen, vorige = [], 0
    for m in LINK.finditer(tekst or ""):
        url = m.group(1)
        delen += [html.escape(tekst[vorige:m.start()]),
                  f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(url)}</a>']
        vorige = m.end()
    return "".join(delen) + html.escape((tekst or "")[vorige:])
