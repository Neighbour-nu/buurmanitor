"""Test het bijdragenatelier zonder Claude en zonder netwerk.

Draaien: python -m tests.test_schrijven
"""
from app.politiek import schrijven


def test_splits_markdown_en_json():
    antwoord = 'Voorzitter,\n\nDit is de tekst.\n\n```json\n{"titel": "Sport", "feiten": []}\n```'
    tekst, meta = schrijven._splits(antwoord)
    assert tekst.startswith("Voorzitter,") and meta["titel"] == "Sport"


def test_splits_alleen_json():
    tekst, meta = schrijven._splits('```json\n{"titel": "Vragen", "velden": {"onderwerp": "X"}}\n```')
    assert tekst == "" and meta["velden"]["onderwerp"] == "X"


def test_geen_gedachtestreepjes():
    assert "—" not in schrijven._geen_gedachtestreepjes("Sport — voor iedereen—altijd")


def test_vragen_genummerd_met_deelvragen():
    t = schrijven.velden_als_tekst("technische_vraag", {
        "onderwerp": "X", "vragen": ["## Bomen", "Hoeveel?", "Verplant?", "- Zo ja?", "## Overig", "Geluid?"]})
    h = schrijven.md_html(t)
    assert '<ol start="3">' in h and "<li>Verplant?\n<ul>" in h and "<h4>Bomen</h4>" in h


def test_word_uit_markdown():
    inhoud = schrijven.word_uit_markdown("# Kop\n\nVoorzitter,\n\n1. **Vraag** een\n- punt", "Titel")
    assert inhoud[:2] == b"PK"


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
