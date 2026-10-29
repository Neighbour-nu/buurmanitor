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


def open_meldingen(c, limiet=20):
    return c.execute("""SELECT * FROM meldingen WHERE NOT gelezen
                        ORDER BY CASE niveau WHEN 'urgent' THEN 0 WHEN 'belangrijk' THEN 1 ELSE 2 END, id DESC
                        LIMIT %s""", (limiet,)).fetchall()


def nieuw_sinds(c, na_id):
    return c.execute("SELECT id, niveau, bron, titel, tekst, link FROM meldingen WHERE id > %s AND NOT gelezen "
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
