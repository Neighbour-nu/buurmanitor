"""Test het verwerken van een Notta-transcript en de agenda van de fractievergadering.

Draaien: python -m tests.test_fractie
"""
import datetime as dt

from app.politiek import fractie

SRT = """1
00:00:01,000 --> 00:00:05,000
Murat Yalcin: Welkom allemaal.

2
00:00:05,500 --> 00:00:09,000
Murat Yalcin: Eerst de mededelingen.

3
00:00:10,000 --> 00:00:15,000
Spreker 2: Ik heb een vraag.
"""


def test_srt_samengevoegd_per_spreker():
    t = fractie.srt_naar_tekst(SRT)
    assert t.split("\n") == ["Murat Yalcin: Welkom allemaal. Eerst de mededelingen.", "Spreker 2: Ik heb een vraag."]
    assert "-->" not in t


def test_bestandstype():
    assert fractie.tekst_uit_bestand("notta.txt", "Hallo".encode()) == "Hallo"
    try:
        fractie.tekst_uit_bestand("opname.m4a", b"x")
        raise AssertionError("geluid zou niet gelezen moeten worden")
    except ValueError:
        pass


def test_agenda_genummerd():
    t = fractie.agenda_tekst(dt.date(2026, 10, 26), {}, {"ingebracht": ["Openbare toiletten"]},
                             [("Raadsvergadering 27-10-2026", "", [])])
    assert "3. Ingebrachte agendapunten\n   1. Openbare toiletten" in t
    assert "4. Raadsvergadering 27-10-2026" in t and "\n\n" not in t


def test_actie_sleutel():
    assert fractie._actie_sleutel("Amendement rondsturen, aan CDA!") == "amendement rondsturen aan cda"


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
