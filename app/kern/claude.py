"""Claude-client van de portal: eigen sleutel (werkruimte Assistent), verbruik per persona, maandplafond.

Kosten zijn een schatting op basis van tokens; de echte rekening staat in de Claude Console.
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


def gesprek(c, persona, systeem, berichten, model=None, max_tokens=2000, timeout=120):
    """Stuur een gesprek naar Claude en geef de tekst van het antwoord terug."""
    if not config.ANTHROPIC_API_KEY:
        raise ClaudeFout("Er is nog geen Claude-sleutel ingesteld. Zet die met deploy/sleutel.sh.")
    besteed, _ = verbruik_maand(c)
    if besteed >= config.MAANDPLAFOND_EUR:
        raise ClaudeFout(f"Het maandplafond van € {config.MAANDPLAFOND_EUR:.2f} is bereikt.")
    model = model or config.CLAUDE_MODEL
    body = {"model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": systeem, "cache_control": {"type": "ephemeral"}}],
            "messages": berichten}
    verzoek = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        antwoord = json.loads(urllib.request.urlopen(verzoek, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        raise ClaudeFout(f"Claude gaf fout {e.code}: {e.read()[:300].decode(errors='replace')}") from e
    gebruik = antwoord.get("usage", {})
    invoer = gebruik.get("input_tokens", 0) + gebruik.get("cache_creation_input_tokens", 0)
    invoer_cache = gebruik.get("cache_read_input_tokens", 0)
    uitvoer = gebruik.get("output_tokens", 0)
    c.execute("INSERT INTO claude_gebruik (persona, model, invoer, uitvoer, kosten_eur) VALUES (%s, %s, %s, %s, %s)",
              (persona, model, invoer + invoer_cache, uitvoer, kosten(model, invoer + invoer_cache / 10, uitvoer)))
    return "".join(b.get("text", "") for b in antwoord.get("content", []) if b.get("type") == "text").strip()
