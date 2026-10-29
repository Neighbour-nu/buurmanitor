"""Berichten naar Martijn sturen: mail (SMTP, bijvoorbeeld Gmail met een app-wachtwoord) en Telegram.

Instellingen komen uit de omgeving (~/promonitor/.env):
SMTP_HOST, SMTP_PORT (465 = SSL, 587 = STARTTLS), SMTP_USER, SMTP_PASSWORD,
MAIL_AAN, MAIL_VAN (optioneel, bijvoorbeeld 'Promonitor <promonitor@neighbour.nu>'),
TELEGRAM_BOT_TOKEN en TELEGRAM_CHAT_ID.

Testen op de server:
    cd ~/promonitor && docker compose exec app python -m app.berichten
"""
import json
import os
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def _env(naam, standaard=""):
    return (os.environ.get("POLITIEK_" + naam) or standaard).strip()  # in Buurmanitor: POLITIEK_SMTP_HOST enz.


def stuur_mail(onderwerp, tekst, html=None, aan=None):
    host, poort = _env("SMTP_HOST"), int(_env("SMTP_PORT", "465"))
    gebruiker, wachtwoord = _env("SMTP_USER"), _env("SMTP_PASSWORD").replace(" ", "")
    aan = aan or _env("MAIL_AAN")
    if not (host and gebruiker and wachtwoord and aan):
        raise RuntimeError("Mail is niet ingesteld: SMTP_HOST, SMTP_USER, SMTP_PASSWORD en MAIL_AAN zijn nodig.")
    bericht = EmailMessage()
    bericht["Subject"] = onderwerp
    bericht["From"] = _env("MAIL_VAN") or gebruiker
    bericht["To"] = aan
    bericht["Date"] = formatdate(localtime=True)
    bericht["Message-ID"] = make_msgid(domain=gebruiker.split("@")[-1])
    bericht.set_content(tekst)
    if html:
        bericht.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    if poort == 465:
        with smtplib.SMTP_SSL(host, poort, context=context, timeout=30) as s:
            s.login(gebruiker, wachtwoord)
            s.send_message(bericht)
    else:
        with smtplib.SMTP(host, poort, timeout=30) as s:
            s.starttls(context=context)
            s.login(gebruiker, wachtwoord)
            s.send_message(bericht)


def stuur_telegram(tekst, html=False):
    token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        raise RuntimeError("Telegram is niet ingesteld: TELEGRAM_BOT_TOKEN en TELEGRAM_CHAT_ID zijn nodig.")
    velden = {"chat_id": chat, "text": tekst, "disable_web_page_preview": "true"}
    if html:
        velden["parse_mode"] = "HTML"
    data = urllib.parse.urlencode(velden).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
        antwoord = json.load(r)
    if not antwoord.get("ok"):
        raise RuntimeError(f"Telegram weigert: {antwoord.get('description')}")


def _test():
    """Stuurt een testmail en een Telegram-testbericht en zegt per kanaal of het lukte."""
    goed = True
    tekst = ("Dit is een test van Promonitor.\n\n"
             "Komt dit bericht aan, dan werkt dit kanaal. Vanaf fase 1 krijg je hier elke ochtend "
             "om 8.00 uur de briefing.")
    for naam, functie in (("Mail", lambda: stuur_mail("Promonitor: testbericht", tekst)),
                          ("Telegram", lambda: stuur_telegram("Promonitor: testbericht\n\n" + tekst))):
        try:
            functie()
            print(f"{naam}: verstuurd")
        except smtplib.SMTPAuthenticationError:
            goed = False
            print(f"{naam}: inloggen geweigerd. Klopt SMTP_USER, en is SMTP_PASSWORD het app-wachtwoord?")
        except Exception as e:  # noqa: BLE001 - de melding is voor de gebruiker
            goed = False
            print(f"{naam}: mislukt ({e})")
    print(f"Afzender: {_env('MAIL_VAN') or _env('SMTP_USER') or '-'}  Aan: {_env('MAIL_AAN') or '-'}")
    return 0 if goed else 1


if __name__ == "__main__":
    sys.exit(_test())
