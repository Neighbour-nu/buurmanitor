"""Eenvoudige opmaak voor artikelen, zodat Martijn in een gewoon tekstvak kan schrijven en redigeren.

Opmaak (zoals in een e-mail of Markdown):
  ## Tussenkop            een tussenkop
  lege regel              nieuwe alinea
  - punt                  opsomming (1. 2. 3. voor een genummerde lijst)
  | a | b |               tabel (eerste regel is de kop; een regel als |---|---| mag, hoeft niet)
  **vet**  *cursief*  [tekst](https://link)
Daarvan maakt de cockpit HTML (voorbeeld en blog_html) en WordPress-blokken in de stijl van de site.
"""
import html
import re

KOP = re.compile(r"^#{1,4}\s+(.*)$")
PLEK = re.compile(r"^\[\[blok (\d+)(?::\s*([^\]]*))?\]\]$")
LIJST = re.compile(r"^\s*[-*•]\s+(.*)$")
GENUMMERD = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _inline(t):
    t = html.escape(t, quote=False)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)
    return t


def _cellen(regel):
    r = regel.strip()
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|"):
        r = r[:-1]
    return [c.strip() for c in r.split("|")]


def blokken(tekst):
    """Deelt de tekst op in blokken: ('kop', t), ('p', t), ('ul'|'ol', [items]), ('tabel', [rijen])."""
    regels = (tekst or "").replace("\r\n", "\n").split("\n")
    uit, alinea = [], []

    def sluit():
        if alinea:
            uit.append(("p", " ".join(x.strip() for x in alinea)))
            alinea.clear()

    i = 0
    while i < len(regels):
        r = regels[i]
        s = r.strip()
        if not s:
            sluit(); i += 1; continue
        m = PLEK.match(s)
        if m:
            sluit(); uit.append(("plek", (m.group(1), m.group(2) or ""))); i += 1; continue
        m = KOP.match(s)
        if m:
            sluit(); uit.append(("kop", m.group(1).strip().strip("*"))); i += 1; continue
        if s.startswith("|") and s.count("|") >= 2:
            sluit()
            rijen = []
            while i < len(regels) and regels[i].strip().startswith("|"):
                c = _cellen(regels[i])
                if not all(re.fullmatch(r":?-{2,}:?", x or "--") for x in c):
                    rijen.append(c)
                i += 1
            if rijen:
                uit.append(("tabel", rijen))
            continue
        for soort, patroon in (("ul", LIJST), ("ol", GENUMMERD)):
            if patroon.match(r):
                sluit()
                items = []
                while i < len(regels) and patroon.match(regels[i]):
                    items.append(patroon.match(regels[i]).group(1).strip()); i += 1
                uit.append((soort, items))
                break
        else:
            alinea.append(s); i += 1
    sluit()
    return uit


def naar_html(tekst):
    """Gewone HTML (voor het voorbeeld en voor blog_html in de cockpit)."""
    delen = []
    for soort, inhoud in blokken(tekst):
        if soort == "plek":
            delen.append(f'<p class="blok-plek">▣ Vast onderdeel {inhoud[0]}: {html.escape(inhoud[1])} (blijft zoals het op de site staat)</p>')
        elif soort == "kop":
            delen.append(f"<h2>{_inline(inhoud)}</h2>")
        elif soort == "p":
            delen.append(f"<p>{_inline(inhoud)}</p>")
        elif soort in ("ul", "ol"):
            delen.append(f"<{soort}>" + "".join(f"<li>{_inline(x)}</li>" for x in inhoud) + f"</{soort}>")
        elif soort == "tabel":
            kop, *rest = inhoud
            delen.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in kop) + "</tr></thead><tbody>"
                         + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest) + "</tbody></table>")
    return "\n".join(delen)


def naar_wp(tekst, vaste_blokken=None):
    """WordPress-blokken in de stijl van de bestaande berichten (witte tussenkoppen, h3).
    vaste_blokken: {nummer: originele blok-HTML} voor regels als [[blok 3: afbeelding]] (bij een bestaand bericht)."""
    out = []
    vaste_blokken = vaste_blokken or {}
    for soort, inhoud in blokken(tekst):
        if soort == "plek":
            if str(inhoud[0]) in vaste_blokken:
                out.append(vaste_blokken[str(inhoud[0])].strip())
        elif soort == "kop":
            out.append('<!-- wp:heading {"level":3,"style":{"elements":{"link":{"color":{"text":"var:preset|color|white"}}}},"textColor":"white"} -->\n'
                       f'<h3 class="wp-block-heading has-white-color has-text-color has-link-color">{_inline(inhoud)}</h3>\n<!-- /wp:heading -->')
        elif soort == "p":
            out.append(f"<!-- wp:paragraph -->\n<p>{_inline(inhoud)}</p>\n<!-- /wp:paragraph -->")
        elif soort in ("ul", "ol"):
            items = "\n".join(f"<!-- wp:list-item -->\n<li>{_inline(x)}</li>\n<!-- /wp:list-item -->" for x in inhoud)
            attr = ' {"ordered":true}' if soort == "ol" else ""
            out.append(f'<!-- wp:list{attr} -->\n<{soort} class="wp-block-list">{items}</{soort}>\n<!-- /wp:list -->')
        elif soort == "tabel":
            kop, *rest = inhoud
            tabel = ("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in kop) + "</tr></thead><tbody>"
                     + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest) + "</tbody>")
            out.append(f'<!-- wp:table -->\n<figure class="wp-block-table"><table>{tabel}</table></figure>\n<!-- /wp:table -->')
    return "\n\n".join(out)


