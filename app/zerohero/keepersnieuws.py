"""Keepersnieuws: Buurmanitor stelt korte nieuwsberichten voor de sectie Keepersnieuws op zerohero.one voor.

Route
1. Elke vijftien minuten kijkt de planner naar het gewogen nieuws (items, gevuld door n8n): berichten van de laatste
   36 uur met een score vanaf de drempel, die nog niet in een voorstel of concept zitten. Berichten over dezelfde keeper
   of hetzelfde voorval worden samengenomen.
2. Claude beoordeelt of het keepersnieuws is voor de sectie (transfer, unieke prestatie, record of statistiek, groot in
   het nieuws) en schrijft dan een eigen bericht in de ZEROHERO-stijl, met bronvermelding. Wat Martijn eerder besliste
   (en waarom), en zijn eigen richtlijnen, gaan mee in de opdracht: zo leert Buurmanitor.
3. Er komt een beeld bij: een AI-sfeerbeeld met de huisstijl (zie ai_beeld.py), anders het huisstijlbeeld.
4. Via Telegram komen beeld, kop en tekst binnen met drie knoppen: Plaatsen (meteen live, in de categorie
   Keepersnieuws), Ik kijk zelf (concept in WordPress) of Niet plaatsen (met een reden). Op de pagina Keepersnieuws in
   de Doellijn kan hetzelfde, en kun je de tekst eerst aanpassen.

Kosten in de hand
- Eerst een snelle, goedkope voorselectie (Haiku, zonder zoeken): is dit keepersnieuws? Alleen dan schrijft het
  schrijfmodel met zoekopdrachten het bericht en komt er een AI-beeld bij.
- Grenzen: hooguit kn_per_dag voorstellen per dag, kn_per_ronde per kwartier, kn_zoeken zoekopdrachten per bericht.
- Maandbudget (kn_budget, euro): is het op, dan stopt Buurmanitor met voorstellen tot de volgende maand. Een nieuw
  AI-beeld wordt dan een huisstijlbeeld. De kosten zijn een schatting op basis van tokens, zoekopdrachten en beelden.
- AI-beelden kunnen uit (kn_ai_beeld): dan altijd het (gratis) huisstijlbeeld.
"""
import os
import datetime as dt
import html
import json
import re
import threading
import traceback

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..kern import config as kern_config, planner, telegram
from . import ai_beeld, claude_api, omgeving, wp
from .main import CONCEPT_SECRET, STATUS_OPEN, clusteren, controleer_herkomst, ctx, q, q1, templates, zelfde

router = APIRouter()

MIGRATIES = [
    "CREATE TABLE IF NOT EXISTS instellingen (sleutel text PRIMARY KEY, waarde text, bijgewerkt timestamptz NOT NULL DEFAULT now())",
    """CREATE TABLE IF NOT EXISTS nieuws_voorstellen (
        id serial PRIMARY KEY, aangemaakt timestamptz NOT NULL DEFAULT now(), bijgewerkt timestamptz NOT NULL DEFAULT now(),
        item_ids bigint[] NOT NULL, keepers text[] NOT NULL DEFAULT '{}', sport text, score int,
        status text NOT NULL DEFAULT 'bezig',
        categorie text, titel text, intro text, tekst text, kernzin text, beeld_scene text, bronnen jsonb, oordeel text,
        beeld bytea, beeld_bron text, beeld_fout text,
        telegram_foto int, telegram_tekst int,
        besluit text, reden text, toelichting text, besloten_op timestamptz, besloten_via text,
        wp_post_id int, wp_link text, fout text, tokens_in int, tokens_out int, zoekopdrachten int)""",
    "CREATE INDEX IF NOT EXISTS nieuws_voorstellen_items ON nieuws_voorstellen USING gin (item_ids)",
    "ALTER TABLE nieuws_voorstellen ADD COLUMN IF NOT EXISTS tokens_in_snel int",
    "ALTER TABLE nieuws_voorstellen ADD COLUMN IF NOT EXISTS tokens_out_snel int",
    "ALTER TABLE nieuws_voorstellen ADD COLUMN IF NOT EXISTS beelden jsonb NOT NULL DEFAULT '{}'",
    "ALTER TABLE nieuws_voorstellen ADD COLUMN IF NOT EXISTS gevoelig boolean NOT NULL DEFAULT false",
    "ALTER TABLE nieuws_voorstellen ADD COLUMN IF NOT EXISTS wp_media_id int",
]
SNEL = os.environ.get("ZEROHERO_KN_SNEL_MODEL", "claude-haiku-4-5")

CATEGORIEEN = {"transfer": "Transfer", "prestatie": "Unieke prestatie", "statistiek": "Record of statistiek", "groot": "Groot in het nieuws",
               "memoriam": "In memoriam"}
REDENEN = {"geen_nieuws": "Geen echt nieuws", "niet_relevant": "Niet interessant voor ZEROHERO", "klopt_niet": "Klopt niet of te dun",
           "oud": "Te laat, al oud nieuws", "dubbel": "Al eerder gebracht", "tekst": "Tekst of kop niet goed", "beeld": "Beeld niet goed"}
STATUSSEN = {"bezig": "Wordt geschreven", "klaar": "Klaar", "verstuurd": "Wacht op jou", "geplaatst": "Geplaatst", "later": "Concept in WordPress",
             "afgewezen": "Niet geplaatst", "overgeslagen": "Geen keepersnieuws", "fout": "Mislukt"}
STANDAARD = {"kn_aan": "1", "kn_drempel": "60", "kn_per_dag": "5", "kn_per_ronde": "2", "kn_richtlijnen": "", "kn_categorie_id": "",
             "kn_zoeken": "2", "kn_budget": "15", "kn_ai_beeld": "1", "kn_voorselectie": "1", "kn_via_n8n": "0",
             "kn_hoofdcat_klaar": "0"}
_slot = threading.Lock()


# ---------------------------------------------------------------- instellingen

def inst():
    rijen = q("SELECT sleutel, waarde FROM instellingen WHERE sleutel = ANY(%s)", (list(STANDAARD),))
    return STANDAARD | {r["sleutel"]: r["waarde"] or "" for r in rijen}


def zet(sleutel, waarde):
    q("""INSERT INTO instellingen (sleutel, waarde) VALUES (%s, %s)
         ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now() RETURNING 1""", (sleutel, str(waarde)))


def _getal(i, sleutel):
    try:
        return int(i[sleutel])
    except (TypeError, ValueError):
        return int(STANDAARD[sleutel])


# ---------------------------------------------------------------- kandidaten

