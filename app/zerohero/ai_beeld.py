"""Beeld bij een keepersnieuwsbericht: een AI-sfeerbeeld met de ZEROHERO-huisstijl erover.

- Het sfeerbeeld komt van OpenAI (ZEROHERO_OPENAI_API_KEY) of Google Gemini (ZEROHERO_GEMINI_API_KEY); de eerste die
  is ingesteld telt (instellen: deploy/zerohero-sleutel.sh beeld). Zonder sleutel, of als het mislukt: het gewone
  huisstijlbeeld (zwart, groene hoeken, de kop).
- Nooit een herkenbaar persoon, logo, clubembleem, rugnummer of tekst in het AI-beeld: het blijft een sfeerbeeld (een
  doel, handschoenen, een leeg stadion, een bal in het net). Zo is er geen gedoe met rechten of met een nepfoto van
  een echte keeper.
- Daaroverheen: een donker verloop, de groene hoekhaken, het woordmerk, het sporticoon van ZEROHERO rechtsboven, het
  label (KEEPERSNIEUWS · VOETBAL) en de kop. Het huisstijlbeeld krijgt hetzelfde sporticoon.
- PRIJS: wat één beeld ongeveer kost (dollar), voor het maandbudget. Een schatting; de echte prijs staat bij de aanbieder.
"""
import base64
import io
import json
import os
import urllib.error
import urllib.request

from PIL import Image, ImageDraw

from . import beeld as hs

