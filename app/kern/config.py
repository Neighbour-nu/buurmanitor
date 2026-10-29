"""Instellingen van Buurmanitor, uit de omgeving (.env via docker-compose)."""
import os
import zoneinfo

TZ = zoneinfo.ZoneInfo("Europe/Amsterdam")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
PORTAL_HOST = os.environ.get("PORTAL_HOST", "buurmanitor.neighbour.nu").lower()
AUTH_URL = os.environ.get("AUTH_URL", "https://auth.neighbour.nu")

# Geheim dat Caddy bij elk verzoek meestuurt (X-Proxy-Geheim). Is het ingesteld, dan komen alleen
# verzoeken via Caddy en Authelia binnen.
PROXY_GEHEIM = os.environ.get("PROXY_GEHEIM", "")
# Adressen waarvandaan formulieren mogen komen: de portal plus eventueel extra adressen (komma's).
TOEGESTANE_HOSTS = {PORTAL_HOST} | {h.strip().lower() for h in os.environ.get("TOEGESTANE_HOSTS", "").split(",") if h.strip()}
# Zonder login bereikbaar: alleen onschuldige of interne paden.
OPEN_PADEN = ("/gezond", "/static/", "/favicon")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_MODEL_LICHT = os.environ.get("CLAUDE_MODEL_LICHT", "claude-haiku-4-5")
MAANDPLAFOND_EUR = float(os.environ.get("MAANDPLAFOND_EUR", "20") or 20)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PLANNER_AAN = os.environ.get("BUURMANITOR_PLANNER", "1") != "0"
BACKUP_MAP = os.environ.get("BACKUP_MAP", "/backups")

# Werkruimtes. Politiek draait onder /politiek zodra POLITIEK_DATABASE_URL is ingesteld (fase 1); daarvoor nog als
# eigen app (Promonitor). ZEROHERO volgt in fase 2 onder /zerohero.
POLITIEK_ACTIEF = bool(os.environ.get("POLITIEK_DATABASE_URL"))


def werkruimtes():
    return [
        {"sleutel": "politiek", "naam": "Politiek", "app": "Promonitor",
         "url": "/politiek" if POLITIEK_ACTIEF else os.environ.get("URL_POLITIEK", "https://promonitor.neighbour.nu")},
        {"sleutel": "zerohero", "naam": "ZEROHERO", "app": "Doellijn",
         "url": os.environ.get("URL_ZEROHERO", "https://cockpit.zerohero.one")},
    ]


DOMEINEN = [
    ("politiek", "Politiek"), ("prive", "Privé"), ("zerohero", "ZEROHERO"), ("fbim", "FB/IM"), ("upperspire", "Upper Spire"),
]