def kandidaten(i=None, nu=None):
    """Verhalen (clusters) die nog een voorstel kunnen worden, hoogste score eerst."""
    i = i or inst()
    rijen = q("""SELECT * FROM items WHERE binnen > now() - interval '36 hours' AND score >= %s AND status = ANY(%s)
                 AND NOT EXISTS (SELECT 1 FROM nieuws_voorstellen v WHERE v.item_ids && ARRAY[items.id])
                 AND NOT EXISTS (SELECT 1 FROM concepten c WHERE c.item_ids && ARRAY[items.id])
                 ORDER BY score DESC NULLS LAST LIMIT 120""", (_getal(i, "kn_drempel"), list(STATUS_OPEN)))
    # Al gebracht (of al beoordeeld): dezelfde keeper in een echt voorstel van de laatste vijf dagen, of hetzelfde
    # bericht (kop, link of verhaal) in een voorstel van de laatste week, ook als het via een andere feed opnieuw binnenkwam.
    recent = [dict(r) for r in q("""SELECT keepers, NULL AS verhaal FROM nieuws_voorstellen WHERE aangemaakt > now() - interval '5 days'
                                    AND status NOT IN ('fout', 'overgeslagen')""")]
    recent += [dict(r) | {"keepers": []} for r in q("""SELECT i.titel, i.url, i.verhaal FROM items i
                     JOIN nieuws_voorstellen v ON i.id = ANY(v.item_ids)
                     WHERE v.aangemaakt > now() - interval '7 days' AND v.status <> 'fout'""")]
    return [v for v in clusteren([dict(r) for r in rijen])
            if not any(zelfde(lid, r) for lid in (v.get("leden") or [v]) for r in recent)]


def vandaag_gemaakt():
    return q1("""SELECT count(*) AS n FROM nieuws_voorstellen WHERE aangemaakt::date = (now() AT TIME ZONE 'Europe/Amsterdam')::date
                 AND status NOT IN ('overgeslagen', 'fout')""")["n"]


# ---------------------------------------------------------------- leren

def leerstof():
    """Wat Martijn eerder besliste, voor in de opdracht aan Claude."""
    i = inst()
    rijen = q("""SELECT categorie, sport, titel, besluit, reden, toelichting FROM nieuws_voorstellen
                 WHERE besluit IS NOT NULL ORDER BY besloten_op DESC LIMIT 40""")
    tel = q("""SELECT categorie, count(*) FILTER (WHERE besluit = 'geplaatst') AS ja, count(*) FILTER (WHERE besluit = 'later') AS later,
                      count(*) FILTER (WHERE besluit = 'afgewezen') AS nee FROM nieuws_voorstellen WHERE besluit IS NOT NULL GROUP BY categorie""")
    delen = []
    if i["kn_richtlijnen"].strip():
        delen.append("Richtlijnen van Martijn (gaan voor alles):\n" + i["kn_richtlijnen"].strip())
    if tel:
        delen.append("Hoe Martijn tot nu toe besliste, per soort:\n" + "\n".join(
            f"- {CATEGORIEEN.get(t['categorie'], t['categorie'] or 'onbekend')}: {t['ja']} geplaatst, {t['later']} zelf bekeken, {t['nee']} niet geplaatst"
            for t in tel))
    if rijen:
        delen.append("Zijn laatste besluiten (nieuwste eerst):\n" + "\n".join(
            f"- [{ {'geplaatst': 'GEPLAATST', 'later': 'ZELF BEKEKEN', 'afgewezen': 'NIET GEPLAATST'}.get(r['besluit'], r['besluit']) }] "
            f"{r['titel']} ({CATEGORIEEN.get(r['categorie'], r['categorie'] or '')}, {r['sport'] or ''})"
            + (f": {REDENEN.get(r['reden'], r['reden'])}" if r["reden"] else "") + (f" ({r['toelichting']})" if r["toelichting"] else "")
            for r in rijen))
    return "\n\n".join(delen) or "Nog geen besluiten van Martijn: volg de richtlijnen hieronder."


SYSTEEM = """Je bent de nieuwsredacteur van ZEROHERO (zerohero.one), het Nederlandstalige platform voor keepers en goalies uit
vijf sporten: voetbal, handbal, hockey, ijshockey en waterpolo. Je beoordeelt nieuws voor de sectie Keepersnieuws en
schrijft er een eigen kort bericht over.

Wat in de sectie hoort (categorie):
- transfer: een keeper verruilt van club, tekent bij, wordt verhuurd, of een grote transfer is officieel.
- prestatie: een unieke prestatie (wedstrijd beslist, penalty's gestopt, historische clean sheet, eerste keer ooit).
- statistiek: een record of mijlpaal in cijfers (aantal clean sheets, reddingspercentage, wedstrijden, jubileum).
- groot: een keeper is om een andere reden groot in het nieuws (blessure of ernstige ziekte van een bekende keeper,
  selectie, afscheid, ophef, prijs). Geen roddel of privézaken.
- memoriam: een bekende (oud-)keeper is overleden. Dat is nieuws voor ZEROHERO en hoort er altijd bij.
Niet: berichten waarin de keeper bijzaak is, voorbeschouwingen zonder nieuws, gokken, geruchten zonder betrouwbare bron,
en verhalen die al ouder zijn dan een paar dagen.

Schrijfregels (ZEROHERO-stijl):
- Nederlands, helder en direct, korte zinnen afgewisseld met langere. Feiten dragen het verhaal. Warm, nooit sentimenteel.
- NOOIT een gedachtestreepje (geen — en geen –). Gebruik een komma, punt of dubbele punt. Koppeltekens in samenstellingen mogen.
- Geen AI-achtige zinnen ("het is belangrijk om", "laten we eerlijk zijn"), geen clichés, geen verzonnen citaten.
- Puur sport: geen link naar bedrijfsleven, geen verwijzing naar het boek De Keeper-mindset.
- Alleen feiten die in de bronnen staan of die je met de zoekfunctie bevestigt. Twijfel je, laat het weg.
  Citaten alleen kort (onder 15 woorden), vertaald en met wie het zei; anders parafraseren.
- Noem de bron in de tekst ("meldt De Telegraaf", "maakte de club bekend").
- Kop: kort, feitelijk en prikkelend, met de naam van de keeper; geen vraag. Intro: één of twee zinnen met de kern.
  Tekst: drie tot vijf korte alinea's (150 tot 300 woorden), met waar het past het keepersperspectief of een
  Nederlandse haak. Geen tussenkoppen, geen hashtags.
- Overlijden en ernstige ziekte: wel brengen, met respect en ingehouden. Bij overlijden: wie, wanneer, op welke leeftijd,
  en wat de keeper betekende (clubs, prestaties, wat hem of haar bijzonder maakte); sluit af met een korte, waardige zin.
  Geen speculatie over de oorzaak, alleen wat familie, club of bond naar buiten bracht. Geen dramatische kop: zakelijk
  en waardig ("Oud-keeper Jan Jansen (78) overleden"). Bij ziekte: alleen wat de keeper of de club zelf meldde.
- Zelfdoding, depressie of verslaving: geen methode, omstandigheden of plaats; geen simpele oorzaak; noem depressie een
  ziekte; zet aan het eind: "Denk je aan zelfdoding? Praat erover: bel 113 of 0800-0113, of chat via 113.nl."
- Zet "gevoelig" op true bij overlijden, ernstige ziekte en de onderwerpen hierboven, zodat Martijn extra goed kijkt.
- Beeld bij gevoelig nieuws: ingetogen en stil (een leeg doel van de juiste sport in avondlicht), geen dramatiek.

Het beeld (beeld_scene, in het Engels): een sfeerbeeld dat past bij de sport én bij dit nieuws.
- Alleen wat echt bij de sport hoort. Voetbal: grasveld, wit doel, keepershandschoenen. Handbal: zaalvloer, doel met
  rood-wit gestreepte palen, kleine bal, GEEN handschoenen. Hockey: kunstgras (blauw of groen), doel met plank, witte bal,
  keeper met helm, legguards en handbeschermers. IJshockey: ijsbaan, rood doel, puck, goalie met masker, pads, blocker en
  vanger. Waterpolo: zwembad, drijvend doel, gele bal, rode cap. Nooit spullen van een andere sport.
- Bij het nieuws: transfer: het doel of de zaal in de clubkleuren van de nieuwe club, als sfeer en zonder logo (bijvoorbeeld
  rode stoeltjes op de tribune); prestatie of record: het moment van een redding, de keeper van achteren of als
  silhouet; groot nieuws: wat het verhaal zegt (bijvoorbeeld een lege kleedkamer bij een blessure); memoriam: stil en leeg.
- Geen herkenbare personen, geen tekst, cijfers, logo's of rugnummers. Beschrijf licht, plek en hoek concreet (2 tot 4 zinnen).

Antwoord ALLEEN met precies één JSON-object (de berichten samen zijn één verhaal; geen tekst ervoor of erna):
{"geschikt": true/false, "oordeel": "een zin: waarom wel of niet, met het leerpunt uit eerdere besluiten als dat meespeelt",
 "categorie": "transfer|prestatie|statistiek|groot|memoriam", "gevoelig": true/false, "titel": "...", "intro": "...", "tekst": "alinea's gescheiden door een lege regel",
 "kernzin": "de sterkste zin van hooguit 12 woorden, voor het beeld", "keepers": ["..."], "sport": "voetbal|handbal|hockey|ijshockey|waterpolo",
 "beeld_scene": "Engelse beschrijving van het sfeerbeeld volgens de regels hierboven",
 "bronnen": [{"titel": "...", "url": "..."}]}"""


