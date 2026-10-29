"""Een prikkelende titel boven de LinkedIn-post.

LinkedIn toont op mobiel ongeveer de eerste 140 tekens van een post en op desktop ongeveer 210, daarna 'meer weergeven'.
In die ruimte beslist de lezer of hij doorleest, dus de eerste regel (de titel) en de openingszin moeten samen werken.
Claude maakt vier titels van verschillende soorten (getal, paradox, verhaal, vraag, tegendraads, nieuws); de beste staat
bovenaan de post, Martijn kan een andere kiezen of zelf een titel typen. Werkt voor alle concepten, ook die uit n8n.
"""
import json
import threading
import traceback

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import claude_api, opmaak
from .main import controleer_herkomst, ctx, q, q1, templates

router = APIRouter()

MIGRATIES = [
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS linkedin_titel text",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS linkedin_titels jsonb",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS linkedin_titel_status text",
]

SOORTEN = {"getal": "Getal", "paradox": "Paradox", "verhaal": "Verhaal", "vraag": "Vraag", "tegendraads": "Tegendraads", "nieuws": "Nieuws"}

REGELS = """Je schrijft de titel: de eerste regel boven een LinkedIn-post van ZEROHERO (zerohero.one), het platform van Martijn Buurman voor keepers en goalies uit alle sporten.

Waarom de titel ertoe doet: LinkedIn toont op mobiel ongeveer de eerste 140 tekens van een post en op desktop ongeveer 210, daarna staat er 'meer weergeven'. In die ruimte beslist de lezer in een fractie van een seconde of hij doorleest. De titel en de openingszin van de post staan daar samen, dus ze moeten elkaar versterken.

Regels voor elke titel:
- Eén regel, bij voorkeur 40 tot 70 tekens, nooit meer dan 80.
- Specifiek: een naam, een sport, een precies getal of een concreet beeld. Geen algemene wijsheid.
- Eén idee per titel, met een klein informatiegat dat de post echt invult. Beloof niets wat de post niet waarmaakt.
- Nuchter en prikkelend, nooit hoogdravend of schreeuwerig. Martijn houdt van een droge, scherpe titel.
- Geen clickbait en geen versleten sjablonen ('Unpopular opinion:', 'Dit verandert alles', 'Niemand praat over', 'Hier is waarom', 'Laat dat even bezinken'). Geen emoji, geen hashtags, geen hoofdletters voor nadruk, geen uitroepteken.
- Herhaal de openingszin van de post niet; de titel zet de toon, de opening bouwt erop voort.
- Gebruik alleen feiten en getallen die in de post of het artikel staan. Verzin niets.
- Nooit gedachtestreepjes (geen — en geen –); een dubbele punt of komma mag. Lev Yashin schrijf je zo.

Maak vier titels van verschillende soorten, gekozen uit:
- getal: begint bij een precies, opvallend getal uit de post
- paradox: zet twee dingen tegenover elkaar die niet lijken te kloppen (de kern van ZEROHERO: het beste keeperswerk is wat niet gebeurt)
- verhaal: begint midden in een scène en verklapt de afloop nog niet
- vraag: een open vraag die de lezer zelf niet direct kan beantwoorden, geen ja/nee-vraag
- tegendraads: spreekt een gangbaar idee tegen, alleen als de post dat onderbouwt
- nieuws: een feitelijke, heldere kop

Kies daarna de beste, gelet op deze post, de opening en de doelgroep (keepers, trainers, sportliefhebbers en mensen uit het bedrijfsleven die de keeper-mindset herkennen).

Antwoord met alleen JSON, zonder dubbele aanhalingstekens binnen de teksten:
{"titels": [{"titel": "...", "soort": "getal|paradox|verhaal|vraag|tegendraads|nieuws", "waarom": "één korte zin"}], "beste": 0}"""


def _platte_tekst(html):
    return opmaak.uit_html(html or "")[:6000]


def _maak(cid):
    c = q1("SELECT * FROM concepten WHERE id = %s", (cid,))
    try:
        user = (f"TITEL VAN HET ARTIKEL: {c['blog_titel'] or ''}\n\nLINKEDIN-POST (zonder titel):\n{c['linkedin'] or ''}\n\n"
                f"ARTIKEL (ter achtergrond):\n{_platte_tekst(c['blog_html'])}")
        tekst, _usage = claude_api.vraag(REGELS, user, max_tokens=4000)
        o = claude_api.json_uit(tekst, "titels")
        titels = []
        for t in o.get("titels") or []:
            if not isinstance(t, dict):
                continue
            titel = opmaak.zonder_streepjes(str(t.get("titel") or "")).strip().strip('"').rstrip(".")
            if titel:
                titels.append({"titel": titel[:120], "soort": str(t.get("soort") or "").lower(),
                               "waarom": opmaak.zonder_streepjes(str(t.get("waarom") or ""))[:200]})
        if not titels:
            raise ValueError("Claude gaf geen titels terug.")
        beste = o.get("beste") if isinstance(o.get("beste"), int) and 0 <= o.get("beste") < len(titels) else 0
        titels.insert(0, titels.pop(beste))
        q("""UPDATE concepten SET linkedin_titels = %s::jsonb, linkedin_titel = coalesce(linkedin_titel, %s),
               linkedin_titel_status = 'klaar' WHERE id = %s RETURNING 1""", (json.dumps(titels, ensure_ascii=False), titels[0]["titel"], cid))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        q("UPDATE concepten SET linkedin_titel_status = %s WHERE id = %s RETURNING 1", ("fout: " + str(e)[:200], cid))


def start(cid, opnieuw=False):
    c = q1("SELECT status, linkedin, linkedin_titel_status FROM concepten WHERE id = %s", (cid,))
    if not c or c["status"] != "klaar" or not c["linkedin"] or not claude_api.actief():
        return False
    if c["linkedin_titel_status"] == "bezig":
        return True
    if opnieuw:
        q("UPDATE concepten SET linkedin_titel = NULL WHERE id = %s RETURNING 1", (cid,))
    q("UPDATE concepten SET linkedin_titel_status = 'bezig' WHERE id = %s RETURNING 1", (cid,))
    threading.Thread(target=_maak, args=(cid,), daemon=True).start()
    return True


def _context(request, cid):
    c = q1("SELECT id, status, linkedin, linkedin_titel, linkedin_titels, linkedin_titel_status FROM concepten WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404)
    c = dict(c)
    if isinstance(c.get("linkedin_titels"), str):
        c["linkedin_titels"] = json.loads(c["linkedin_titels"])
    return templates.TemplateResponse("_li_post.html", ctx(request, c=c, soorten=SOORTEN, actief=claude_api.actief()))


@router.get("/concepten/{cid}/linkedin", response_class=HTMLResponse)
def li_blok(request: Request, cid: int):
    return _context(request, cid)


@router.post("/concepten/{cid}/linkedin/maak", response_class=HTMLResponse)
def li_maak(request: Request, cid: int, opnieuw: str = Form("")):
    controleer_herkomst(request)
    start(cid, opnieuw=bool(opnieuw))
    return _context(request, cid)


@router.post("/concepten/{cid}/linkedin/kies", response_class=HTMLResponse)
def li_kies(request: Request, cid: int, titel: str = Form("")):
    controleer_herkomst(request)
    titel = opmaak.zonder_streepjes(titel.strip())[:150]
    q("UPDATE concepten SET linkedin_titel = %s WHERE id = %s RETURNING 1", (titel or None, cid))
    return _context(request, cid)
