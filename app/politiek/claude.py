"""Claude aanroepen vanuit Promonitor, met een logboek van het verbruik en een dagplafond.

De sleutel staat in ~/promonitor/.env (ANTHROPIC_API_KEY). Het model voor het wegen van nieuws staat
bij Instellingen (claude_model_weging); standaard het kleine, goedkope model, zoals in Doellijn.
"""
import json
import os
import urllib.error
import urllib.request

# Prijs in dollar per miljoen tokens (invoer, uitvoer). Onbekende modellen tellen als Sonnet (voorzichtig).
PRIJZEN = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}
DOLLAR_NAAR_EURO = 0.92


def actief():
    return bool(os.environ.get("POLITIEK_ANTHROPIC_API_KEY", "").strip())


def kosten_eur(model, invoer, uitvoer):
    soort = next((k for k in PRIJZEN if k in (model or "")), "sonnet")
    p_in, p_uit = PRIJZEN[soort]
    return round((invoer * p_in + uitvoer * p_uit) / 1_000_000 * DOLLAR_NAAR_EURO, 5)


def vandaag_uitgegeven(c, onderdelen=None):
    """Wat Claude vandaag kostte, eventueel alleen voor bepaalde onderdelen (zoals ['nieuws wegen'])."""
    if onderdelen:
        r = c.execute("""SELECT coalesce(sum(kosten_eur), 0) AS eur FROM claude_gebruik
                         WHERE tijd::date = current_date AND onderdeel = ANY(%s)""", (list(onderdelen),)).fetchone()
    else:
        r = c.execute("SELECT coalesce(sum(kosten_eur), 0) AS eur FROM claude_gebruik WHERE tijd::date = current_date").fetchone()
    return float(r["eur"])


# Onderdelen die samen onder het dagplafond voor schrijven vallen (los van het wegen van nieuws).
SCHRIJF_ONDERDELEN = ["schrijven", "adviseur", "notulen", "dossier", "stemadvies", "begroting"]


def schrijfplafond(c):
    """Fout als het dagplafond voor schrijven, adviseur en analyses vandaag al is bereikt."""
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = 'claude_dagplafond_schrijven_eur'").fetchone()
    plafond = float(((r and r["waarde"]) or "3").replace(",", "."))
    if vandaag_uitgegeven(c, SCHRIJF_ONDERDELEN) >= plafond:
        raise RuntimeError(f"Het dagplafond voor schrijven en analyses (€ {plafond:.2f}) is bereikt. "
                           "Morgen weer, of verhoog het bij Instellingen.")


# Als een model (nog) niet bestaat voor deze sleutel, proberen we het vorige.
TERUGVAL = {"claude-sonnet-5-5": "claude-sonnet-4-5", "claude-haiku-5-5": "claude-haiku-4-5", "claude-opus-5-5": "claude-opus-4-5"}


def vraag(c, onderdeel, system, user, model, max_tokens=4000, timeout=120, cache=True):
    """Eén vraag aan Claude. Logt het verbruik in claude_gebruik. Geeft de tekst terug."""
    return gesprek(c, onderdeel, system, [{"role": "user", "content": user}], model, max_tokens, timeout, cache)


def gesprek(c, onderdeel, system, berichten, model, max_tokens=4000, timeout=300, cache=True):
    """Een gesprek (lijst van {'role', 'content'}) aan Claude voorleggen. Logt het verbruik. Geeft de tekst terug."""
    try:
        return _gesprek(c, onderdeel, system, berichten, model, max_tokens, timeout, cache)
    except RuntimeError as e:
        if "404" in str(e) and model in TERUGVAL:
            return _gesprek(c, onderdeel, system, berichten, TERUGVAL[model], max_tokens, timeout, cache)
        raise


def _gesprek(c, onderdeel, system, berichten, model, max_tokens, timeout, cache, opnieuw=True):
    """Nieuwe modellen denken eerst na, en dat telt mee in max_tokens. Raakt de ruimte op voordat er (genoeg) tekst
    is, dan volgt één nieuwe poging met vier keer zoveel ruimte (hoogstens 64.000 tokens)."""
    sleutel = os.environ.get("POLITIEK_ANTHROPIC_API_KEY", "").strip()
    if not sleutel:
        raise RuntimeError("Er is geen Claude-sleutel ingesteld (ANTHROPIC_API_KEY in ~/promonitor/.env).")
    sys_blok = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cache else system
    body = {"model": model, "max_tokens": max_tokens, "system": sys_blok, "messages": berichten}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": sleutel, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            antwoord = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Claude gaf fout {e.code}. {detail[:200]}") from None
    u = antwoord.get("usage") or {}
    invoer = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0) // 10
    uitvoer = u.get("output_tokens", 0)
    c.execute("""INSERT INTO claude_gebruik (onderdeel, model, invoer, uitvoer, kosten_eur)
                 VALUES (%s, %s, %s, %s, %s)""", (onderdeel, model, invoer, uitvoer, kosten_eur(model, invoer, uitvoer)))
    tekst = "".join(b.get("text", "") for b in antwoord.get("content") or [] if b.get("type") == "text")
    if antwoord.get("stop_reason") == "max_tokens":
        if opnieuw and max_tokens < 64000:
            return _gesprek(c, onderdeel, system, berichten, model, min(64000, max_tokens * 4), timeout, cache, opnieuw=False)
        tekst += "\n\n[Het antwoord is afgebroken omdat het te lang werd.]"
    return tekst


def json_uit(tekst):
    """Het eerste JSON-object of de eerste JSON-lijst uit een antwoord, ook als er tekst omheen staat."""
    t = tekst.replace("```json", "").replace("```", "")
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise ValueError("Geen JSON in het antwoord van Claude.")
    start = min(starts)
    eind = t.rfind("}" if t[start] == "{" else "]")
    return json.loads(t[start:eind + 1])