def _vraag(verhaal, leer):
    leden = verhaal.get("leden") or [verhaal]
    berichten = "\n\n".join(
        f"Bericht {n + 1} ({x.get('bron') or 'bron onbekend'}, {x.get('gepubliceerd') or x.get('binnen')}): {x.get('titel') or ''}\n"
        f"URL: {x.get('url') or ''}\nSport: {x.get('sport') or ''}; competitie: {x.get('competitie') or ''}; keepers: {', '.join(x.get('keepers') or [])}\n"
        f"Samenvatting: {x.get('samenvatting') or ''}\nInvalshoek: {x.get('invalshoek') or ''}"
        for n, x in enumerate(leden[:6]))
    nu = dt.datetime.now(kern_config.TZ)
    return (f"Vandaag is het {nu:%d-%m-%Y}.\n\n# Wat Martijn eerder besliste\n{leer}\n\n# Het nieuws\n{berichten}\n\n"
            "Beoordeel en schrijf. Controleer met de zoekfunctie het kernfeit (wie, welke club, welk cijfer) als dat kan.")


def _schoon(t, n):
    t = re.sub(r"\s*[—–]\s*", ", ", str(t or "")).strip()
    return t[:n]


def schoon_antwoord(j):
    """Het JSON-antwoord van Claude nalopen. Pure functie."""
    if not isinstance(j, dict):
        raise ValueError("Geen JSON-object")
    cat = j.get("categorie") if j.get("categorie") in CATEGORIEEN else "groot"
    tekst = "\n\n".join(_schoon(a, 1500) for a in re.split(r"\n\s*\n", str(j.get("tekst") or "")) if a.strip())
    bronnen = [{"titel": _schoon(b.get("titel"), 150), "url": str(b.get("url"))[:500]} for b in (j.get("bronnen") or [])
               if isinstance(b, dict) and str(b.get("url") or "").startswith(("http://", "https://"))][:6]
    geschikt = bool(j.get("geschikt")) and bool(str(j.get("titel") or "").strip()) and bool(tekst)
    return {"geschikt": geschikt, "oordeel": _schoon(j.get("oordeel"), 400), "categorie": cat,
            "gevoelig": bool(j.get("gevoelig")) or cat == "memoriam", "titel": _schoon(j.get("titel"), 160),
            "intro": _schoon(j.get("intro"), 400), "tekst": tekst[:6000], "kernzin": _schoon(j.get("kernzin"), 120),
            "keepers": [_schoon(k, 80) for k in (j.get("keepers") or []) if str(k).strip()][:5],
            "sport": str(j.get("sport") or "")[:20].lower(), "beeld_scene": str(j.get("beeld_scene") or "")[:800], "bronnen": bronnen}


# ---------------------------------------------------------------- kosten

def kosten_voorstel(v, k=None):
    """Geschatte kosten van één voorstel in euro: tokens (snel en schrijven), zoekopdrachten en AI-beelden."""
    from . import kosten  # pylint: disable=import-outside-toplevel
    dollar = (kosten.dollar(v.get("tokens_in_snel"), v.get("tokens_out_snel"), SNEL)
              + kosten.dollar(v.get("tokens_in"), v.get("tokens_out"), claude_api.SCHRIJFMODEL, v.get("zoekopdrachten") or 0)
              + sum(ai_beeld.PRIJS.get(a, 0.07) * int(n) for a, n in (v.get("beelden") or {}).items()))
    return dollar * (k if k is not None else kosten.koers(q))


def kosten_maand():
    """(euro deze maand, aantal voorstellen) sinds de eerste van de maand."""
    from . import kosten  # pylint: disable=import-outside-toplevel
    k = kosten.koers(q)
    rijen = q("""SELECT tokens_in, tokens_out, tokens_in_snel, tokens_out_snel, zoekopdrachten, beelden FROM nieuws_voorstellen
                 WHERE aangemaakt >= date_trunc('month', now() AT TIME ZONE 'Europe/Amsterdam') AT TIME ZONE 'Europe/Amsterdam'""")
    return sum(kosten_voorstel(r, k) for r in rijen), len(rijen)


def budget_op(i=None):
    i = i or inst()
    try:
        budget = float(str(i["kn_budget"]).replace(",", "."))
    except ValueError:
        budget = float(STANDAARD["kn_budget"])
    return budget > 0 and kosten_maand()[0] >= budget


# ---------------------------------------------------------------- een voorstel maken

VOORSELECTIE = """Je bent de eindredacteur van ZEROHERO, een platform over keepers en goalies (voetbal, handbal, hockey, ijshockey,
waterpolo). Beoordeel snel of dit nieuws in de sectie Keepersnieuws hoort: een keeper wisselt van club (transfer), levert een
unieke prestatie, haalt een record of statistische mijlpaal, of is om een andere reden groot in het nieuws (ook een
ernstige blessure of ziekte van een bekende keeper). Het overlijden van een bekende (oud-)keeper hoort er altijd bij
(categorie memoriam). Niet: de keeper is bijzaak, voorbeschouwingen, geruchten zonder betrouwbare bron, oud nieuws, roddel.
Houd rekening met wat Martijn eerder besliste en met zijn richtlijnen.
De berichten gaan samen over één verhaal: beoordeel dat verhaal als geheel.
Antwoord ALLEEN met precies één JSON-object: {"geschikt": true/false, "categorie": "transfer|prestatie|statistiek|groot|memoriam", "oordeel": "een zin waarom"}"""


