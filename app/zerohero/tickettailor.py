"""Ticket Tailor: alle bestellingen zelf ophalen, naast de webhook die n8n ontvangt.

De webhook meldt nieuwe verkopen direct (met een Telegram-bericht). Mist hij er een, bijvoorbeeld een verkoop van vóór
de koppeling of tijdens een storing, dan vult deze ophaalronde het aan. Bestaande bestellingen worden bijgewerkt
(status, aantal), er gaat geen extra Telegram-bericht uit.

Sleutel: TT_API_KEY in ~/cockpit/.env (Ticket Tailor > Box office settings > API), via deploy/tickettailor.sh.
"""
from . import omgeving
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

API = "https://api.tickettailor.com"
SLEUTEL = os.environ.get("ZEROHERO_TT_API_KEY", "").strip()
INTERVAL = 3 * 3600


def actief():
    return bool(SLEUTEL)


def _get(pad):
    auth = base64.b64encode(f"{SLEUTEL}:".encode()).decode()
    req = urllib.request.Request(API + pad, headers={"Authorization": f"Basic {auth}", "Accept": "application/json",
                                                     "User-Agent": "Doellijn/1.0 (+https://zerohero.one)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def bestellingen(max_paginas=50):
    """Alle bestellingen, nieuwste eerst, pagina voor pagina."""
    uit, pad = [], "/v1/orders?limit=100"
    for _ in range(max_paginas):
        r = _get(pad)
        uit += r.get("data") or []
        volgende = (r.get("links") or {}).get("next")
        if not volgende:
            break
        pad = volgende if volgende.startswith("/") else "/" + volgende.split("tickettailor.com/", 1)[-1]
    return uit


def _rij(o):
    """Zelfde vertaling als de webhook in n8n (n8n/build_tt.py)."""
    regels = o.get("line_items") or []
    tickets = [r for r in regels if not r.get("type") or "ticket" in str(r.get("type")).lower()]
    aantal = sum(int(r.get("quantity") or 0) for r in tickets)
    if not aantal and isinstance(o.get("issued_tickets"), list):
        aantal = len(o["issued_tickets"])
    types = {}
    for r in tickets:
        k = r.get("description") or "Ticket"
        types[k] = types.get(k, 0) + int(r.get("quantity") or 0)
    ev = o.get("event_summary") or {}
    valuta = o.get("currency")
    valuta = (valuta.get("code") if isinstance(valuta, dict) else valuta) or "eur"
    return {"order_id": o.get("id"), "event_id": ev.get("id") or o.get("event_id"), "status": str(o.get("status") or "").lower(),
            "aantal": aantal, "totaal_cent": int(o.get("total") or 0), "valuta": str(valuta).upper(), "tickettypes": json.dumps(types),
            "besteld": o.get("created_at"), "evenement": ev.get("name") or ""}


def opslaan(q, orders):
    nieuw = 0
    for o in orders:
        r = _rij(o)
        if not r["order_id"]:
            continue
        besteld = r["besteld"]
        besteld_sql = "to_timestamp(%s)" if isinstance(besteld, (int, float)) or str(besteld).isdigit() else "coalesce(%s::timestamptz, now())"
        res = q(f"""INSERT INTO tt_orders (order_id, event_id, status, aantal, totaal_cent, valuta, tickettypes, besteld, evenement)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, {besteld_sql}, %s)
                    ON CONFLICT (order_id) DO UPDATE SET status = EXCLUDED.status, aantal = EXCLUDED.aantal,
                      totaal_cent = EXCLUDED.totaal_cent, tickettypes = EXCLUDED.tickettypes,
                      evenement = coalesce(nullif(EXCLUDED.evenement, ''), tt_orders.evenement),
                      event_id = coalesce(tt_orders.event_id, EXCLUDED.event_id)
                    RETURNING (xmax = 0) AS nieuw""",
                (r["order_id"], r["event_id"], r["status"], r["aantal"], r["totaal_cent"], r["valuta"], r["tickettypes"],
                 int(besteld) if str(besteld).isdigit() else besteld, r["evenement"]))
        nieuw += 1 if res and res[0]["nieuw"] else 0
    return nieuw


def ververs(q):
    """Ophalen en opslaan; status in de tabel koppelingen. Geeft (ok, melding) terug."""
    if not actief():
        return False, "Geen TT_API_KEY ingesteld (deploy/tickettailor.sh)."
    try:
        orders = bestellingen()
        nieuw = opslaan(q, orders)
        fout, melding = None, f"{len(orders)} bestellingen bekeken, {nieuw} nieuw toegevoegd."
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:400].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        try:
            detail = (json.loads(detail).get("message") or json.loads(detail).get("error") or detail) if detail.strip().startswith("{") else detail
        except ValueError:
            pass
        if "<html" in str(detail).lower():
            detail = "geweigerd door de beveiliging van Ticket Tailor (geen API-antwoord)"
        fout = {401: "Ticket Tailor weigert de sleutel (401). Controleer of je de hele sleutel hebt geplakt.",
                403: "Geen toegang (403).",
                429: "Te veel verzoeken (429). De volgende ronde gaat vanzelf goed."}.get(e.code, f"Ticket Tailor gaf fout {e.code}.")
        if detail:
            fout += f" Ticket Tailor: {str(detail).strip()[:200]}"
        melding = fout
    except Exception as e:  # noqa: BLE001
        fout = melding = "Ticket Tailor niet bereikbaar: " + str(e)[:200]
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('tickettailor', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""", (fout is None, fout))
    return fout is None, melding


def status(q):
    return (q("SELECT * FROM koppelingen WHERE naam = 'tickettailor'") or [None])[0]


_gestart = False


def start_achtergrond(q):
    global _gestart
    if _gestart or not actief() or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(20)
        while True:
            try:
                ververs(q)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=lus, name="tickettailor", daemon=True).start()
