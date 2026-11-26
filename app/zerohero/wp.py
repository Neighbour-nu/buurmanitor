"""Koppeling met WordPress (zerohero.one) en Wikimedia Commons voor beelden bij concepten."""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

WP_URL = os.environ.get("ZEROHERO_WP_URL", "https://zerohero.one").rstrip("/")
WP_USER = os.environ.get("ZEROHERO_WP_USER", "")
WP_APP_PASSWORD = os.environ.get("ZEROHERO_WP_APP_PASSWORD", "")
UA = "Doellijn/1.0 (ZEROHERO cockpit; martijn@zerohero.one)"


def wp_actief():
    return bool(WP_USER and WP_APP_PASSWORD)


class WpFout(urllib.error.HTTPError):
    """Een fout van WordPress, met de stap en de melding van WordPress erbij (blijft een HTTPError met .code)."""

    def __init__(self, e, methode, pad, inhoud):
        super().__init__(e.url, e.code, e.reason, e.headers, None)
        self.methode, self.pad, self.melding = methode, pad, melding_uit(inhoud)

    def __str__(self):
        return f"WordPress gaf {self.code} bij {stap(self.methode, self.pad)}" + (f": {self.melding}" if self.melding else "")


def stap(methode, pad):
    """Een leesbare naam voor een WordPress-aanroep. Pure functie."""
    p = pad.split("?")[0]
    if p == "/wp/v2/media":
        return "het beeld uploaden"
    if p.startswith("/wp/v2/media/"):
        return "de beschrijving van het beeld"
    if p == "/wp/v2/posts":
        return "het bericht aanmaken" if methode == "POST" else "berichten opvragen"
    if p.startswith("/wp/v2/posts/"):
        return "het bericht bijwerken of publiceren" if methode == "POST" else "het bericht opvragen"
    if p.startswith("/wp/v2/categories"):
        return "de categorie"
    return f"{methode} {p}"


def melding_uit(inhoud):
    """De melding uit een foutantwoord van WordPress (JSON of HTML), kort en zonder opmaak. Pure functie."""
    tekst = (inhoud or b"").decode("utf-8", "replace") if isinstance(inhoud, (bytes, bytearray)) else str(inhoud or "")
    try:
        j = json.loads(tekst)
    except ValueError:
        j = None
    delen = []
    if isinstance(j, dict):
        delen = [str(j.get("message") or ""), str(((j.get("data") or {}).get("error") or {}).get("message") or "")
                 if isinstance(j.get("data"), dict) and isinstance(j["data"].get("error"), dict) else "", str(j.get("code") or "")]
    else:
        delen = [tekst]
    schoon = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", d)).strip() for d in delen]
    return " · ".join(d for d in schoon if d)[:300]


def _wp(methode, pad, data=None, headers=None, ruw=False, timeout=60):
    token = base64.b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode()).decode()
    h = {"Authorization": "Basic " + token, "User-Agent": UA}
    if headers:
        h.update(headers)
    if data is not None and not ruw:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(WP_URL + "/wp-json" + pad, data=data, headers=h, method=methode)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            inhoud = e.read()[:20000]
        except Exception:  # noqa: BLE001
            inhoud = b""
        raise WpFout(e, methode, pad, inhoud) from None


def upload(png_of_jpg, bestandsnaam, alt="", bijschrift="", mime="image/png"):
    m = _wp("POST", "/wp/v2/media", data=png_of_jpg, ruw=True,
            headers={"Content-Type": mime, "Content-Disposition": f'attachment; filename="{bestandsnaam}"'})
    try:  # alt-tekst en bijschrift: fijn, maar niet nodig om verder te kunnen
        _wp("POST", f"/wp/v2/media/{m['id']}", data={"alt_text": alt[:250], "caption": bijschrift})
    except WpFout:
        pass
    return m["id"], m.get("source_url", "")


def zet_uitgelicht(post_id, media_id):
    _wp("POST", f"/wp/v2/posts/{int(post_id)}", data={"featured_media": int(media_id)})


def media_url(media_id):
    m = _wp("GET", f"/wp/v2/media/{int(media_id)}?_fields=id,source_url,media_details")
    groot = (m.get("media_details") or {}).get("sizes", {}).get("medium_large") or {}
    return groot.get("source_url") or m.get("source_url", "")


def zoek_mediabibliotheek(term, aantal=6):
    q = urllib.parse.urlencode({"search": term, "media_type": "image", "per_page": aantal,
                                "_fields": "id,source_url,alt_text,title,media_details"})
    rijen = _wp("GET", "/wp/v2/media?" + q)
    uit = []
    for m in rijen or []:
        sizes = (m.get("media_details") or {}).get("sizes", {})
        thumb = (sizes.get("medium") or sizes.get("thumbnail") or {}).get("source_url") or m.get("source_url")
        uit.append({"bron": "media", "id": m["id"], "thumb": thumb,
                    "titel": re.sub("<[^>]+>", "", (m.get("title") or {}).get("rendered", "")),
                    "alt": m.get("alt_text", ""), "credit": "Mediabibliotheek zerohero.one"})
    return uit


def _schoon(html):
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", "", html or "")).strip()