def voorselectie(verhaal, leer):
    """Snelle, goedkope beoordeling (zonder zoeken). Geeft (geschikt, categorie, oordeel, usage)."""
    tekst, usage, _ = claude_api.gesprek(VOORSELECTIE, [{"role": "user", "content": _vraag(verhaal, leer).replace(
        "Beoordeel en schrijf. Controleer met de zoekfunctie het kernfeit (wie, welke club, welk cijfer) als dat kan.", "Beoordeel.")}],
        model=SNEL, max_tokens=400, timeout=120)
    return (*samenvoegen_selectie(claude_api.json_uit(tekst, alle=True)), usage)


def samenvoegen_selectie(objecten):
    """Soms beoordeelt het snelle model elk bericht apart: dan telt het als geschikt als er één geschikt is. Pure functie."""
    objecten = [o for o in objecten if "geschikt" in o] or objecten
    j = next((o for o in objecten if o.get("geschikt") is True), objecten[0])
    return bool(j.get("geschikt") is True), (j.get("categorie") if j.get("categorie") in CATEGORIEEN else None), _schoon(j.get("oordeel"), 400)


def kies_antwoord(objecten):
    """Het antwoord uit de JSON-objecten van Claude: het laatste volledige (na het zoeken schrijft hij soms opnieuw). Pure functie."""
    met = [o for o in objecten if "geschikt" in o] or objecten
    return next((o for o in reversed(met) if o.get("tekst") and o.get("titel")), met[-1])



def maak_voorstel(verhaal):
    """Schrijven, beeld maken en (als het geschikt is) via Telegram melden. Geeft de rij terug."""
    v = q1("""INSERT INTO nieuws_voorstellen (item_ids, keepers, sport, score) VALUES (%s, %s, %s, %s) RETURNING *""",
           (verhaal["ids"], list(verhaal.get("keepers") or []), verhaal.get("sport"), verhaal.get("score")))
    try:
        leer = leerstof()
        if inst()["kn_voorselectie"] == "1":
            geschikt, cat, oordeel, u = voorselectie(verhaal, leer)
            v = q1("""UPDATE nieuws_voorstellen SET tokens_in_snel = %s, tokens_out_snel = %s, categorie = %s, oordeel = %s
                      WHERE id = %s RETURNING *""", (u.get("input_tokens"), u.get("output_tokens"), cat, oordeel, v["id"]))
            if not geschikt:
                return q1("""UPDATE nieuws_voorstellen SET status = 'overgeslagen', titel = %s, bijgewerkt = now() WHERE id = %s RETURNING *""",
                          ((verhaal.get("titel") or "")[:160], v["id"]))
        tekst, usage, bronnen = claude_api.gesprek(SYSTEEM, [{"role": "user", "content": _vraag(verhaal, leer)}],
                                                   max_tokens=6000, zoeken=_getal(inst(), "kn_zoeken"), timeout=300)
        a = schoon_antwoord(kies_antwoord(claude_api.json_uit(tekst, alle=True)))
        gezien = {b["url"] for b in a["bronnen"]}
        a["bronnen"] += [b for b in bronnen if b["url"] not in gezien][:4]
        if not a["bronnen"]:
            a["bronnen"] = [{"titel": x.get("bron") or x.get("titel") or "Bron", "url": x["url"]} for x in verhaal.get("leden") or [] if x.get("url")][:3]
        status = "klaar" if a["geschikt"] else "overgeslagen"
        v = q1("""UPDATE nieuws_voorstellen SET status = %s, categorie = %s, titel = %s, intro = %s, tekst = %s, kernzin = %s,
                  beeld_scene = %s, bronnen = %s, oordeel = %s, keepers = %s, sport = coalesce(nullif(%s, ''), sport), gevoelig = %s,
                  tokens_in = %s, tokens_out = %s, zoekopdrachten = %s, bijgewerkt = now() WHERE id = %s RETURNING *""",
               (status, a["categorie"], a["titel"], a["intro"], a["tekst"], a["kernzin"], a["beeld_scene"], json.dumps(a["bronnen"]),
                a["oordeel"], a["keepers"] or list(verhaal.get("keepers") or []), a["sport"], a["gevoelig"], usage.get("input_tokens"),
                usage.get("output_tokens"), usage.get("zoekopdrachten"), v["id"]))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return q1("UPDATE nieuws_voorstellen SET status = 'fout', fout = %s, bijgewerkt = now() WHERE id = %s RETURNING *",
                  (str(e)[:400], v["id"]))
    if v["status"] != "klaar":
        return v
    v = nieuw_beeld(v)
    return melden(v)


def nieuw_beeld(v, zonder_ai=False):
    """Een beeld maken: AI-sfeerbeeld met huisstijl, of het huisstijlbeeld (als AI uit staat, gevraagd is of het budget op is)."""
    i = inst()
    ai = not zonder_ai and i["kn_ai_beeld"] == "1" and bool(ai_beeld.aanbieder())
    melding = ""
    if ai and budget_op(i):
        ai, melding = False, "Het maandbudget is op, dus het huisstijlbeeld."
    png, bron, fout = ai_beeld.maak(v["titel"] or "", v["beeld_scene"] or "", v["sport"] or "", v["kernzin"] or "", ai=ai)
    beelden = dict(v.get("beelden") or {})
    if bron.startswith("ai-"):
        beelden[bron[3:]] = int(beelden.get(bron[3:], 0)) + 1
    return q1("""UPDATE nieuws_voorstellen SET beeld = %s, beeld_bron = %s, beeld_fout = %s, beelden = %s, bijgewerkt = now()
                 WHERE id = %s RETURNING *""", (png, bron, (fout or melding) or None, json.dumps(beelden), v["id"]))


# ---------------------------------------------------------------- Telegram

# De bot: de ZEROHERO Doellijn-bot (ZEROHERO_TELEGRAM_BOT_TOKEN en _CHAT_ID in zerohero.env), anders die van Buurmanitor.
BOT = telegram.registreer(telegram.Bot(os.environ.get("ZEROHERO_TELEGRAM_BOT_TOKEN") or kern_config.TELEGRAM_BOT_TOKEN,
                                       os.environ.get("ZEROHERO_TELEGRAM_CHAT_ID") or kern_config.TELEGRAM_CHAT_ID, "zerohero"))


def KNOPPEN(vid):  # noqa: N802
    """Tikknoppen; kan Buurmanitor de tikken niet ophalen (webhook van n8n op dezelfde bot) en stuurt n8n ze niet door
    (kn_via_n8n), dan linkknoppen naar de Doellijn."""
    if BOT.status.get("conflict") and inst()["kn_via_n8n"] != "1":
        return [[("✅ Plaatsen", f"{link(vid)}?snel=plaatsen"), ("👀 Ik kijk zelf", f"{link(vid)}?snel=concept")],
                [("❌ Niet plaatsen", f"{link(vid)}?snel=afwijzen")]]
    return [[("✅ Plaatsen", f"kn:{vid}:p"), ("👀 Ik kijk zelf", f"kn:{vid}:l")], [("❌ Niet plaatsen", f"kn:{vid}:n")]]


