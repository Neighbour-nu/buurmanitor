"""Een werkruimte (zoals Promonitor) één op één onder een voorvoegsel draaien, zoals /politiek.

De oorspronkelijke app gebruikt absolute paden (/agenda, /inbox, RedirectResponse("/concepten/3")). Deze laag zet
die om naar /politiek/agenda enzovoort, zonder dat de code van de werkruimte hoeft te veranderen:
- Location- en HX-Redirect-koppen van doorverwijzingen;
- href, action, src, formaction en hx-get/hx-post (htmx) in HTML, en fetch('/...') en location='/...' in scripts.
Volledige adressen (https://...) en adressen die al met het voorvoegsel beginnen, blijven ongemoeid.
Bestanden die geen HTML zijn (Word, pdf, css, afbeeldingen) gaan ongewijzigd door.
"""
import re


def herschrijf_html(tekst, voorvoegsel):
    v = re.escape(voorvoegsel.strip("/"))
    attr = re.compile(r'(\b(?:href|action|src|formaction|hx-(?:get|post|put|patch|delete))=")/(?!/|' + v + r'(?:/|"|\?|#))')
    tekst = attr.sub(lambda m: m.group(1) + voorvoegsel + "/", tekst)
    # fetch('/...') en location='/...' of location.href = "/..." in scripts en onchange-attributen
    js = re.compile(r"(\bfetch\(\s*['\"]|\blocation(?:\.href)?\s*=\s*['\"])/(?!/|" + v + r"(?:/|['\"?#]))")
    return js.sub(lambda m: m.group(1) + voorvoegsel + "/", tekst)


def herschrijf_pad(waarde, voorvoegsel):
    if waarde.startswith("/") and not waarde.startswith("//") and not (
            waarde == voorvoegsel or waarde.startswith(voorvoegsel + "/") or waarde.startswith(voorvoegsel + "?")):
        return voorvoegsel + waarde
    return waarde


class Voorvoegsel:
    """ASGI-laag rond een werkruimte-app die onder `voorvoegsel` is gekoppeld (app.mount)."""

    def __init__(self, app, voorvoegsel):
        self.app = app
        self.voorvoegsel = "/" + voorvoegsel.strip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        staat = {"start": None, "html": False, "delen": []}

        async def verstuur(bericht):
            if bericht["type"] == "http.response.start":
                koppen = []
                for naam, waarde in bericht.get("headers", []):
                    n = naam.lower()
                    if n in (b"location", b"hx-redirect"):
                        waarde = herschrijf_pad(waarde.decode("latin-1"), self.voorvoegsel).encode("latin-1")
                    if n == b"content-type" and waarde.lower().startswith(b"text/html"):
                        staat["html"] = True
                    koppen.append((naam, waarde))
                bericht = dict(bericht, headers=koppen)
                if not staat["html"]:
                    return await send(bericht)
                staat["start"] = bericht
                return None
            if bericht["type"] == "http.response.body" and staat["html"]:
                staat["delen"].append(bericht.get("body", b""))
                if bericht.get("more_body"):
                    return None
                tekst = b"".join(staat["delen"]).decode("utf-8", errors="replace")
                lichaam = herschrijf_html(tekst, self.voorvoegsel).encode("utf-8")
                start = staat["start"]
                koppen = [(n, w) for n, w in start["headers"] if n.lower() != b"content-length"]
                koppen.append((b"content-length", str(len(lichaam)).encode()))
                await send(dict(start, headers=koppen))
                return await send({"type": "http.response.body", "body": lichaam, "more_body": False})
            return await send(bericht)

        return await self.app(scope, receive, verstuur)


class Doorgeven:
    """Stuurt verzoeken waarvan het pad met `begin` begint ongewijzigd naar een andere app.

    Voor de n8n-werkstromen: die roepen http://cockpit-app:8000/intern/... aan. Na de overstap heet de portal ook
    cockpit-app, en deze laag geeft /intern/... rechtstreeks aan de werkruimte ZEROHERO. De werkruimte controleert
    die verzoeken zelf (met het conceptgeheim), net als de oude Doellijn.
    """

    def __init__(self, app, begin, doel):
        self.app, self.begin, self.doel = app, begin, doel

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.begin):
            return await self.doel(scope, receive, send)
        return await self.app(scope, receive, send)