def zoek_commons(term, aantal=6):
    q = urllib.parse.urlencode({"action": "query", "format": "json", "generator": "search",
                                "gsrsearch": f"filetype:bitmap {term}", "gsrnamespace": 6, "gsrlimit": aantal,
                                "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 1800})
    req = urllib.request.Request("https://commons.wikimedia.org/w/api.php?" + q, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    uit = []
    for p in sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda x: x.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        licentie = _schoon((meta.get("LicenseShortName") or {}).get("value"))
        if not licentie or "fair use" in licentie.lower():
            continue
        maker = _schoon((meta.get("Artist") or {}).get("value"))[:80] or "onbekend"
        uit.append({"bron": "commons", "id": p["title"], "thumb": ii.get("thumburl") or ii.get("url"),
                    "groot": ii.get("thumburl") or ii.get("url"),
                    "titel": p["title"].replace("File:", "").rsplit(".", 1)[0],
                    "credit": f"Foto: {maker}, {licentie}, via Wikimedia Commons",
                    "pagina": ii.get("descriptionurl", "")})
    return uit


def haal_commons(titel):
    """Haalt een Commons-foto op (1800 px breed) met de juiste naamsvermelding."""
    q = urllib.parse.urlencode({"action": "query", "format": "json", "titles": titel,
                                "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 1800})
    req = urllib.request.Request("https://commons.wikimedia.org/w/api.php?" + q, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    p = next(iter((data.get("query") or {}).get("pages", {}).values()), {})
    ii = (p.get("imageinfo") or [{}])[0]
    if not ii.get("url"):
        raise ValueError("Foto niet gevonden op Commons")
    meta = ii.get("extmetadata") or {}
    licentie = _schoon((meta.get("LicenseShortName") or {}).get("value")) or "zie Commons"
    maker = _schoon((meta.get("Artist") or {}).get("value"))[:80] or "onbekend"
    url = ii.get("thumburl") or ii["url"]
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        inhoud, mime = r.read(), r.headers.get("Content-Type", "image/jpeg")
    return inhoud, mime, {"titel": titel.replace("File:", "").rsplit(".", 1)[0],
                          "credit": f"Foto: {maker}, {licentie}, via Wikimedia Commons"}


def naar_prullenbak_als_concept(post_id):
    """Zet een WordPress-bericht in de prullenbak, maar alleen als het nog geen gepubliceerd bericht is."""
    p = _wp("GET", f"/wp/v2/posts/{int(post_id)}?context=edit&_fields=id,status")
    if p.get("status") in ("draft", "pending", "auto-draft", "future"):
        _wp("DELETE", f"/wp/v2/posts/{int(post_id)}")
        return True
    return False


# ---------- uitgelichte afbeelding ook bovenaan in het artikel ----------
# Het thema (Breakdance) toont het uitgelichte beeld niet in het bericht zelf. Daarom zet de cockpit het als eerste
# blok in de inhoud, herkenbaar aan de class zh-uitgelicht, zodat het bij een nieuw beeld wordt vervangen en nooit dubbel staat.

UITGELICHT_BLOK = re.compile(r'<!-- wp:image \{[^\n]*?"className":"zh-uitgelicht"[^\n]*?\} -->.*?<!-- /wp:image -->\s*', re.S)


def _esc(t):
    return (str(t or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


def beeld_blok(media_id):
    m = _wp("GET", f"/wp/v2/media/{int(media_id)}?context=edit&_fields=id,source_url,alt_text,caption,media_details")
    sizes = (m.get("media_details") or {}).get("sizes", {})
    groot = sizes.get("large") or sizes.get("medium_large") or {}
    url = groot.get("source_url") or m.get("source_url", "")
    maat = "large" if sizes.get("large") else "full"
    bijschrift = _schoon((m.get("caption") or {}).get("raw") or (m.get("caption") or {}).get("rendered") or "")
    fig = f'<figcaption class="wp-element-caption">{_esc(bijschrift)}</figcaption>' if bijschrift else ""
    return (f'<!-- wp:image {{"id":{int(media_id)},"sizeSlug":"{maat}","linkDestination":"none","className":"zh-uitgelicht"}} -->\n'
            f'<figure class="wp-block-image size-{maat} zh-uitgelicht"><img src="{_esc(url)}" alt="{_esc(m.get("alt_text"))}" '
            f'class="wp-image-{int(media_id)}"/>{fig}</figure>\n<!-- /wp:image -->\n\n')


def beeld_in_artikel(post_id):
    """Zet het uitgelichte beeld van een bericht bovenaan in de inhoud (of vervangt het vorige). Geeft True als er iets veranderde."""
    p = _wp("GET", f"/wp/v2/posts/{int(post_id)}?context=edit&_fields=id,featured_media,content")
    media = p.get("featured_media") or 0
    inhoud = (p.get("content") or {}).get("raw") or ""
    zonder = UITGELICHT_BLOK.sub("", inhoud, count=1).lstrip()
    nieuw = (beeld_blok(media) + zonder) if media else zonder
    if nieuw == inhoud:
        return False
    _wp("POST", f"/wp/v2/posts/{int(post_id)}", data={"content": nieuw})
    return True