def link(vid):
    return f"{omgeving.BASIS}/keepersnieuws/{vid}"


def bericht_tekst(v, slot=""):
    """De tekst van het Telegram-bericht (HTML). Pure functie."""
    e = telegram.e
    bronnen = " · ".join(f'<a href="{html.escape(b["url"])}">{e(b["titel"] or "bron")}</a>' for b in (v.get("bronnen") or [])[:4])
    delen = ["⚠️ <b>Gevoelig onderwerp</b>: lees extra goed na voor je plaatst." if v.get("gevoelig") else "",
             f"<b>{e(v['titel'])}</b>", f"<i>{e(v['intro'])}</i>" if v.get("intro") else "", e(v.get("tekst") or ""),
             f"Bronnen: {bronnen}" if bronnen else "",
             f"<i>{e(CATEGORIEEN.get(v.get('categorie'), ''))} · {e(v.get('sport') or '')} · score {v.get('score') or '?'}. {e(v.get('oordeel') or '')}</i>",
             f'<a href="{link(v["id"])}">Aanpassen in de Doellijn</a>', slot]
    tekst = "\n\n".join(d for d in delen if d)
    if len(tekst) > 4000:  # te lang voor Telegram: de tekst inkorten, de rest staat in de Doellijn
        kort = e((v.get("tekst") or "")[:2500].rsplit(" ", 1)[0]) + " (...)"
        tekst = "\n\n".join(d for d in delen[:3] + [kort] + delen[4:] if d)
    return tekst


def melden(v):
    if not BOT.actief():
        return q1("UPDATE nieuws_voorstellen SET status = 'verstuurd', fout = %s WHERE id = %s RETURNING *",
                  ("Telegram is niet ingesteld; het voorstel staat alleen in de Doellijn.", v["id"]))
    try:
        foto = BOT.stuur_foto(bytes(v["beeld"]), f"🧤 <b>Keepersnieuws</b> · {telegram.e(CATEGORIEEN.get(v['categorie'], ''))}\n"
                                   f"{telegram.e(v['titel'])}", bestandsnaam=f"keepersnieuws-{v['id']}.png") if v.get("beeld") else {}
        tekst = BOT.stuur(bericht_tekst(v), KNOPPEN(v["id"]))
        return q1("""UPDATE nieuws_voorstellen SET status = 'verstuurd', telegram_foto = %s, telegram_tekst = %s, fout = NULL
                     WHERE id = %s RETURNING *""", (foto.get("message_id"), tekst.get("message_id"), v["id"]))
    except telegram.TelegramFout as e:
        return q1("UPDATE nieuws_voorstellen SET status = 'verstuurd', fout = %s WHERE id = %s RETURNING *",
                  (f"Telegram lukte niet: {e}", v["id"]))


def op_knop(data, bericht):
    """Een tik in Telegram: kn:<id>:p|l|n, of kn:<id>:r:<reden> na Niet plaatsen."""
    delen = data.split(":")
    vid, actie = int(delen[1]), delen[2]
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if not v:
        return {"toast": "Dit voorstel bestaat niet meer.", "knoppen": []}
    if actie == "n":
        if v["besluit"] in ("geplaatst", "later"):
            return {"toast": "Dit voorstel is al afgehandeld.", "knoppen": []}
        besluit(vid, "afgewezen", via="telegram")
        rijen = [[(t, f"kn:{vid}:r:{k}")] for k, t in REDENEN.items()]
        return {"toast": "Niet geplaatst. Waarom niet? Tik een reden, of antwoord met je eigen woorden.",
                "tekst": bericht_tekst(v, "❌ <b>Niet geplaatst.</b> Waarom niet? Tik een reden, of antwoord op dit bericht met je eigen woorden."),
                "knoppen": rijen}
    if actie == "r":
        reden = delen[3] if len(delen) > 3 and delen[3] in REDENEN else None
        q("UPDATE nieuws_voorstellen SET reden = %s WHERE id = %s RETURNING 1", (reden, vid))
        return {"toast": "Genoteerd, daar leer ik van.", "tekst": bericht_tekst(v, f"❌ <b>Niet geplaatst</b>: {telegram.e(REDENEN.get(reden, ''))}"),
                "knoppen": []}
    if v["besluit"] in ("geplaatst",):
        return {"toast": "Staat al op de site.", "knoppen": []}
    uitslag = besluit(vid, "geplaatst" if actie == "p" else "later", via="telegram")
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if v["fout"] and not v["wp_post_id"]:
        return {"toast": uitslag[:190], "tekst": bericht_tekst(v, f"⚠️ {telegram.e(uitslag)}"), "knoppen": KNOPPEN(vid)}
    slot = (f'✅ <b>Geplaatst</b>: <a href="{html.escape(v["wp_link"] or "")}">bekijk op zerohero.one</a>' if v["besluit"] == "geplaatst" else
            f'👀 <b>Staat als concept in WordPress.</b> <a href="{link(vid)}">Bekijken en plaatsen in de Doellijn</a>')
    return {"toast": uitslag[:190], "tekst": bericht_tekst(v, slot), "knoppen": []}


def op_antwoord(bericht_id, tekst):
    """Een eigen reden als antwoord op het Telegram-bericht van een voorstel."""
    v = q1("SELECT * FROM nieuws_voorstellen WHERE telegram_tekst = %s OR telegram_foto = %s", (bericht_id, bericht_id))
    if not v:
        return None
    q("UPDATE nieuws_voorstellen SET toelichting = %s WHERE id = %s RETURNING 1", (tekst.strip()[:500], v["id"]))
    return "Dank je, je toelichting is bewaard. Daar leer ik van."


# ---------------------------------------------------------------- plaatsen

def categorie_id():
    """De WordPress-categorie Keepersnieuws (gemaakt als hij er nog niet is)."""
    i = inst()
    if i["kn_categorie_id"].isdigit():
        return int(i["kn_categorie_id"])
    gevonden = wp._wp("GET", "/wp/v2/categories?search=Keepersnieuws&_fields=id,name,slug")  # pylint: disable=protected-access
    cid = next((c["id"] for c in gevonden or [] if c.get("slug") == "keepersnieuws" or c.get("name", "").lower() == "keepersnieuws"), None)
    if not cid:
        cid = wp._wp("POST", "/wp/v2/categories", data={"name": "Keepersnieuws", "slug": "keepersnieuws",  # pylint: disable=protected-access
                                                       "description": "Nieuws over keepers en goalies uit alle sporten."})["id"]
    zet("kn_categorie_id", cid)
    return cid


