"""Koppeling met MailerLite (nieuwe API, connect.mailerlite.com).

Haalt periodiek op: aantal actieve abonnees, formulieren (aanmeldingen per formulier) en de laatst
verzonden nieuwsbrieven met hun statistieken. Alles wordt bewaard, zodat het dashboard trends kan
tonen (MailerLite zelf geeft vooral standen van nu).

Sleutel: MAILERLITE_API_KEY in ~/cockpit/.env (MailerLite > Integraties > MailerLite API).
"""
from . import omgeving
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://connect.mailerlite.com/api"
SLEUTEL = os.environ.get("ZEROHERO_MAILERLITE_API_KEY", "").strip()
INTERVAL = 3 * 3600  # elke drie uur

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS koppelingen (
        naam text PRIMARY KEY, laatst timestamptz, ok boolean, fout text)""",
    """CREATE TABLE IF NOT EXISTS ml_formulieren (
        id text PRIMARY KEY, naam text, soort text, conversies int, weergaven int, ratio numeric,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS ml_campagnes (
        id text PRIMARY KEY, naam text, onderwerp text, verzonden_op timestamptz, verzonden int,
        opens int, open_ratio numeric, kliks int, klik_ratio numeric, afmeldingen int,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
]


def actief():
    return bool(SLEUTEL)


def _get(pad, params=None):
    url = API + pad + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + SLEUTEL, "Accept": "application/json",
                                               "User-Agent": "Doellijn/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def _ratio(v):
    """MailerLite geeft ratio's als {'float': 0.42, 'string': '42%'} of als getal."""
    if isinstance(v, dict):
        v = v.get("float", v.get("value"))
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f * 100, 1) if f <= 1 else round(f, 1)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _tijd(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None


def ophalen():
    """Haalt alles op bij MailerLite en geeft een dict terug (nog niets opgeslagen)."""
    try:
        abonnees = _get("/subscribers", {"limit": 0, "filter[status]": "active"}).get("total")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise
        abonnees = _get("/subscribers", {"limit": 0}).get("total")

    formulieren = []
    for soort in ("embedded", "popup", "promotion"):
        try:
            data = _get(f"/forms/{soort}", {"limit": 100}).get("data") or []
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise
            continue
        for f in data:
            formulieren.append({"id": str(f.get("id")), "naam": f.get("name") or "(zonder naam)", "soort": soort,
                                "conversies": _int(f.get("conversions_count")), "weergaven": _int(f.get("opens_count")),
                                "ratio": _ratio(f.get("conversion_rate"))})

    campagnes = []
    data = _get("/campaigns", {"filter[status]": "sent", "limit": 25}).get("data") or []
    for c in data:
        mail = (c.get("emails") or [{}])[0] or {}
        st = mail.get("stats") or c.get("stats") or {}
        campagnes.append({
            "id": str(c.get("id")), "naam": c.get("name") or "", "onderwerp": mail.get("subject") or "",
            "verzonden_op": _tijd(c.get("finished_at") or c.get("scheduled_for") or c.get("updated_at")),
            "verzonden": _int(st.get("sent")), "opens": _int(st.get("unique_opens_count") or st.get("opens_count")),
            "open_ratio": _ratio(st.get("open_rate")), "kliks": _int(st.get("unique_clicks_count") or st.get("clicks_count")),
            "klik_ratio": _ratio(st.get("click_rate")), "afmeldingen": _int(st.get("unsubscribes_count"))})
    campagnes.sort(key=lambda c: c["verzonden_op"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return {"abonnees": abonnees, "formulieren": formulieren, "campagnes": campagnes}


def opslaan(q, gegevens):
    vandaag = dt.date.today()

    def meting(kanaal, sleutel, waarde):
        if waarde is None:
            return
        q("""INSERT INTO metingen (kanaal, sleutel, datum, waarde, bron) VALUES (%s, %s, %s, %s, 'api')
             ON CONFLICT (kanaal, sleutel, datum) DO UPDATE SET waarde = EXCLUDED.waarde, bron = 'api', bijgewerkt = now()
             RETURNING 1""", (kanaal, sleutel, vandaag, waarde))

    meting("nieuwsbrief", "abonnees", gegevens["abonnees"])
    for f in gegevens["formulieren"]:
        q("""INSERT INTO ml_formulieren (id, naam, soort, conversies, weergaven, ratio, bijgewerkt)
             VALUES (%s, %s, %s, %s, %s, %s, now())
             ON CONFLICT (id) DO UPDATE SET naam = EXCLUDED.naam, soort = EXCLUDED.soort, conversies = EXCLUDED.conversies,
               weergaven = EXCLUDED.weergaven, ratio = EXCLUDED.ratio, bijgewerkt = now() RETURNING 1""",
          (f["id"], f["naam"], f["soort"], f["conversies"], f["weergaven"], f["ratio"]))
        meting("formulier", f["id"], f["conversies"])
    for c in gegevens["campagnes"]:
        q("""INSERT INTO ml_campagnes (id, naam, onderwerp, verzonden_op, verzonden, opens, open_ratio, kliks, klik_ratio, afmeldingen, bijgewerkt)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
             ON CONFLICT (id) DO UPDATE SET naam = EXCLUDED.naam, onderwerp = EXCLUDED.onderwerp, verzonden_op = EXCLUDED.verzonden_op,
               verzonden = EXCLUDED.verzonden, opens = EXCLUDED.opens, open_ratio = EXCLUDED.open_ratio, kliks = EXCLUDED.kliks,
               klik_ratio = EXCLUDED.klik_ratio, afmeldingen = EXCLUDED.afmeldingen, bijgewerkt = now() RETURNING 1""",
          (c["id"], c["naam"], c["onderwerp"], c["verzonden_op"], c["verzonden"], c["opens"], c["open_ratio"],
           c["kliks"], c["klik_ratio"], c["afmeldingen"]))
    if gegevens["campagnes"]:
        meting("nieuwsbrief", "openratio", gegevens["campagnes"][0]["open_ratio"])


def ververs(q):
    """Ophalen en opslaan; houdt de status bij in de tabel koppelingen. Geeft (ok, foutmelding) terug."""
    if not actief():
        return False, "Geen MAILERLITE_API_KEY ingesteld"
    try:
        opslaan(q, ophalen())
        fout = None
    except urllib.error.HTTPError as e:
        fout = {401: "MailerLite weigert de sleutel (401). Maak een nieuwe API-sleutel aan.",
                403: "Geen toegang (403). Controleer de rechten van de API-sleutel.",
                429: "Te veel verzoeken (429). De volgende ronde gaat vanzelf goed."}.get(e.code, f"MailerLite gaf fout {e.code}.")
    except Exception as e:  # noqa: BLE001
        fout = "MailerLite niet bereikbaar: " + str(e)[:200]
    q("""INSERT INTO koppelingen (naam, laatst, ok, fout) VALUES ('mailerlite', now(), %s, %s)
         ON CONFLICT (naam) DO UPDATE SET laatst = now(), ok = EXCLUDED.ok, fout = EXCLUDED.fout RETURNING 1""",
      (fout is None, fout))
    return fout is None, fout


_gestart = False


def start_achtergrond(q):
    """Start eenmalig een achtergronddraad die elke drie uur ververst."""
    global _gestart
    if _gestart or not actief() or not omgeving.ACHTERGROND:
        return
    _gestart = True

    def lus():
        time.sleep(5)
        while True:
            try:
                ververs(q)
            except Exception:  # noqa: BLE001  (nooit de draad laten stoppen)
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=lus, name="mailerlite", daemon=True).start()
