"""Telegram met knoppen: berichten (en foto's) sturen met knoppen eronder, en verwerken wat Martijn aantikt.

- Sturen: stuur(tekst, knoppen) en stuur_foto(png, bijschrift, knoppen); knoppen is een lijst rijen met (tekst, data).
- Tikken: een draad haalt de updates op bij Telegram (long polling, getUpdates). De data van een knop begint met een
  voorvoegsel ('kn:12:p'); HANDLERS[voorvoegsel](data, bericht) doet het werk en geeft terug wat er in Telegram moet
  veranderen. Alleen tikken uit de eigen chat (TELEGRAM_CHAT_ID) tellen.
- Antwoorden: een tekst als antwoord op een bericht van de bot gaat naar ANTWOORDEN (bijvoorbeeld een eigen reden).
- Staat er bij Telegram een webhook op deze bot (bijvoorbeeld een Telegram-trigger in n8n), dan weigert Telegram
  getUpdates (409). Dat staat dan in STATUS en op de schermen die knoppen gebruiken.
"""
import html
import json
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid

from . import config

HANDLERS = {}  # voorvoegsel -> functie(data, bericht) -> {"toast": str, "tekst": str|None, "knoppen": list|None}
ANTWOORDEN = []  # functies(op_bericht_id, tekst) -> str|None (None: niet voor mij)
STATUS = {"draait": False, "fout": "", "laatst": None}
_draad = None


def actief():
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def e(t):
    return html.escape(str(t or ""), quote=False)


def _url(methode):
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{methode}"


def _vraag(methode, data=None, timeout=30):
    verzoek = urllib.request.Request(_url(methode), data=json.dumps(data or {}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(verzoek, timeout=timeout) as r:
            j = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as fout:
        try:
            j = json.loads(fout.read() or b"{}")
        except ValueError:
            j = {}
        raise TelegramFout(j.get("description") or f"Telegram gaf fout {fout.code}", fout.code) from None
    if not j.get("ok"):
        raise TelegramFout(j.get("description") or "Telegram weigerde het bericht", j.get("error_code"))
    return j.get("result")


class TelegramFout(Exception):
    def __init__(self, tekst, code=None):
        super().__init__(tekst)
        self.code = code


def markup(knoppen):
    return {"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in rij] for rij in knoppen]} if knoppen else {"inline_keyboard": []}


def stuur(tekst, knoppen=None, antwoord_op=None):
    """Een HTML-bericht (hooguit 4096 tekens). Geeft het bericht van Telegram terug."""
    data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": tekst[:4096], "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if knoppen:
        data["reply_markup"] = markup(knoppen)
    if antwoord_op:
        data["reply_parameters"] = {"message_id": antwoord_op, "allow_sending_without_reply": True}
    return _vraag("sendMessage", data)


def stuur_foto(png, bijschrift="", knoppen=None, bestandsnaam="beeld.png"):
    """Een foto met een HTML-bijschrift (hooguit 1024 tekens)."""
    grens = uuid.uuid4().hex
    delen = []

    def veld(naam, waarde):
        delen.append(f'--{grens}\r\nContent-Disposition: form-data; name="{naam}"\r\n\r\n{waarde}\r\n'.encode())
    veld("chat_id", config.TELEGRAM_CHAT_ID)
    veld("caption", bijschrift[:1024])
    veld("parse_mode", "HTML")
    if knoppen:
        veld("reply_markup", json.dumps(markup(knoppen)))
    delen.append(f'--{grens}\r\nContent-Disposition: form-data; name="photo"; filename="{bestandsnaam}"\r\n'
                 f"Content-Type: image/png\r\n\r\n".encode() + png + b"\r\n")
    delen.append(f"--{grens}--\r\n".encode())
    verzoek = urllib.request.Request(_url("sendPhoto"), data=b"".join(delen), headers={"Content-Type": f"multipart/form-data; boundary={grens}"})
    try:
        with urllib.request.urlopen(verzoek, timeout=60) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as fout:
        raise TelegramFout(f"Telegram gaf fout {fout.code} bij de foto", fout.code) from None
    if not j.get("ok"):
        raise TelegramFout(j.get("description") or "Telegram weigerde de foto")
    return j["result"]


def bewerk(bericht_id, tekst=None, knoppen=None):
    """Tekst en knoppen van een eerder bericht aanpassen (knoppen=[] haalt ze weg)."""
    if tekst is not None:
        data = {"chat_id": config.TELEGRAM_CHAT_ID, "message_id": bericht_id, "text": tekst[:4096], "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True}, "reply_markup": markup(knoppen)}
        return _vraag("editMessageText", data)
    return _vraag("editMessageReplyMarkup", {"chat_id": config.TELEGRAM_CHAT_ID, "message_id": bericht_id, "reply_markup": markup(knoppen)})


# ---------------------------------------------------------------- tikken verwerken

def van_mij(bericht):
    chat = ((bericht or {}).get("chat") or {}).get("id")
    return str(chat) == str(config.TELEGRAM_CHAT_ID)


def verwerk(update):
    """Eén update van Telegram. Geeft een korte uitslag (voor de tests)."""
    cq = update.get("callback_query")
    if cq:
        bericht = cq.get("message") or {}
        if not van_mij(bericht):
            return "niet van mij"
        data = cq.get("data") or ""
        functie = HANDLERS.get(data.split(":", 1)[0])
        toast, uit = "", None
        try:
            uit = functie(data, bericht) if functie else {"toast": "Deze knop ken ik niet meer."}
            toast = (uit or {}).get("toast", "")
        except Exception as fout:  # noqa: BLE001
            traceback.print_exc()
            toast = f"Dat ging mis: {str(fout)[:150]}"
        try:
            _vraag("answerCallbackQuery", {"callback_query_id": cq["id"], "text": toast[:190]})
        except TelegramFout:
            pass
        if uit and (uit.get("tekst") is not None or uit.get("knoppen") is not None):
            try:
                bewerk(bericht["message_id"], uit.get("tekst"), uit.get("knoppen"))
            except TelegramFout:
                pass
        return toast
    m = update.get("message")
    if m and van_mij(m) and m.get("reply_to_message") and m.get("text"):
        for functie in ANTWOORDEN:
            uit = functie(m["reply_to_message"]["message_id"], m["text"])
            if uit:
                try:
                    stuur(e(uit), antwoord_op=m["message_id"])
                except TelegramFout:
                    pass
                return uit
    return ""


def _lus():
    STATUS["draait"] = True
    offset = None
    while True:
        try:
            updates = _vraag("getUpdates", {"timeout": 50, "allowed_updates": ["callback_query", "message"],
                                            **({"offset": offset} if offset else {})}, timeout=70)
            STATUS.update(fout="", laatst=time.time())
            for u in updates or []:
                offset = u["update_id"] + 1
                verwerk(u)
        except TelegramFout as fout:
            STATUS["fout"] = ("Er staat een webhook op deze bot (bijvoorbeeld een Telegram-trigger in n8n), dus knoppen werken niet."
                              if fout.code == 409 else str(fout))
            time.sleep(300 if fout.code in (401, 404, 409) else 30)
        except Exception as fout:  # noqa: BLE001
            STATUS["fout"] = str(fout)[:200]
            time.sleep(30)


def start():
    """De draad die tikken ophaalt (alleen met bot, chat en planner aan)."""
    global _draad  # pylint: disable=global-statement
    if _draad or not actief() or not config.PLANNER_AAN:
        return
    _draad = threading.Thread(target=_lus, name="telegram-knoppen", daemon=True)
    _draad.start()