def sport_categorie(sport):
    """De bestaande sportcategorie op de site (dezelfde als bij de blogs). Pure functie."""
    from .artikel import CAT  # pylint: disable=import-outside-toplevel
    s = re.sub(r"[^a-z]", "", str(sport or "").lower())
    s = {"veldhockey": "hockey", "zaalhockey": "hockey", "football": "voetbal", "soccer": "voetbal", "icehockey": "ijshockey",
         "handball": "handbal", "waterpolo": "waterpolo"}.get(s, s)
    if not s or s in ("alle", "allesporten", "meerdere"):
        return CAT["alle"]
    return CAT.get(s, CAT["anders"])


def inhoud_html(v):
    """De inhoud voor WordPress (blokken). Pure functie."""
    e = lambda t: html.escape(str(t or ""), quote=False)  # noqa: E731
    delen = []
    if v.get("intro"):
        delen.append(f'<!-- wp:paragraph {{"className":"zh-intro"}} -->\n<p class="zh-intro"><strong>{e(v["intro"])}</strong></p>\n<!-- /wp:paragraph -->')
    for a in re.split(r"\n\s*\n", v.get("tekst") or ""):
        if a.strip():
            delen.append(f"<!-- wp:paragraph -->\n<p>{e(a.strip())}</p>\n<!-- /wp:paragraph -->")
    bronnen = ", ".join(f'<a href="{html.escape(b["url"])}" target="_blank" rel="noopener">{e(b["titel"] or "bron")}</a>'
                        for b in (v.get("bronnen") or [])[:4])
    if bronnen:
        delen.append(f'<!-- wp:paragraph {{"className":"zh-bronnen"}} -->\n<p class="zh-bronnen"><em>Bronnen: {bronnen}</em></p>\n<!-- /wp:paragraph -->')
    return "\n\n".join(delen)


def _slug(t):
    s = re.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")
    return s[:70] or "keepersnieuws"


def plaats(v, live=True):
    """In WordPress zetten (live of als concept), met het beeld als uitgelicht beeld. Geeft de bijgewerkte rij.
    Eerst als concept (zoals de blogs), daarna pas publiceren: elke stap wordt meteen bewaard, zodat opnieuw proberen
    geen dubbel beeld of dubbel bericht maakt."""
    if not wp.wp_actief():
        raise RuntimeError("De Doellijn heeft geen WordPress-gegevens (deploy/zerohero-sleutel.sh wordpress).")
    if not v["wp_post_id"]:
        media_id = v.get("wp_media_id")
        if v.get("beeld") and not media_id:
            media_id, _ = wp.upload(bytes(v["beeld"]), f"{_slug(v['titel'])}.png", alt=v["titel"] or "",
                                    bijschrift="Beeld: ZEROHERO" + (" (AI-sfeerbeeld)" if (v["beeld_bron"] or "").startswith("ai") else ""))
            v = q1("UPDATE nieuws_voorstellen SET wp_media_id = %s WHERE id = %s RETURNING *", (media_id, v["id"]))
        p = wp._wp("POST", "/wp/v2/posts", data={  # pylint: disable=protected-access
            "status": "draft", "title": v["titel"], "content": inhoud_html(v), "excerpt": v["intro"] or "", "slug": _slug(v["titel"]),
            "categories": [categorie_id(), sport_categorie(v.get("sport"))], **({"featured_media": media_id} if media_id else {})})
        v = q1("UPDATE nieuws_voorstellen SET wp_post_id = %s, wp_link = %s WHERE id = %s RETURNING *", (p["id"], p.get("link"), v["id"]))
        hoofdcategorie(p["id"])
        if media_id:
            try:
                wp.beeld_in_artikel(p["id"])
            except Exception:  # noqa: BLE001  (het uitgelichte beeld staat er in elk geval)
                pass
    pid = int(v["wp_post_id"])
    try:
        p = wp._wp("POST", f"/wp/v2/posts/{pid}", data={  # pylint: disable=protected-access
            "status": "publish" if live else "draft", "title": v["titel"], "content": inhoud_html(v), "excerpt": v["intro"] or ""})
    except wp.WpFout as e:
        # Een plugin kan bij het publiceren een fout geven terwijl het bericht wel live staat: dan even nakijken.
        if not live or e.code < 500:
            raise
        p = wp._wp("GET", f"/wp/v2/posts/{pid}?context=edit&_fields=id,status,link")  # pylint: disable=protected-access
        if (p or {}).get("status") != "publish":
            raise RuntimeError(f"{e}. Het bericht staat als concept in WordPress (bericht {pid}); publiceren lukte niet.") from None
    return q1("""UPDATE nieuws_voorstellen SET wp_link = %s, fout = NULL, bijgewerkt = now() WHERE id = %s RETURNING *""",
              ((p or {}).get("link") or v["wp_link"], v["id"]))


def hoofdcategorie(post_id):
    """Keepersnieuws als hoofdcategorie in Rank Math: dan staat er in de breadcrumb Home > Keepersnieuws (en niet de sport).
    Niet erg als het niet lukt."""
    try:
        wp._wp("POST", "/rankmath/v1/updateMeta", data={"objectType": "post", "objectID": int(post_id),  # pylint: disable=protected-access
                                                       "meta": {"rank_math_primary_category": categorie_id()}})
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


@planner.taak("keepersnieuws_hoofdcategorie", elke=dt.timedelta(hours=6))
def taak_hoofdcategorie(_c):
    """Eenmalig voor berichten die er al stonden: de hoofdcategorie alsnog zetten."""
    if inst().get("kn_hoofdcat_klaar") == "1" or not wp.wp_actief():
        return "niets te doen"
    rijen = q("SELECT wp_post_id FROM nieuws_voorstellen WHERE wp_post_id IS NOT NULL")
    goed = sum(1 for r in rijen if hoofdcategorie(r["wp_post_id"]))
    if goed == len(rijen):
        zet("kn_hoofdcat_klaar", "1")
    return f"{goed} van {len(rijen)} berichten"


def besluit(vid, wat, via="scherm", reden=None, toelichting=None):
    """geplaatst (live), later (concept in WordPress) of afgewezen. Geeft een korte uitslag."""
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if not v:
        raise HTTPException(404)
    if wat == "afgewezen":
        if v["wp_post_id"] and v["besluit"] == "later":
            try:
                wp.naar_prullenbak_als_concept(v["wp_post_id"])
            except Exception:  # noqa: BLE001
                pass
        q("""UPDATE nieuws_voorstellen SET status = 'afgewezen', besluit = 'afgewezen', reden = coalesce(%s, reden),
             toelichting = coalesce(%s, toelichting), besloten_op = now(), besloten_via = %s, bijgewerkt = now() WHERE id = %s RETURNING 1""",
          (reden, toelichting, via, vid))
        return "Niet geplaatst."
    try:
        v = plaats(v, live=(wat == "geplaatst"))
    except Exception as e:  # noqa: BLE001
        q("UPDATE nieuws_voorstellen SET fout = %s WHERE id = %s RETURNING 1", (f"Plaatsen lukte niet: {str(e)[:250]}", vid))
        return f"Plaatsen lukte niet: {str(e)[:150]}"
    q("""UPDATE nieuws_voorstellen SET status = %s, besluit = %s, besloten_op = now(), besloten_via = %s WHERE id = %s RETURNING 1""",
      (wat, wat, via, vid))
    return "Geplaatst op zerohero.one." if wat == "geplaatst" else "Staat als concept in WordPress."


