"""Koppeling met de WooCommerce-webshop op zerohero.one (boeken en T-shirts).

Gebruikt de WordPress-inlog van de cockpit (WP_USER en WP_APP_PASSWORD, applicatiewachtwoord).
Die gebruiker moet beheerder of winkelmanager zijn. Haalt elke drie uur de bestellingen op en
bewaart ze, zodat het dashboard verkopen per periode en per product kan tonen.
Alleen bestellingen met status processing (in behandeling) of completed (afgerond) tellen als verkocht.
"""
from . import omgeving
import datetime as dt
import threading
import time
import urllib.error
import urllib.parse

from . import wp

VERKOCHT = ("processing", "completed")
INTERVAL = 3 * 3600

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS woo_producten (
        id int PRIMARY KEY, naam text, categorie text, prijs numeric, bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS woo_orders (
        id int PRIMARY KEY, status text, besteld timestamptz, totaal numeric, valuta text, items jsonb,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS woo_orders_besteld ON woo_orders (besteld)",
]


def actief():
    return wp.wp_actief()


def _get(pad, params):
    return wp._wp("GET", pad + "?" + urllib.parse.urlencode(params), timeout=30)


def _alle(pad, params, max_paginas=30):
    uit = []
    for pagina in range(1, max_paginas + 1):
        rijen = _get(pad, {**params, "per_page": 100, "page": pagina}) or []
        uit.extend(rijen)
        if len(rijen) < 100:
            break
    return uit


def _tijd(v):
    if not v:
        return None
    return dt.datetime.fromisoformat(str(v).replace("Z", "")).replace(tzinfo=dt.timezone.utc)


def ververs(q):
    """Haalt producten en bestellingen op en slaat ze op. Geeft (ok, foutmelding) terug."""
    if not actief():
        return False, "Geen WordPress-gegevens voor de cockpit ingesteld"
    try:
        producten = _alle("/wc/v3/products", {"status": "any", "_fields": "id,name,categories,price"})
        for p in producten:
            cat = ", ".join(c.get("name", "") for c in p.get("categories") or [])
            try:
                prijs = float(p.get("price") or 0)
            except ValueError:
                prijs = None
            q("""INSERT INTO woo_producten (id, naam, categorie, prijs, bijgewerkt) VALUES (%s, %s, %s, %s, now())
                 ON CONFLICT (id) DO UPDATE SET naam = EXCLUDED.naam, categorie = EXCLUDED.categorie, prijs = EXCLUDED.prijs,
                   bijgewerkt = now() RETURNING 1""", (p["id"], p.get("name"), cat, prijs))
        # Eerste keer: alles. Daarna alleen wat de afgelopen 45 dagen is gewijzigd.
        # Volledig ophalen bij de eerste keer, of als oudere regels nog zonder btw zijn opgeslagen.
        al = q1_waarde(q, """SELECT count(*) AS n FROM woo_orders
                              WHERE NOT EXISTS (SELECT 1 FROM woo_orders w, jsonb_array_elements(w.items) li WHERE NOT (li ? 'incl'))""")
        params = {"orderby": "date", "order": "desc",
                  "_fields": "id,status,date_created_gmt,total,currency,line_items"}
        if al:
            params["modified_after"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S")
        for o in _alle("/wc/v3/orders", params):
            items = [{"product_id": li.get("product_id"), "naam": li.get("name"), "aantal": li.get("quantity") or 0,
                      "totaal": round(_f(li.get("total")) + _f(li.get("total_tax")), 2), "incl": True}
                     for li in o.get("line_items") or []]
            q("""INSERT INTO woo_orders (id, status, besteld, totaal, valuta, items, bijgewerkt)
                 VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                 ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, totaal = EXCLUDED.totaal, items = EXCLUDED.items,
                   bijgewerkt = now() RETURNING 1""",
              (o["id"], o.get("status"), _tijd(o.get("date_created_gmt")), o.get("total") or 0, o.get("currency"),
               _json(items)))
        fout = None
    except urllib.error.HTTPError as e:
        fout = {401: "WordPress weigert de inlog van de cockpit (401). Draai deploy/wp_opnieuw.sh.",
                403: "De WordPress-gebruiker van de cockpit mag de webshop niet lezen (403). Die moet beheerder of winkelmanager zijn."
                }.get(e.code, f"WooCommerce gaf fout {e.code}.")
    except Exception as e:  # noqa: BLE001
        fout = "Webshop niet bereikbaar: " + str(e)[:200]
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('woocommerce', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""",
      (fout is None, fout))
    return fout is None, fout


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def q1_waarde(q, sql):
    rij = q(sql)
    return rij[0]["n"] if rij else 0


def _json(v):
    import json
    return json.dumps(v, ensure_ascii=False)


def overzicht(q, dagen):
    """Verkopen per product in de periode en de periode daarvoor (alleen verkochte bestellingen)."""
    rijen = q("""
        WITH r AS (
          SELECT o.besteld, (li->>'product_id')::int AS pid, li->>'naam' AS naam, (li->>'aantal')::int AS aantal,
                 coalesce((li->>'totaal')::numeric, 0) AS omzet
          FROM woo_orders o, jsonb_array_elements(o.items) li
          WHERE o.status = ANY(%s) AND o.besteld > now() - make_interval(days => %s * 2))
        SELECT r.pid, coalesce(p.naam, max(r.naam)) AS naam, coalesce(p.categorie, '') AS categorie,
               coalesce(sum(r.aantal) FILTER (WHERE r.besteld > now() - make_interval(days => %s)), 0) AS nu,
               coalesce(sum(r.aantal) FILTER (WHERE r.besteld <= now() - make_interval(days => %s)), 0) AS toen,
               coalesce(sum(r.omzet) FILTER (WHERE r.besteld > now() - make_interval(days => %s)), 0) AS omzet,
               coalesce(sum(r.omzet) FILTER (WHERE r.besteld <= now() - make_interval(days => %s)), 0) AS omzet_toen
        FROM r LEFT JOIN woo_producten p ON p.id = r.pid
        GROUP BY r.pid, p.naam, p.categorie ORDER BY nu DESC, naam""", (list(VERKOCHT), dagen, dagen, dagen, dagen, dagen))
    boeken = [r for r in rijen if "boek" in (r["categorie"] or "").lower()]
    totaal = q("""SELECT count(*) FILTER (WHERE besteld > now() - make_interval(days => %s)) AS bestellingen,
                         count(*) AS ooit
                  FROM woo_orders WHERE status = ANY(%s)""", (dagen, list(VERKOCHT)))[0]
    totaal["omzet"] = sum(r["omzet"] for r in rijen)
    return {"producten": rijen, "boeken_nu": sum(r["nu"] for r in boeken), "boeken_toen": sum(r["toen"] for r in boeken),
            "bestellingen": totaal["bestellingen"], "omzet": totaal["omzet"], "ooit": totaal["ooit"]}


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

    threading.Thread(target=lus, name="woocommerce", daemon=True).start()