OPENAI_KEY = os.environ.get("ZEROHERO_OPENAI_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("ZEROHERO_GEMINI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("ZEROHERO_BEELD_MODEL_OPENAI", "gpt-image-1")
GEMINI_MODEL = os.environ.get("ZEROHERO_BEELD_MODEL_GEMINI", "gemini-2.5-flash-image")

PRIJS = {"openai": float(os.environ.get("ZEROHERO_BEELD_PRIJS_OPENAI", "0.07")),
         "google": float(os.environ.get("ZEROHERO_BEELD_PRIJS_GOOGLE", "0.04"))}
ICONEN = os.path.join(os.path.dirname(__file__), "fonts", "sport")
SPORTNAMEN = {"voetbal": "voetbal", "handbal": "handbal", "hockey": "hockey", "veldhockey": "hockey", "ijshockey": "ijshockey",
              "waterpolo": "waterpolo"}

REGELS = ("Photorealistic editorial sports photograph, cinematic light, shallow depth of field, landscape 16:9. "
          "Strictly NO recognizable people: no faces. At most an anonymous goalkeeper seen from behind, in silhouette or cropped "
          "so the face is not visible. "
          "No text, no letters, no numbers, no logos, no club crests, no sponsor names, no jersey numbers, no flags. "
          "Every object must belong to the sport described below; never mix equipment from different sports. ")

# Wat bij de sport hoort en wat er nooit in mag (zodat er bijvoorbeeld geen keepershandschoenen bij handbal liggen).
SPORT_BEELD = {
    "voetbal": ("SPORT: association football (soccer). Setting: outdoor grass pitch (or artificial grass) with white line markings, "
                "a full-size goal with white posts and crossbar (7.32 m wide, 2.44 m high) and a net. Ball: size-5 football. "
                "The goalkeeper wears padded goalkeeper gloves and a long-sleeve keeper shirt in a colour different from the "
                "outfield players. NEVER show: indoor court floors, ice, skates, sticks, helmets, pucks, swimming pools."),
    "handbal": ("SPORT: team handball (indoor). Setting: indoor sports hall with a smooth coloured court floor (often blue, orange "
                "or grey) and the curved 6-metre goal-area line; goal 3 m wide and 2 m high with posts painted in alternating "
                "red-and-white (or black-and-white) stripes and a net. Ball: small handball with grip that fits in one hand. "
                "Handball goalkeepers do NOT wear gloves: NEVER show goalkeeper gloves. NEVER show: grass, outdoor pitches, "
                "football boots with studs, large footballs, sticks, helmets, ice."),
    "hockey": ("SPORT: field hockey. Setting: outdoor water-based artificial turf (usually blue or green, often wet) with the "
               "white semicircular shooting circle; goal 3.66 m wide and 2.14 m high with a low wooden backboard and sideboards "
               "and a net. Ball: small hard white ball; players use curved hockey sticks. The goalkeeper wears heavy protective "
               "gear: helmet with a face cage, large foam leg guards and kickers, padded hand protectors and a padded body "
               "protector under a contrasting smock. NEVER show: footballs, ice, skates, pucks, thin football keeper gloves, grass."),
    "ijshockey": ("SPORT: ice hockey. Setting: indoor ice rink with white ice, red goal line and blue goal crease, boards and "
                  "plexiglass. Goal: red metal frame 1.83 m wide and 1.22 m high with a white net. Puck: black rubber disc. "
                  "The goalie wears a goalie mask, large leg pads, a blocker and a catching glove, skates, and holds a goalie "
                  "stick with a wide paddle. NEVER show: grass, footballs, field hockey balls, handball goals."),
    "waterpolo": ("SPORT: water polo. Setting: swimming pool with clear blue water, a floating goal with a net in the water, "
                  "lane ropes or floating boundary markers. Ball: yellow water polo ball. The goalkeeper wears a red swim cap "
                  "with ear guards and treads water with arms raised. NEVER show: gloves, shoes, grass, a dry pitch, sticks."),
}
STANDAARD_SCENE = {
    "voetbal": "an empty football goal at dusk under floodlights, a ball resting on the penalty spot",
    "handbal": "an empty handball goal with red-and-white striped posts in a quiet indoor hall, a handball on the 6-metre line",
    "hockey": "an empty field hockey goal on wet blue turf under floodlights, a white ball in the shooting circle",
    "ijshockey": "an empty ice hockey goal on fresh ice under arena lights, a puck in the blue crease",
    "waterpolo": "a floating water polo goal on calm blue pool water, a yellow ball drifting in front of it",
}


def sport_prompt(sport):
    """De sportregels voor de beeldmaker. Pure functie."""
    s = sport_van(sport)
    if s:
        return SPORT_BEELD[s] + " "
    if sport:
        return f"SPORT: {sport}. Show only the setting and equipment that really belong to this sport. "
    return "Show only the setting and equipment that really belong to the sport in the scene. "


class BeeldFout(Exception):
    pass


def aanbieder():
    return "openai" if OPENAI_KEY else "google" if GEMINI_KEY else ""


def _post(url, body, headers, timeout=180):
    verzoek = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(verzoek, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = (json.loads(e.read() or b"{}").get("error") or {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise BeeldFout(f"Beeldmaker gaf fout {e.code}. {detail[:200]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise BeeldFout(f"Beeldmaker niet bereikbaar: {e}") from None


def sfeerbeeld(scene, sport=""):
    """Het AI-beeld als PNG-bytes (zonder huisstijl). BeeldFout als het niet lukt of er geen sleutel is."""
    standaard = STANDAARD_SCENE.get(sport_van(sport), "an empty goal under floodlights, seen from the pitch")
    prompt = REGELS + sport_prompt(sport) + "Scene: " + (scene or standaard)[:900]
    if OPENAI_KEY:
        j = _post("https://api.openai.com/v1/images/generations",
                  {"model": OPENAI_MODEL, "prompt": prompt, "size": "1536x1024", "quality": "medium", "n": 1},
                  {"Authorization": f"Bearer {OPENAI_KEY}"})
        b64 = ((j.get("data") or [{}])[0]).get("b64_json")
    elif GEMINI_KEY:
        j = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                  {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}}},
                  {"x-goog-api-key": GEMINI_KEY})
        delen = (((j.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        b64 = next(((p.get("inlineData") or p.get("inline_data") or {}).get("data") for p in delen
                    if (p.get("inlineData") or p.get("inline_data"))), None)
    else:
        raise BeeldFout("Er is geen sleutel voor een beeldmaker (deploy/zerohero-sleutel.sh beeld).")
    if not b64:
        raise BeeldFout("De beeldmaker gaf geen beeld terug (mogelijk geweigerd door het filter).")
    return base64.b64decode(b64)


def _vullend(img, b, h):
    """Bijsnijden en schalen tot het hele vlak gevuld is."""
    schaal = max(b / img.width, h / img.height)
    img = img.resize((round(img.width * schaal), round(img.height * schaal)), Image.LANCZOS)
    x, y = (img.width - b) // 2, (img.height - h) // 2
    return img.crop((x, y, x + b, y + h))


def sport_van(sport):
    return SPORTNAMEN.get((sport or "").strip().lower().replace("-", ""), "")


def icoon(img, sport, grootte=190):
    """Het witte sporticoon van ZEROHERO rechtsboven, binnen de hoekhaken (niets als de sport onbekend is)."""
    s = sport_van(sport)
    pad = os.path.join(ICONEN, f"{s}.png") if s else ""
    if not pad or not os.path.exists(pad):
        return img
    ic = Image.open(pad).convert("RGBA")
    ic.thumbnail((grootte, grootte), Image.LANCZOS)
    alfa = ic.getchannel("A").point(lambda a: int(a * 0.92))
    ic.putalpha(alfa)
    img = img.convert("RGBA")
    img.alpha_composite(ic, (img.width - 150 - ic.width, 120 + (grootte - ic.height) // 2))
    return img.convert("RGB")


# Keepersnieuws-beelden zijn 16:9: zo valt er in de overzichten van de site (Breakdance, 16:9 bijgesneden) niets weg.
B, H = 1920, 1080


def huisstijl(kop, sport):
    """Het huisstijlbeeld zonder AI: zwart, met dezelfde opmaak als het AI-beeld (1920 x 1080 PNG)."""
    zwart = io.BytesIO()
    Image.new("RGB", (B, H), hs.ZWART).save(zwart, "PNG")
    return samenstellen(zwart.getvalue(), kop, sport)


def samenstellen(achtergrond, kop, sport=""):
    """Huisstijl over een sfeerbeeld: 1920 x 1080 PNG."""
    img = _vullend(Image.open(io.BytesIO(achtergrond)).convert("RGB"), B, H)
    # Donker verloop: onderaan bijna zwart (voor de kop), bovenaan licht (voor het woordmerk).
    verloop = Image.new("L", (1, H))
    for y in range(H):
        verloop.putpixel((0, y), int(70 + 175 * (y / H) ** 1.6))
    zwart = Image.new("RGB", (B, H), hs.ZWART)
    img = Image.composite(zwart, img, verloop.resize((B, H)))
    d = ImageDraw.Draw(img)
    hs._hoekjes(d, B, H)  # pylint: disable=protected-access
    hs._woordmerk(img)  # pylint: disable=protected-access
    img = icoon(img, sport)
    d = ImageDraw.Draw(img)
    # Label: alleen KEEPERSNIEUWS en de sport
    fl = hs._font("BarlowCondensed-SemiBold.woff", 46)  # pylint: disable=protected-access
    naam = sport_van(sport) or (sport or "")
    tekst = "KEEPERSNIEUWS" + (f" · {naam.upper()}" if naam else "")
    # Kop: zo groot als past, onderaan
    vak_l, vak_r, onder = 150, B - 150, H - 150
    for grootte in (100, 92, 84, 76, 68, 60, 54):
        f = hs._font("Anton.woff", grootte)  # pylint: disable=protected-access
        regels = hs._regels((kop or "").upper(), f, vak_r - vak_l, d)  # pylint: disable=protected-access
        if len(regels) <= 3:
            break
    hoogte = len(regels) * grootte * 1.12
    y = onder - hoogte
    d.text((vak_l, y - 70), tekst, font=fl, fill=hs.GROEN)
    for i, r in enumerate(regels):
        d.text((vak_l, y + i * grootte * 1.12), r, font=f, fill=(255, 255, 255))
    uit = io.BytesIO()
    img.save(uit, "PNG", optimize=True)
    return uit.getvalue()


def maak(kop, scene, sport="", kernzin="", ai=True):
    """(png, bron, fout): het samengestelde AI-beeld, of anders (of met ai=False) het huisstijlbeeld met de reden waarom."""
    if aanbieder() and ai:
        try:
            return samenstellen(sfeerbeeld(scene, sport), kop, sport), f"ai-{aanbieder()}", ""
        except BeeldFout as e:
            fout = str(e)
        except Exception as e:  # noqa: BLE001  (een onleesbaar beeld en dergelijke)
            fout = f"Het AI-beeld lukte niet: {str(e)[:150]}"
    else:
        fout = ""
    return huisstijl(kop or kernzin, sport), "huisstijl", fout