@router.post("/intern/telegram")
async def intern_telegram(request: Request):
    """Tikken die n8n doorstuurt (als n8n de webhook van de Doellijn-bot heeft). Het werk gaat op de achtergrond, zodat
    n8n meteen verder kan; Buurmanitor beantwoordt de tik zelf bij Telegram."""
    if not CONCEPT_SECRET or request.headers.get("X-Doellijn-Secret") != CONCEPT_SECRET:
        raise HTTPException(403)
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(400) from None
    if isinstance(update, list):  # n8n stuurt soms een lijst items
        update = update[0] if update else {}
    update = update.get("body", update) if isinstance(update, dict) else {}
    if not (update.get("callback_query") or update.get("message")):
        return {"ok": True, "verwerkt": False}
    zet("kn_via_n8n_laatst", dt.datetime.now(dt.timezone.utc).isoformat())
    threading.Thread(target=BOT.verwerk, args=(update,), name="telegram-via-n8n", daemon=True).start()
    return {"ok": True, "verwerkt": True}


telegram.HANDLERS["kn"] = op_knop
telegram.ANTWOORDEN.append(op_antwoord)


# ---------------------------------------------------------------- de ronde

def ronde(forceer=False):
    """Eén ronde: nieuwe voorstellen maken (binnen de grenzen). Geeft een korte uitslag."""
    i = inst()
    if i["kn_aan"] != "1" and not forceer:
        return "staat uit"
    if not claude_api.actief():
        return "geen Claude-sleutel in de Doellijn"
    if not _slot.acquire(blocking=False):
        return "al bezig"
    try:
        ruimte = min(_getal(i, "kn_per_ronde"), _getal(i, "kn_per_dag") - vandaag_gemaakt())
        if ruimte <= 0:
            return "dagmaximum bereikt"
        if budget_op(i):
            return f"maandbudget op (€ {kosten_maand()[0]:.2f} van € {i['kn_budget']})"
        gemaakt = []
        for verhaal in kandidaten(i)[:ruimte]:
            gemaakt.append(maak_voorstel(verhaal))
        if not gemaakt:
            return "geen nieuw keepersnieuws boven de drempel"
        tel = {s: sum(1 for v in gemaakt if v["status"] == s) for s in ("verstuurd", "overgeslagen", "fout")}
        return f"{tel['verstuurd']} voorgesteld, {tel['overgeslagen']} geen keepersnieuws" + (f", {tel['fout']} mislukt" if tel["fout"] else "")
    finally:
        _slot.release()


@planner.taak("keepersnieuws", elke=dt.timedelta(minutes=15))
def taak_keepersnieuws(_c):
    return ronde()


# ---------------------------------------------------------------- schermen

def _terug(pad, melding="", fout=""):
    from urllib.parse import urlencode  # pylint: disable=import-outside-toplevel
    params = {k: v for k, v in (("melding", melding), ("fout", fout)) if v}
    return RedirectResponse(pad + ("?" + urlencode(params) if params else ""), status_code=303)


def _rij(v):
    v = dict(v)
    v["kosten"] = kosten_voorstel(v)
    v["bronnen"] = v.get("bronnen") or []
    v["heeft_beeld"] = v.pop("beeld", None) is not None
    return v


@router.get("/keepersnieuws", response_class=HTMLResponse)
def overzicht(request: Request, melding: str = "", fout: str = ""):
    rijen = [_rij(v) for v in q("""SELECT * FROM nieuws_voorstellen ORDER BY CASE WHEN status IN ('verstuurd', 'klaar', 'later') THEN 0 ELSE 1 END,
                                    aangemaakt DESC LIMIT 80""")]
    tel = q("""SELECT categorie, count(*) FILTER (WHERE besluit = 'geplaatst') AS ja, count(*) FILTER (WHERE besluit = 'later') AS later,
                      count(*) FILTER (WHERE besluit = 'afgewezen') AS nee FROM nieuws_voorstellen WHERE besluit IS NOT NULL GROUP BY categorie""")
    redenen = q("""SELECT reden, count(*) AS n FROM nieuws_voorstellen WHERE besluit = 'afgewezen' AND reden IS NOT NULL
                   GROUP BY reden ORDER BY n DESC""")
    return templates.TemplateResponse("keepersnieuws.html", ctx(
        request, pagina="keepersnieuws", rijen=rijen, i=inst(), tel=tel, redenen=redenen, categorieen=CATEGORIEEN, redenen_namen=REDENEN,
        statussen=STATUSSEN, aanbieder=ai_beeld.aanbieder(), telegram_actief=BOT.actief(), telegram_status=BOT.status,
        eigen_bot=bool(os.environ.get("ZEROHERO_TELEGRAM_BOT_TOKEN")),
        wp_actief=wp.wp_actief(), claude_actief=claude_api.actief(), vandaag=vandaag_gemaakt(), maand=kosten_maand(),
        budget_op=budget_op(), melding=melding, fout=fout))


@router.post("/keepersnieuws/instellingen")
def instellingen_opslaan(request: Request, kn_aan: str = Form(""), kn_drempel: int = Form(60), kn_per_dag: int = Form(5),
                         kn_per_ronde: int = Form(2), kn_richtlijnen: str = Form(""), kn_zoeken: int = Form(2), kn_budget: str = Form("15"),
                         kn_ai_beeld: str = Form(""), kn_voorselectie: str = Form(""), kn_via_n8n: str = Form("")):
    controleer_herkomst(request)
    zet("kn_via_n8n", "1" if kn_via_n8n else "0")
    zet("kn_aan", "1" if kn_aan else "0")
    zet("kn_ai_beeld", "1" if kn_ai_beeld else "0")
    zet("kn_voorselectie", "1" if kn_voorselectie else "0")
    try:
        zet("kn_budget", f"{max(0.0, min(500.0, float(kn_budget.replace(',', '.')))):g}")
    except ValueError:
        pass
    zet("kn_drempel", max(0, min(100, kn_drempel)))
    zet("kn_per_dag", max(0, min(20, kn_per_dag)))
    zet("kn_per_ronde", max(1, min(5, kn_per_ronde)))
    zet("kn_zoeken", max(0, min(5, kn_zoeken)))
    zet("kn_richtlijnen", kn_richtlijnen.strip()[:3000])
    return _terug("/keepersnieuws", melding="Opgeslagen.")


@router.post("/keepersnieuws/zoek")
def nu_zoeken(request: Request):
    controleer_herkomst(request)
    threading.Thread(target=ronde, kwargs={"forceer": True}, name="keepersnieuws", daemon=True).start()
    return _terug("/keepersnieuws", melding="Buurmanitor kijkt nu naar het nieuws. Nieuwe voorstellen komen over een paar minuten "
                                            "in Telegram en hieronder.")


