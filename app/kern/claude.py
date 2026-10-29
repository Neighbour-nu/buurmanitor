"""Claude-client van de portal: eigen sleutel (werkruimte Assistent), verbruik per persona, maandplafond.

Kosten zijn een schatting op basis van tokens; de echte rekening staat in de Claude Console.

Nieuwe modellen (zoals Claude Sonnet 5.5) denken eerst na voordat ze antwoorden, en dat denkwerk telt mee in
max_tokens. Daarom vraagt de portal voor gesprekken een lage inspanning ('low', genoeg voor agenda en overzicht) en
geeft hij naast de ruimte voor het antwoord vaste ruimte voor het nadenken. Komt er toch geen of een afgebroken
antwoord, dan probeert hij het één keer opnieuw met meer ruimte.
"""
import json
import urllib.error
import urllib.request

from . import config

API = "https://api.anthropic.com/v1/messages"
# Schatting in dollar per miljoen tokens (invoer, uitvoer), omgerekend met KOERS. Bijstellen als de prijzen veranderen.
PRIJZEN = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}
KOERS = 0.92


class ClaudeFout(Exception):
    pass


def kosten(model, invoer, uitvoer):
    soort = next((k for k in PRIJZEN if k in model), "sonnet")
    p_in, p_uit = PRIJZEN[soort]
    return (invoer * p_in + uitvoer * p_uit) / 1_000_000 * KOERS


def verbruik_maand(c):
    r = c.execute("SELECT coalesce(sum(kosten_eur), 0) AS eur, count(*) AS aantal FROM claude_gebruik "
                  "WHERE tijd >= date_trunc('month', now())").fetchone()
    return float(r["eur"]), r["aantal"]


DENKRUIMTE = 8000  # extra tokens voor het nadenken, bovenop de ruimte voor het antwoord
ZONDER_INSPANNING = ("haiku-4-5", "sonnet-4-5")  # deze modellen kennen output_config.effort niet


def berichten_schoon(berichten):
    """Lege berichten (bijvoorbeeld een mislukt antwoord van vroeger) weglaten: Claude weigert lege tekst."""
    return [b for b in berichten if str(b.get("content") or "").strip()]


def gesprek(c, persona, systeem, berichten, model=None, max_tokens=2000, timeout=120, inspanning="low"):
    """Stuur een gesprek naar Claude en geef de tekst van het antwoord terug (nooit leeg)."""
    if not config.ANTHROPIC_API_KEY:
        raise ClaudeFout("Er is nog geen Claude-sleutel ingesteld. Zet die met deploy/sleutel.sh.")
    besteed, _ = verbruik_maand(c)
    if besteed >= config.MAANDPLAFOND_EUR:
        raise ClaudeFout(f"Het maandplafond van € {config.MAANDPLAFOND_EUR:.2f} is bereikt.")
    model = model or config.CLAUDE_MODEL
    berichten = berichten_schoon(berichten)
    if not berichten:
        raise ClaudeFout("Er is geen vraag om te beantwoorden.")
    met_inspanning = bool(inspanning) and not any(x in model for x in ZONDER_INSPANNING)
    ruimte = max_tokens + DENKRUIMTE
    for poging in range(2):
        antwoord = _verstuur(c, persona, systeem, berichten, model, ruimte, timeout, inspanning if met_inspanning else None)
        tekst = "".join(b.get("text", "") for b in antwoord.get("content", []) if b.get("type") == "text").strip()
        stop = antwoord.get("stop_reason")
        if stop == "refusal":
            raise ClaudeFout("Claude wilde op deze vraag niet antwoorden.")
        if stop != "max_tokens":
            if not tekst:
                raise ClaudeFout("Claude gaf een leeg antwoord. Probeer het nog eens, of stel de vraag anders.")
            return tekst
        if poging == 0:
            ruimte *= 2  # te weinig ruimte: één keer opnieuw met het dubbele
    if tekst:
        return tekst + "\n\n(Mijn antwoord is afgebroken omdat het te lang werd. Vraag gerust om de rest.)"
    raise ClaudeFout("Het antwoord paste niet in de beschikbare ruimte. Stel de vraag wat korter of specifieker.")


def _verstuur(c, persona, systeem, berichten, model, ruimte, timeout, inspanning):
    body = {"model": model, "max_tokens": ruimte,
            "system": [{"type": "text", "text": systeem, "cache_control": {"type": "ephemeral"}}],
            "messages": berichten}
    if inspanning:
        body["output_config"] = {"effort": inspanning}
    verzoek = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        antwoord = json.loads(urllib.request.urlopen(verzoek, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        fout = e.read()[:300].decode(errors="replace")
        if inspanning and e.code == 400 and "output_config" in fout:  # model kent inspanning niet: zonder opnieuw
            return _verstuur(c, persona, systeem, berichten, model, ruimte, timeout, None)
        raise ClaudeFout(f"Claude gaf fout {e.code}: {fout}") from e
    gebruik = antwoord.get("usage", {})
    invoer = gebruik.get("input_tokens", 0) + gebruik.get("cache_creation_input_tokens", 0)
    invoer_cache = gebruik.get("cache_read_input_tokens", 0)
    uitvoer = gebruik.get("output_tokens", 0)
    c.execute("INSERT INTO claude_gebruik (persona, model, invoer, uitvoer, kosten_eur) VALUES (%s, %s, %s, %s, %s)",
              (persona, model, invoer + invoer_cache, uitvoer, kosten(model, invoer + invoer_cache / 10, uitvoer)))
    return antwoord
