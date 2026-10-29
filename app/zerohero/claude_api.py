"""Kleine helper om Claude rechtstreeks vanuit de cockpit aan te roepen (sleutel: ANTHROPIC_API_KEY, via deploy/claude.sh)."""
import json
import os
import urllib.error
import urllib.request

SLEUTEL = os.environ.get("ZEROHERO_ANTHROPIC_API_KEY", "").strip()
SCHRIJFMODEL = os.environ.get("ZEROHERO_SCHRIJF_MODEL", "claude-sonnet-5-5")


def actief():
    return bool(SLEUTEL)


def _post(body, timeout):
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": SLEUTEL, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def vraag(system, user, model=None, max_tokens=16000, zoeken=0, timeout=600):
    """Stelt één vraag. Met zoeken > 0 mag Claude het web doorzoeken (zoveel keer). Geeft (tekst, usage) terug.
    Pauzeert de API halverwege (pause_turn bij lange zoektochten), dan vragen we tot drie keer om door te gaan."""
    tekst, usage, _bronnen = gesprek(system, [{"role": "user", "content": user}], model, max_tokens, zoeken, timeout)
    return tekst, usage


def _als_voorzet(inhoud):
    """Het antwoord tot nu toe, geschikt om als laatste assistent-beurt terug te sturen (de API schrijft dan verder).
    Lege tekstblokken mogen niet, en het laatste tekstblok mag niet op witruimte eindigen."""
    uit = [dict(b) for b in inhoud if not (b.get("type") == "text" and not (b.get("text") or "").strip())]
    while uit and uit[-1].get("type") in ("thinking", "redacted_thinking"):
        uit.pop()  # een beurt mag niet eindigen op een denkblok
    if uit and uit[-1].get("type") == "text":
        uit[-1]["text"] = uit[-1]["text"].rstrip()
    return uit


VERDER = ("Je antwoord werd afgebroken omdat het te lang werd. Ga precies verder op het punt waar je stopte, midden in het woord of de zin "
          "als dat zo uitkomt. Herhaal niets en zet er niets voor.")


def heeft_tekst(inhoud):
    return any(b.get("type") == "text" and (b.get("text") or "").strip() for b in inhoud)


def gesprek(system, berichten, model=None, max_tokens=16000, zoeken=0, timeout=600, cache=False, doorgaan=0, inspanning=None):
    """Zoals vraag, maar met een hele gespreksgeschiedenis [{'role': 'user'|'assistant', 'content': ...}].
    Met cache=True wordt de (lange, vaste) systeemtekst gecachet, zodat volgende beurten goedkoper zijn.
    Met inspanning ('low', 'medium', 'high') stuur je hoeveel Claude vooraf nadenkt; dat denkwerk telt mee in max_tokens.
    Met doorgaan > 0 schrijft Claude zoveel keer verder als het antwoord tegen max_tokens aanloopt, in plaats van af te breken.
    Geeft (tekst, usage, bronnen) terug; bronnen zijn de webpagina's die Claude aanhaalde."""
    if not actief():
        raise RuntimeError("Er is geen Claude-sleutel ingesteld. Draai deploy/claude.sh op de server.")
    sys_blok = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cache else system
    basis = list(berichten)
    body = {"model": model or SCHRIJFMODEL, "max_tokens": max_tokens, "system": sys_blok, "messages": basis}
    if zoeken:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": int(zoeken)}]
    if inspanning:
        body["output_config"] = {"effort": inspanning}
    inhoud, usage = [], {"input_tokens": 0, "output_tokens": 0}
    verder = 0
    for _ in range(4 + doorgaan):
        try:
            r = _post(body, timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            if e.code == 400 and "output_config" in body and ("effort" in detail or "output_config" in detail):
                body = {k: v for k, v in body.items() if k != "output_config"}  # model kent de instelling niet: zonder proberen
                r = _post(body, timeout)
            else:
                raise RuntimeError(f"Claude gaf fout {e.code}. {detail[:200]}") from None
        inhoud += r.get("content") or []
        u = r.get("usage") or {}
        usage["input_tokens"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0) // 10
        usage["output_tokens"] += u.get("output_tokens", 0)
        usage["zoekopdrachten"] = usage.get("zoekopdrachten", 0) + ((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        stop = r.get("stop_reason")
        if stop == "max_tokens" and doorgaan and not heeft_tekst(inhoud) and verder < doorgaan:
            # Alles ging op aan nadenken: dan helpt doorschrijven niet. Opnieuw, met minder nadenken.
            verder += 1
            inhoud = []
            body = dict(body, output_config={"effort": "low"}, messages=basis)
            continue
        if stop == "max_tokens":
            if verder >= doorgaan and doorgaan:
                usage["afgebroken"] = True
                break
            if verder >= doorgaan:
                raise RuntimeError("Het antwoord werd te lang en is afgebroken. Probeer het met een kortere tekst of opdracht.")
            verder += 1
            # Het afgebroken antwoord als afgeronde beurt terugsturen en om de rest vragen.
            body = dict(body, messages=basis + [{"role": "assistant", "content": _als_voorzet(inhoud)},
                                                {"role": "user", "content": VERDER}])
            continue
        if stop != "pause_turn":
            break
        # pause_turn (lange zoektocht): het antwoord tot nu toe terugsturen, de API gaat dan zelf verder.
        voorzet = _als_voorzet(inhoud)
        laatste = body["messages"][-1]
        if laatste.get("role") == "user" and laatste.get("content") == VERDER:
            body = dict(body, messages=body["messages"] + [{"role": "assistant", "content": _als_voorzet(r.get("content") or [])}])
        else:
            body = dict(body, messages=basis + ([{"role": "assistant", "content": voorzet}] if voorzet else []))
    tekst = "".join(b.get("text", "") for b in inhoud if b.get("type") == "text")
    bronnen, gezien = [], set()
    for b in inhoud:
        for c in b.get("citations") or []:
            url = c.get("url")
            if url and url not in gezien:
                gezien.add(url)
                bronnen.append({"titel": (c.get("title") or url)[:150], "url": url})
    return tekst, usage, bronnen


def json_uit(tekst, eerste_sleutel=None):
    """Haalt het JSON-object uit een antwoord (ook als er tekst omheen staat)."""
    t = tekst.replace("```json", "").replace("```", "")
    start = t.find('{"' + eerste_sleutel) if eerste_sleutel else -1
    if start < 0:
        start = t.find("{")
    eind = t.rfind("}")
    if start < 0 or eind <= start:
        raise ValueError("Geen JSON in het antwoord van Claude.")
    return json.loads(t[start:eind + 1])
