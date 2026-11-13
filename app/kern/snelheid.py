"""Hoe snel Buurmanitor reageert: de traagste verzoeken van de laatste tijd en de bezetting van de databasepools.

Alles in het geheugen van het proces (na een herstart begint het opnieuw); te zien op de pagina Gezondheid.
"""
import collections
import datetime as dt
import threading
import time

from . import config

TRAAG_MS = 1000  # vanaf hier telt een verzoek als traag
_traag = collections.deque(maxlen=40)
_per_pad = {}  # pad (zonder getallen) -> [aantal, totaal ms, max ms]
_slot = threading.Lock()
_sinds = time.time()


def _soort(pad):
    """'/bram/bericht/123' -> '/bram/bericht/#', zodat dezelfde soort pagina samen telt. Pure functie."""
    return "/".join("#" if deel.isdigit() else deel for deel in pad.split("/"))


def noteer(methode, pad, ms):
    with _slot:
        s = _per_pad.setdefault(f"{methode} {_soort(pad)}", [0, 0.0, 0.0])
        s[0] += 1
        s[1] += ms
        s[2] = max(s[2], ms)
        if ms >= TRAAG_MS:
            _traag.appendleft((time.time(), methode, pad, round(ms)))


def overzicht(aantal=10):
    """De traagste soorten pagina's (gemiddeld) en de laatste trage verzoeken."""
    with _slot:
        rij = sorted(((k, n, t / n, m) for k, (n, t, m) in _per_pad.items() if n), key=lambda x: -x[2])[:aantal]
        return {"per_pad": [{"pad": k, "aantal": n, "gem": round(g), "max": round(m)} for k, n, g, m in rij],
                "traag": [{"tijd": dt.datetime.fromtimestamp(t, config.TZ), "methode": me, "pad": p, "ms": ms} for t, me, p, ms in _traag],
                "sinds": _sinds}


def pools(**pools_):
    """Bezetting per pool: grootte, in gebruik, wachtend, en hoe vaak er gewacht moest worden."""
    uit = []
    for naam, p in pools_.items():
        if p is None:
            continue
        try:
            s = p.get_stats()
        except Exception:  # noqa: BLE001
            continue
        grootte = s.get("pool_size", 0)
        uit.append({"naam": naam, "max": p.max_size, "open": grootte, "vrij": s.get("pool_available", 0),
                    "bezet": grootte - s.get("pool_available", 0), "wachtend": s.get("requests_waiting", 0),
                    "gewacht": s.get("requests_queued", 0), "wacht_ms": s.get("requests_wait_ms", 0),
                    "te_lang": s.get("requests_errors", 0)})
    return uit


async def meten(request, call_next):
    """Middleware: meet hoe lang elk verzoek duurt (statische bestanden niet)."""
    if request.url.path.startswith("/static"):
        return await call_next(request)
    begin = time.perf_counter()
    try:
        return await call_next(request)
    finally:
        noteer(request.method, request.url.path, (time.perf_counter() - begin) * 1000)
