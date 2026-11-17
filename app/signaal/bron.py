"""De Signal-container (bbernhard/signal-cli-rest-api, MODE=native) via zijn REST-API op het interne netwerk.

Buurmanitor gebruikt alleen: de QR-code om te koppelen, de lijst met accounts, de groepen (voor de namen) en het
ophalen van nieuwe berichten. Versturen zit er bewust niet in.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("SIGNAL_URL", "").rstrip("/")


class SignalFout(Exception):
    def __init__(self, tekst, code=None):
        super().__init__(tekst)
        self.code = code


def actief():
    return bool(URL)


def _vraag(pad, timeout=30, ruw=False):
    if not URL:
        raise SignalFout("Signal staat nog niet aan op de server (SIGNAL_URL ontbreekt).")
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + pad, headers={"Accept": "*/*"}), timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        tekst = e.read()[:500].decode("utf-8", "replace")
        try:
            tekst = json.loads(tekst).get("error") or tekst
        except (ValueError, AttributeError):
            pass
        raise SignalFout(f"Signal gaf fout {e.code}: {tekst[:200]}", e.code) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise SignalFout(f"De Signal-container is niet bereikbaar ({getattr(e, 'reason', e)}).") from e
    if ruw:
        return data
    return json.loads(data or b"null")


def accounts():
    """De gekoppelde nummers (leeg zolang er niet gekoppeld is)."""
    try:
        lijst = _vraag("/v1/accounts") or []
    except SignalFout as e:
        # Vóór de eerste koppeling heeft signal-cli nog geen lijst met accounts en geeft de container fout 500
        # ("Failed to read local accounts list"). Dat betekent gewoon: nog niet gekoppeld.
        if e.code == 500 and "accounts" in str(e).lower():
            return []
        raise
    return [a for a in lijst if a]


def qr_code(naam="Buurmanitor"):
    """De QR-code (PNG) om Buurmanitor als gekoppeld apparaat toe te voegen. Elke keer een nieuwe; even geldig."""
    return _vraag("/v1/qrcodelink?" + urllib.parse.urlencode({"device_name": naam}), timeout=60, ruw=True)


def groepen(nummer):
    return _vraag(f"/v1/groups/{urllib.parse.quote(nummer)}", timeout=60) or []


def ontvang(nummer):
    """Nieuwe berichten (en ontvangstbevestigingen, typen, enzovoort) sinds de vorige keer. Signal geeft ze maar één keer."""
    return _vraag(f"/v1/receive/{urllib.parse.quote(nummer)}?timeout=5", timeout=120) or []
