"""Gedeelde webonderdelen: sjablonen, context, herkomstcontrole en de toegangscontrole via Caddy."""
import datetime as dt
import hmac
import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from . import config

BASIS = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(BASIS, "templates"))

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
           "november", "december"]
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def datum_lang(d):
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


def geleden(t):
    if not t:
        return "nooit"
    s = (dt.datetime.now(config.TZ) - t).total_seconds()
    if s < 90:
        return "zojuist"
    if s < 3600:
        return f"{int(s // 60)} minuten geleden"
    if s < 86400:
        return f"{int(s // 3600)} uur geleden"
    return f"{int(s // 86400)} dagen geleden"


def euro(x):
    return f"€ {float(x or 0):.2f}".replace(".", ",")


def mb(n):
    return f"{n / 1024 ** 2:.1f} MB".replace(".", ",")


def ctx(request: Request, scherm, **kw):
    kw.update(request=request, scherm=scherm, gebruiker=request.headers.get("Remote-Name")
              or request.headers.get("Remote-User") or "", auth_url=config.AUTH_URL,
              geleden=geleden, datum_lang=datum_lang, domeinen=config.DOMEINEN, euro=euro, mb=mb)
    return kw


def herkomst_toegestaan(origin, referer, toegestaan):
    """Komt een formulier van een van onze eigen adressen? Vergelijkt het adres exact, niet als stukje tekst."""
    herkomst = origin or referer or ""
    if not herkomst or herkomst == "null":
        return False
    adres = urlsplit(herkomst)
    return adres.scheme == "https" and (adres.hostname or "").lower() in toegestaan


def controleer_herkomst(request: Request):
    if not herkomst_toegestaan(request.headers.get("origin"), request.headers.get("referer"), config.TOEGESTANE_HOSTS):
        raise HTTPException(403, "Onbekende herkomst")


async def alleen_via_caddy(request: Request, call_next):
    """Alleen verzoeken via Caddy (met het geheim) en Authelia (met Remote-User), behalve de open paden."""
    if not request.url.path.startswith(config.OPEN_PADEN):
        if config.PROXY_GEHEIM and not hmac.compare_digest(request.headers.get("X-Proxy-Geheim", ""), config.PROXY_GEHEIM):
            return JSONResponse({"fout": "niet via de proxy"}, status_code=401)
        if not request.headers.get("Remote-User"):
            return JSONResponse({"fout": "niet ingelogd"}, status_code=401)
    return await call_next(request)
