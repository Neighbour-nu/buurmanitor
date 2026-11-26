"""Telegram met knoppen: berichten (en foto's) sturen met knoppen eronder, en verwerken wat Martijn aantikt.

- Een Bot heeft een token en een chat. STANDAARD is de bot van Buurmanitor (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID);
  een onderdeel kan een eigen bot gebruiken (keepersnieuws: de ZEROHERO Doellijn-bot) en meldt die aan met registreer().
- Sturen: bot.stuur(tekst, knoppen) en bot.stuur_foto(png, bijschrift, knoppen); knoppen is een lijst rijen met
  (tekst, data). Begint data met https://, dan wordt het een linkknop.
- Tikken: per bot haalt een draad de updates op (long polling, getUpdates). De data van een knop begint met een
  voorvoegsel ('kn:12:p'); HANDLERS[voorvoegsel](data, bericht) doet het werk en geeft terug wat er in Telegram moet
  veranderen. Alleen tikken uit de eigen chat van die bot tellen.
- Antwoorden: een tekst als antwoord op een bericht van de bot gaat naar ANTWOORDEN (bijvoorbeeld een eigen reden).
- Staat er bij Telegram een webhook op een bot (bijvoorbeeld een Telegram-trigger in n8n), dan weigert Telegram
  getUpdates (409). Dan zet de draad bot.status['conflict'] en kan het onderdeel linkknoppen gebruiken in plaats van
  tikknoppen. De webhook van n8n blijft ongemoeid.
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


def e(t):
    return html.escape(str(t or ""), quote=False)


class TelegramFout(Exception):
    def __init__(self, tekst, code=None):
        super().__init__(tekst)
        self.code = code


def markup(knoppen):
    rijen = []
    for rij in knoppen or []:
        rijen.append([{"text": t, "url": d} if str(d).startswith("https://") else {"text": t, "callback_data": d[:64]} for t, d in rij])
    return {"inline_keyboard": rijen}


class Bot:
    def __init__(self, token, chat, naam):
        self.token, self.chat, self.naam = (token or "").strip(), str(chat or "").strip(), naam
        self.status = {"draait": False, "fout": "", "conflict": False, "laatst": None}
        self._draad = None

    def actief(self):
        return bool(self.token and self.chat)

    def _url(self, methode):
        return f"https://api.telegram.org/bot{self.token}/{methode}"

    def _vraag(self, methode, data=None, timeout=30):
        verzoek = urllib.request.Request(self._url(methode), data=json.dumps(data or {}).encode(), headers={"Content-Type": "application/json"})
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

    def stuur(self, tekst, knoppen=None, antwoord_op=None):
        """Een HTML-bericht (hooguit 4096 tekens). Geeft het bericht van Telegram terug."""
        data = {"chat_id": self.chat, "text": tekst[:4096], "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if knoppen:
            data["reply_markup"] = markup(knoppen)
        if antwoord_op:
            data["reply_parameters"] = {"message_id": antwoord_op, "allow_sending_without_reply": True}
        return self._vraag("sendMessage", data)

    def stuur_foto(self, png, bijschrift="", knoppen=None, bestandsnaam="beeld.png"):
        """Een foto met een HTML-bijschrift (hooguit 1024 tekens)."""
        grens = uuid.uuid4().hex
        delen = []

        def veld(naam, waarde):
            delen.append(f'--{grens}\r\nContent-Disposition: form-data; name="{naam}"\r\n\r\n{waarde}\r\n'.encode())
        veld("chat_id", self.chat)
        veld("caption", bijschrift[:1024])
        veld("parse_mode", "HTML")
        if knoppen:
            veld("reply_markup", json.dumps(markup(knoppen)))
        delen.append(f'--{grens}\r\nContent-Disposition: form-data; name="photo"; filename="{bestandsnaam}"\r\n'
                     f"Content-Type: image/png\r\n\r\n".encode() + png + b"\r\n")
        delen.append(f"--{grens}--\r\n".encode())
        verzoek = urllib.request.Request(self._url("sendPhoto"), data=b"".join(delen),
                                         headers={"Content-Type": f"multipart/form-data; boundary={grens}"})
        try:
            with urllib.request.urlopen(verzoek, timeout=60) as r:
                j = json.loads(r.read())
        except urllib.error.HTTPError as fout:
            raise TelegramFout(f"Telegram gaf fout {fout.code} bij de foto", fout.code) from None
        if not j.get("ok"):
            raise TelegramFout(j.get("description") or "Telegram weigerde de foto")
        return j["result"]

    def bewerk(self, bericht_id, tekst=None, knoppen=None):
        """Tekst en knoppen van een eerder bericht aanpassen (knoppen=[] haalt ze weg)."""
        if tekst is not None:
            data = {"chat_id": self.chat, "message_id": bericht_id, "text": tekst[:4096], "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True}, "reply_markup": markup(knoppen)}
            return self._vraag("editMessageText", data)
        return self._vraag("editMessageReplyMarkup", {"chat_id": self.chat, "message_id": bericht_id, "reply_markup": markup(knoppen)})

    # ------------------------------------------------------------ tikken verwerken

    def van_mij(self, bericht):
        return str(((bericht or {}).get("chat") or {}).get("id")) == self.chat

    def verwerk(self, update):
        """Eén update van Telegram. Geeft een korte uitslag (voor de tests)."""
        cq = update.get("callback_query")
        if cq:
            bericht = cq.get("message") or {}
            if not self.van_mij(bericht):
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
                self._vraag("answerCallbackQuery", {"callback_query_id": cq["id"], "text": toast[:190]})
            except TelegramFout:
                pass
            if uit and (uit.get("tekst") is not None or uit.get("knoppen") is not None):
                try:
                    self.bewerk(bericht["message_id"], uit.get("tekst"), uit.get("knoppen"))
                except TelegramFout:
                    pass
            return toast
        m = update.get("message")
        if m and self.van_mij(m) and m.get("reply_to_message") and m.get("text"):
            for functie in ANTWOORDEN:
                uit = functie(m["reply_to_message"]["message_id"], m["text"])
                if uit:
                    try:
                        self.stuur(e(uit), antwoord_op=m["message_id"])
                    except TelegramFout:
                        pass
                    return uit
        return ""

    def _lus(self):
        self.status["draait"] = True
        offset = None
        while True:
            try:
                updates = self._vraag("getUpdates", {"timeout": 50, "allowed_updates": ["callback_query", "message"],
                                                     **({"offset": offset} if offset else {})}, timeout=70)
                self.status.update(fout="", conflict=False, laatst=time.time())
                for u in updates or []:
                    offset = u["update_id"] + 1
                    self.verwerk(u)
            except TelegramFout as fout:
                self.status["conflict"] = fout.code == 409
                self.status["fout"] = ("Op deze bot staat een webhook (bijvoorbeeld een Telegram-trigger in n8n), dus Buurmanitor kan "
                                       "tikken niet ophalen. De knoppen in Telegram openen daarom de Doellijn." if fout.code == 409 else str(fout))
                time.sleep(300 if fout.code in (401, 404, 409) else 30)
            except Exception as fout:  # noqa: BLE001
                self.status["fout"] = str(fout)[:200]
                time.sleep(30)

    def start(self):
        if self._draad or not self.actief():
            return
        self._draad = threading.Thread(target=self._lus, name=f"telegram-{self.naam}", daemon=True)
        self._draad.start()


STANDAARD = Bot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, "buurmanitor")
BOTS = {}


def registreer(bot):
    """Een bot aanmelden zodat zijn tikken worden opgehaald (één draad per token)."""
    if bot.actief():
        BOTS.setdefault(bot.token, bot)
    return bot


def start():
    """De draden die tikken ophalen (alleen met planner aan)."""
    if not config.PLANNER_AAN:
        return
    for bot in BOTS.values():
        bot.start()
