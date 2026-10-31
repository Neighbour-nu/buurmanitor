"""Meldingen: wat aandacht vraagt, op drie niveaus.

- urgent: banner op het dashboard, systeemmelding op je scherm (met geluid) en, als ingesteld, Telegram;
- belangrijk: systeemmelding zonder geluid en bovenaan in het dashboard;
- normaal: alleen in het dashboard.
Een sleutel voorkomt dat dezelfde melding twee keer verschijnt (bijvoorbeeld 'geheugen-2026-10-29').
"""
import json
import urllib.request

from . import config

NIVEAUS = ("urgent", "belangrijk", "normaal")


def melden(c, niveau, bron, titel, tekst="", link="", sleutel=None):
    """Maak een melding. Geeft het id terug, of None als deze sleutel al bestond."""
    assert niveau in NIVEAUS
    r = c.execute("""INSERT INTO meldingen (niveau, bron, titel, tekst, link, sleutel) VALUES (%s, %s, %s, %s, %s, %s)
                     ON CONFLICT (sleutel) DO NOTHING RETURNING id""",
                  (niveau, bron, titel, tekst, link, sleutel)).fetchone()
    if r and niveau == "urgent":
        telegram(f"⚠️ {titel}\n{tekst}".strip())
    return r["id"] if r else None


# De mappen op het scherm Meldingen: (status, naam, uitleg).
MAPPEN = (("open", "Open", "Nog te doen"), ("opgelost", "Opgelost", "Afgevinkt"),
          ("archief", "Archief", "Bewaard, niet meer in beeld"), ("verwijderd", "Prullenbak", "Verwijderd"))
# Wat je met een melding kunt doen, en de status die daarbij hoort.
ACTIES = {"opgelost": "opgelost", "archief": "archief", "verwijder": "verwijderd", "terug": "open"}


def afhandelen(c, mid, actie):
    """Een melding afvinken, archiveren, verwijderen of terugzetten. Alles behalve terugzetten telt ook als gelezen,
    zodat hij uit de banner en het dashboard verdwijnt."""
    status = ACTIES[actie]
    if status == "open":
        return c.execute("UPDATE meldingen SET status = 'open', afgehandeld = NULL WHERE id = %s RETURNING id", (mid,)).fetchone()
    return c.execute("UPDATE meldingen SET status = %s, gelezen = true, afgehandeld = now() WHERE id = %s RETURNING id",
                     (status, mid)).fetchone()


def aantallen(c):
    """Per map het aantal meldingen, en bij Open ook hoeveel er nog ongelezen zijn."""
    uit = {m[0]: 0 for m in MAPPEN} | {"ongelezen": 0}
    for r in c.execute("SELECT status, count(*) AS n, count(*) FILTER (WHERE NOT gelezen) AS o FROM meldingen GROUP BY status"):
        uit[r["status"]] = r["n"]
        if r["status"] == "open":
            uit["ongelezen"] = r["o"]
    return uit


def open_meldingen(c, limiet=20):
    return c.execute("""SELECT * FROM meldingen WHERE NOT gelezen AND status = 'open'
                        ORDER BY CASE niveau WHEN 'urgent' THEN 0 WHEN 'belangrijk' THEN 1 ELSE 2 END, id DESC
                        LIMIT %s""", (limiet,)).fetchall()


def nieuw_sinds(c, na_id):
    return c.execute("SELECT id, niveau, bron, titel, tekst, link FROM meldingen WHERE id > %s AND NOT gelezen AND status = 'open' "
                     "AND niveau <> 'normaal' ORDER BY id", (na_id,)).fetchall()


def telegram(tekst):
    """Stuur een bericht via Telegram, als bot en chat zijn ingesteld. Fouten blijven stil (de melding staat ook in de portal)."""
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return False
    try:
        data = json.dumps({"chat_id": config.TELEGRAM_CHAT_ID, "text": tekst[:3500]}).encode()
        verzoek = urllib.request.Request(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage", data=data,
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(verzoek, timeout=15).read()
        return True
    except Exception:  # noqa: BLE001
        return False