def uit_html(h):
    """Zet HTML (bijvoorbeeld van een bestaand bericht) om naar de eenvoudige opmaak."""
    t = re.sub(r"<!--.*?-->", "", h or "", flags=re.S)
    t = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", lambda m: "\n\n## " + re.sub("<[^>]+>", "", m.group(1)).strip() + "\n\n", t, flags=re.S)
    t = re.sub(r"<li[^>]*>(.*?)</li>", lambda m: "\n- " + m.group(1).strip(), t, flags=re.S)
    t = re.sub(r"<tr[^>]*>(.*?)</tr>", lambda m: "\n| " + " | ".join(re.sub("<[^>]+>", "", c).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", m.group(1), re.S)) + " |", t, flags=re.S)
    t = re.sub(r"<(strong|b)>(.*?)</\1>", r"**\2**", t, flags=re.S)
    t = re.sub(r"<(em|i)>(.*?)</\1>", r"*\2*", t, flags=re.S)
    t = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", t, flags=re.S)
    t = re.sub(r"</p>|<br\s*/?>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def zonder_streepjes(t):
    return re.sub(r"\s*[—–]\s*", ", ", t or "").replace(", ,", ",")


# ---------- een bestaand WordPress-bericht omzetten naar de eenvoudige opmaak ----------

WP_COMMENT = re.compile(r"<!--\s*(/?)wp:([a-z0-9/-]+)(\s+\{.*?\})?\s*(/?)-->", re.S)
OMZETBAAR = {"paragraph", "core/paragraph", "heading", "core/heading", "list", "core/list", "table", "core/table"}
LABELS = {"image": "afbeelding", "gallery": "fotogalerij", "embed": "video of embed", "video": "video", "html": "HTML-blok",
          "buttons": "knoppen", "separator": "scheidingslijn", "quote": "citaat", "pullquote": "citaat", "columns": "kolommen",
          "group": "groep", "spacer": "witruimte", "audio": "audio", "file": "bestand", "shortcode": "shortcode"}


def _label(naam, inhoud):
    kort = naam.split("/")[-1]
    label = LABELS.get(kort, kort)
    tekst = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inhoud)).strip()
    alt = re.search(r'alt="([^"]+)"', inhoud)
    extra = (alt.group(1) if alt else tekst)[:50]
    return f"{label}{', ' + extra if extra else ''}".replace("]", ")")


def topniveau_blokken(raw):
    """Geeft [(naam, attrs, volledige_html)] van de blokken op het hoogste niveau, of None als er geen blokken zijn."""
    uit, stapel, start = [], [], None
    for m in WP_COMMENT.finditer(raw or ""):
        dicht, naam, attrs, zelf = m.group(1), m.group(2), m.group(3) or "", m.group(4)
        if zelf:
            if not stapel:
                uit.append((naam, attrs, m.group(0)))
            continue
        if not dicht:
            if not stapel:
                start = m.start()
            stapel.append(naam)
        elif stapel:
            stapel.pop()
            if not stapel:
                uit.append((naam, attrs, raw[start:m.end()]))
    return uit if uit else None


def wp_naar_opmaak(raw):
    """Zet de inhoud van een bestaand bericht om. Wat niet als tekst te bewerken is (foto's, video's, knoppen), wordt een
    vaste regel [[blok N: omschrijving]] en komt bij het opslaan ongewijzigd terug. Het uitgelichte beeld bovenaan
    (zh-uitgelicht) laat de cockpit weg; dat zet hij na het opslaan opnieuw."""
    vaste, delen = {}, []
    blokken_ = topniveau_blokken(raw)
    if blokken_ is None:
        # Klassieke inhoud zonder blokken: foto's en embeds apart houden, de rest als tekst.
        stukken = re.split(r"(<figure.*?</figure>|<iframe.*?</iframe>|<img[^>]*>|<blockquote.*?</blockquote>)", raw or "", flags=re.S | re.I)
        for st in stukken:
            if not st.strip():
                continue
            if re.match(r"<(figure|iframe|img|blockquote)", st, re.I):
                if "zh-uitgelicht" in st:
                    continue
                n = str(len(vaste) + 1)
                vaste[n] = st
                delen.append(f"[[blok {n}: {_label('afbeelding' if 'img' in st.lower() else 'embed', st)}]]")
            else:
                t = uit_html(st)
                if t:
                    delen.append(t)
        return "\n\n".join(delen), vaste
    for naam, attrs, h in blokken_:
        if "zh-uitgelicht" in attrs or "zh-uitgelicht" in h[:400]:
            continue
        if naam in OMZETBAAR:
            binnen = WP_COMMENT.sub("", h)
            t = uit_html(binnen)
            if naam.endswith("heading"):
                t = "## " + re.sub(r"^#+\s*", "", t).strip()
            if t.strip():
                delen.append(t.strip())
        else:
            n = str(len(vaste) + 1)
            vaste[n] = h
            delen.append(f"[[blok {n}: {_label(naam, WP_COMMENT.sub('', h))}]]")
    return "\n\n".join(delen), vaste