@router.post("/keepersnieuws/opnieuw")
def alles_opnieuw(request: Request):
    """Alle mislukte voorstellen van de laatste 36 uur opnieuw: ze tellen dan weer mee bij de volgende ronde."""
    controleer_herkomst(request)
    n = len(q("""DELETE FROM nieuws_voorstellen WHERE status = 'fout' AND aangemaakt > now() - interval '36 hours' RETURNING 1"""))
    if n:
        threading.Thread(target=ronde, kwargs={"forceer": True}, name="keepersnieuws", daemon=True).start()
    return _terug("/keepersnieuws", melding=f"{n} mislukte voorstel(len) worden opnieuw geprobeerd (binnen de grenzen per ronde en per dag)."
                  if n else "Er zijn geen recente mislukte voorstellen.")


@router.get("/keepersnieuws/{vid}", response_class=HTMLResponse)
def voorstel(request: Request, vid: int, melding: str = "", fout: str = "", snel: str = ""):
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if not v:
        return RedirectResponse("/keepersnieuws")
    items = q("SELECT id, titel, url, bron, score FROM items WHERE id = ANY(%s) ORDER BY score DESC NULLS LAST", (v["item_ids"],))
    return templates.TemplateResponse("keepersnieuws_voorstel.html", ctx(
        request, pagina="keepersnieuws", v=_rij(v), items=items, categorieen=CATEGORIEEN, redenen=REDENEN, statussen=STATUSSEN,
        aanbieder=ai_beeld.aanbieder(), melding=melding, fout=fout or (v["fout"] or ""),
        snel=snel if snel in ("plaatsen", "concept", "afwijzen") and not v["besluit"] else ""))


@router.get("/keepersnieuws/{vid}/beeld.png")
def beeld_png(vid: int):
    v = q1("SELECT beeld FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if not v or v["beeld"] is None:
        raise HTTPException(404)
    return Response(bytes(v["beeld"]), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/keepersnieuws/{vid}")
def voorstel_opslaan(request: Request, vid: int, titel: str = Form(...), intro: str = Form(""), tekst: str = Form(""),
                     categorie: str = Form("groot"), actie: str = Form("opslaan"), reden: str = Form(""), toelichting: str = Form("")):
    """Aanpassen en daarna eventueel plaatsen, als concept zetten of afwijzen."""
    controleer_herkomst(request)
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if not v:
        raise HTTPException(404)
    if v["status"] in ("bezig",):
        return _terug(f"/keepersnieuws/{vid}", fout="Dit voorstel wordt nog geschreven.")
    gewijzigd = (titel.strip(), intro.strip(), tekst.strip()) != (v["titel"] or "", v["intro"] or "", v["tekst"] or "")
    v = q1("""UPDATE nieuws_voorstellen SET titel = %s, intro = %s, tekst = %s, categorie = %s, bijgewerkt = now() WHERE id = %s RETURNING *""",
           (_schoon(titel, 160) or v["titel"], _schoon(intro, 400), "\n\n".join(_schoon(a, 1500) for a in re.split(r"\n\s*\n", tekst) if a.strip()),
            categorie if categorie in CATEGORIEEN else v["categorie"], vid))
    if gewijzigd and v["wp_post_id"] and v["besluit"] == "geplaatst" and actie == "opslaan":
        try:
            plaats(v, live=True)
        except Exception as e:  # noqa: BLE001
            return _terug(f"/keepersnieuws/{vid}", fout=f"Opgeslagen, maar WordPress bijwerken lukte niet: {str(e)[:150]}")
    if actie == "plaatsen":
        uitslag = besluit(vid, "geplaatst")
    elif actie == "concept":
        uitslag = besluit(vid, "later")
    elif actie == "afwijzen":
        uitslag = besluit(vid, "afgewezen", reden=reden if reden in REDENEN else None, toelichting=toelichting.strip()[:500] or None)
    else:
        uitslag = "Opgeslagen."
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if v["telegram_tekst"] and actie in ("plaatsen", "concept", "afwijzen") and BOT.actief():
        try:  # de knoppen in Telegram weghalen: het is al beslist
            BOT.bewerk(v["telegram_tekst"], bericht_tekst(v, f"<i>Afgehandeld in de Doellijn: {telegram.e(uitslag)}</i>"), [])
        except telegram.TelegramFout:
            pass
    if uitslag.startswith("Plaatsen lukte niet"):
        return _terug(f"/keepersnieuws/{vid}", fout=uitslag)
    return _terug(f"/keepersnieuws/{vid}", melding=uitslag)


@router.post("/keepersnieuws/{vid}/opnieuw")
def opnieuw(request: Request, vid: int):
    """Een mislukt voorstel opnieuw laten schrijven (met dezelfde berichten)."""
    controleer_herkomst(request)
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if not v or v["status"] != "fout":
        return _terug(f"/keepersnieuws/{vid}" if v else "/keepersnieuws", fout="Alleen een mislukt voorstel kan opnieuw.")
    rijen = q("SELECT * FROM items WHERE id = ANY(%s) ORDER BY score DESC NULLS LAST", (v["item_ids"],))
    if not rijen:
        return _terug(f"/keepersnieuws/{vid}", fout="De berichten van dit voorstel zijn er niet meer.")
    verhaal = clusteren([dict(r) for r in rijen])[0]
    verhaal["ids"] = [r["id"] for r in rijen]
    verhaal["leden"] = [dict(r) for r in rijen]
    q("DELETE FROM nieuws_voorstellen WHERE id = %s RETURNING 1", (vid,))
    threading.Thread(target=maak_voorstel, args=(verhaal,), name="keepersnieuws-opnieuw", daemon=True).start()
    return _terug("/keepersnieuws", melding="Het voorstel wordt opnieuw geschreven. Het komt over een paar minuten in Telegram en hieronder.")


@router.post("/keepersnieuws/{vid}/beeld")
def beeld_opnieuw(request: Request, vid: int, soort: str = Form("ai"), scene: str = Form(None)):
    controleer_herkomst(request)
    v = q1("SELECT * FROM nieuws_voorstellen WHERE id = %s", (vid,))
    if not v:
        raise HTTPException(404)
    if scene is not None and soort == "ai" and scene.strip() != (v["beeld_scene"] or ""):
        v = q1("UPDATE nieuws_voorstellen SET beeld_scene = %s WHERE id = %s RETURNING *", (scene.strip()[:900], vid))
    v = nieuw_beeld(v, zonder_ai=(soort == "huisstijl"))
    if v["wp_post_id"] and v["beeld"] is not None:
        try:
            media_id, _ = wp.upload(bytes(v["beeld"]), f"{_slug(v['titel'])}.png", alt=v["titel"] or "")
            wp.zet_uitgelicht(v["wp_post_id"], media_id)
            wp.beeld_in_artikel(v["wp_post_id"])
        except Exception as e:  # noqa: BLE001
            return _terug(f"/keepersnieuws/{vid}", fout=f"Nieuw beeld gemaakt, maar WordPress bijwerken lukte niet: {str(e)[:150]}")
    return _terug(f"/keepersnieuws/{vid}", fout=v["beeld_fout"] or "", melding="" if v["beeld_fout"] else "Nieuw beeld gemaakt.")
