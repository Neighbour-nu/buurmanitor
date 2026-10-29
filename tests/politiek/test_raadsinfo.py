"""Test het lezen van Notubiz-modules en vergaderstukken (zonder netwerk).

Draaien: python -m tests.test_raadsinfo
"""
import datetime as dt
import json
import os

from app.politiek import notubiz, raadsinfo

MAP = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(MAP, "module_voorbeeld.json"), encoding="utf-8") as f:
    VOORBEELD = json.load(f)


def test_rib():
    items = notubiz._lijst(VOORBEELD["m8"]["item"])
    s = raadsinfo.stuk_uit_item(notubiz.lees_item(items[0], 8))
    assert s["soort"] == "rib" and s["titel"] == "RIB 88 Uitbesteding Oekraïne locatiebeheer"
    assert s["nummer"] == "2024-088" and s["datum"] == dt.date(2024, 6, 25)
    assert s["document_url"] == "https://api.notubiz.nl/document/14402814/1" and s["document_id"] == 14402814
    assert s["extern_id"] == "m8-961233" and not s["pro"]


def test_vraag_los_item_en_fractie():
    item = notubiz._lijst(VOORBEELD["m4"]["item"])[0]  # een los item, geen lijst
    s = raadsinfo.stuk_uit_item(notubiz.lees_item(item, 4))
    assert s["soort"] == "vraag" and s["fracties"] == "Helder Helmond"
    assert "antwoord verwacht 2026-12-03" in s["context"] and not s["pro"]


def test_vraag_van_pro_beantwoord():
    item = notubiz._lijst(VOORBEELD["m4_pro"]["item"])[0]
    s = raadsinfo.stuk_uit_item(notubiz.lees_item(item, 4))
    assert s["pro"] and "beantwoord" in s["context"]
    assert s["document_url"].endswith("17600000/1")  # het antwoord, niet de vraag


def test_vraag_extra():
    open_ = raadsinfo.stuk_uit_item(notubiz.lees_item(VOORBEELD["m4"]["item"], 4))
    assert open_["extra"]["verwacht"] == "2026-12-03" and not open_["extra"]["beantwoord"]
    assert open_["extra"]["vraag_url"].endswith("17579971/1") and open_["extra"]["antwoord_url"] == ""
    item = notubiz._lijst(VOORBEELD["m4_pro"]["item"])[0]
    klaar = raadsinfo.stuk_uit_item(notubiz.lees_item(item, 4))
    assert klaar["extra"]["beantwoord"] and klaar["extra"]["antwoord_url"].endswith("17600000/1")
    assert klaar["extra"]["datum_antwoord"] == "2026-11-20" and klaar["extra"]["categorie"] == "Politieke vraag"


def test_vergaderstukken():
    verg = {"naam": "Raadsvergadering", "start": dt.datetime(2026, 10, 27, 19, 30), "agenda": [
        {"nummer": "5.", "onderwerp": "Benoemen burgercommissielid", "documenten": [
            {"id": 17562114, "titel": "RV 2026-092 Benoeming burgercommissielid Helder Helmond",
             "url": "https://api.notubiz.nl/document/17562114/1", "typen": ["Voorstel"]},
            {"id": 17562115, "titel": "Bijlage cv", "url": "https://api.notubiz.nl/document/17562115/1", "typen": []}]}]}
    stukken = raadsinfo.stukken_uit_vergadering(verg, "raad")
    assert [s["soort"] for s in stukken] == ["raadsvoorstel", "bijlage"]
    assert "agendapunt 5. Benoemen burgercommissielid" in stukken[0]["context"]


def test_besloten_bijeenkomst():
    ev = {"id": 1530001, "type": "meeting", "naam": "Peeltour inwerkprogramma klimaatadaptatie en water",
          "start": dt.datetime(2026, 10, 15, 19, 30), "plek": "Deurne", "url": "", "alleen_leden": True}
    s = raadsinfo.besloten_stuk(ev)
    assert s["soort"] == "besloten" and s["extern_id"] == "leden-1530001"
    assert s["document_url"] == "https://helmond.notubiz.nl/vergadering/1530001"
    assert "ingelogd in Notubiz" in s["context"] and "15-10-2026 19.30 uur, Deurne" in s["context"]


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
