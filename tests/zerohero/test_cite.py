"""ZEROHERO: bronvermeldingen als <cite index=...> komen nooit in de tekst (zonder database, zonder netwerk)."""
import os

os.environ.setdefault("ZEROHERO_DATABASE_URL", "postgresql://niemand@localhost/niets")

from app.zerohero import claude_api, opmaak  # noqa: E402

RUW = 'Volgens de bond <cite index="3-1,3-2">groeit het aantal keepers met 8 procent</cite>. <CITE index="5-1">Dat</cite> klopt.'


def test_zonder_cite():
    assert opmaak.zonder_cite(RUW) == "Volgens de bond groeit het aantal keepers met 8 procent. Dat klopt."
    assert opmaak.zonder_cite(None) == ""


def test_html_en_blokken_zonder_cite():
    h = opmaak.naar_html("## Kop\n\n" + RUW)
    assert "cite" not in h.lower() and "groeit het aantal keepers" in h
    assert "cite" not in str(opmaak.blokken(RUW)).lower()


def test_antwoord_van_claude_zonder_cite(monkeypatch):
    monkeypatch.setattr(claude_api, "actief", lambda: True)
    monkeypatch.setattr(claude_api, "_post", lambda body, timeout: {
        "content": [{"type": "text", "text": RUW, "citations": [{"url": "https://knvb.nl", "title": "KNVB"}]}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})
    tekst, _usage, bronnen = claude_api.gesprek("systeem", [{"role": "user", "content": "vraag"}], zoeken=3)
    assert "cite" not in tekst.lower() and bronnen == [{"titel": "KNVB", "url": "https://knvb.nl"}]
