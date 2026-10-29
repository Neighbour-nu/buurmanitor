"""Test de Whaller-berichten met echte Notubiz-gegevens (raad 27-10-2026, commissieavond 6-10-2026).

Draaien: python -m tests.test_whaller   (of: pytest tests)
"""
import datetime as dt
import json
import os

from app.politiek import notubiz, whaller

MAP = os.path.join(os.path.dirname(__file__), "fixtures")


def _nep_haal(pad, **_):
    soort, nummer = pad.rsplit("/", 1)
    naam = {"events/meetings": "meeting", "events/assemblies": "assembly"}[soort]
    bestand = f"raad_{nummer}.json" if nummer == "1434686" else f"{naam}_{nummer}.json"
    with open(os.path.join(MAP, bestand), encoding="utf-8") as f:
        return json.load(f)


notubiz._haal = _nep_haal

RAADSLEDEN = {"murat_yalcin", "martijn_buurman", "bert_van_sas", "JeanetteHendriks", "n.engbers", "SanaeYahia"}
INST = {"raad_dag": "dinsdag", "rood_map_commissie": "https://w/commissie", "rood_map_raad": "https://w/raad",
        "groen_map_notulen": "https://w/notulen", "groen_map_agenda": "https://w/agenda",
        "groen_formulier": "https://w/form", "fractie_tijd": "19.00", "fractie_dag": "maandag"}


def tw(onderwerp, soort, datum, wv, erbij=""):
    return {"sleutel": whaller.sleutel(onderwerp), "onderwerp": onderwerp, "soort": soort,
            "datum": dt.date.fromisoformat(datum), "woordvoerder": wv, "zit_erbij": erbij}


TOEWIJZINGEN = [
    tw("Afwegingskader sportaccommodaties", "commissie", "2026-10-06", "martijn_buurman", "murat_yalcin"),
    tw("Fusie onderwijsgroep Oost Brabant (OGOB)", "commissie", "2026-10-06", "n.engbers", "PetraVerdonk"),
    tw("Participatieverordening", "commissie", "2026-10-06", "StijnvanHoek", "bert_van_sas"),
    tw("Vaststellen Assetsheets 2027-2031", "commissie", "2026-10-06", "JeanetteHendriks", "Deliana"),
    tw("Bouwplan Baroniehof 75 appartementen", "commissie", "2026-10-06", "martijn_buurman", "StijnvanHoek"),
    tw("Grondexploitatie Woonzorg zone Helmond Oost (Generaal Snijderstraat)", "commissie", "2026-10-13",
       "StijnvanHoek", "martijn_buurman"),
]


def test_tijd_splitsen():
    assert notubiz.splits_tijd("Afwegingskader sportaccommodaties - (19.35-20.50 uur)") == \
        ("Afwegingskader sportaccommodaties", "19.35-20.50")
    assert notubiz.splits_tijd("Vaststellen Assetsheets 2027-2031 (21.00-21.30)") == \
        ("Vaststellen Assetsheets 2027-2031", "21.00-21.30")
    assert notubiz.splits_tijd("Participatieverordening") == ("Participatieverordening", "")


def test_commissie():
    avond = notubiz.commissieavond(1523603)
    assert [c["naam"] for c in avond["commissies"]] == ["Commissie A", "Commissie B"]
    punten = whaller.commissie_punten(avond, TOEWIJZINGEN)
    assert [p["onderwerp"] for p in punten] == [
        "Afwegingskader sportaccommodaties", "Fusie onderwijsgroep Oost Brabant (OGOB)",
        "Participatieverordening", "Vaststellen Assetsheets 2027-2031", "Bouwplan Baroniehof 75 appartementen"]
    assert punten[0]["portefeuillehouder"] == "wethouder Rieter"
    tekst = whaller.commissie_bericht(avond, punten, INST)
    assert "Commissievergadering 6-10-2026" in tekst
    assert "<strong>Commissie A (Raadzaal)</strong>" in tekst and "<strong>Commissie B (Commissiekamer)</strong>" in tekst
    assert "Afwegingskader sportaccommodaties - (19.35-20.50 uur) | @martijn_buurman &amp; @murat_yalcin" in tekst
    assert "Vaststellen Assetsheets 2027-2031 - (21.00-21.30 uur) | @JeanetteHendriks &amp; @Deliana" in tekst
    assert "Let op" not in tekst  # dinsdag
    assert "10 minuten" not in tekst and "Sluiting" not in tekst and "GGD" not in tekst


def test_raad():
    verg = notubiz.vergadering(1434686)
    indeling = whaller.raad_indeling(verg, TOEWIJZINGEN, RAADSLEDEN)
    assert [i["onderwerp"] for i in indeling["bijzonder"]] == [
        "Benoemen van de heer D. Eindhoven als burgercommissielid namens Helder Helmond"]
    assert len(indeling["hamer"]) == 8
    namen = {i["onderwerp"]: i["woordvoerder"] for i in indeling["bespreek"]}
    assert namen == {
        "Afwegingskader sportaccommodaties": "martijn_buurman",
        "Fusie onderwijsgroep Oost Brabant (OGOB)": "n.engbers",
        "Participatieverordening": "bert_van_sas",  # Stijn is geen raadslid: de tweede naam
        "Grondexploitatie Woonzorg zone Helmond Oost (Generaal Snijderstraat)": "martijn_buurman",
    }
    vooraf = [{"naam": "Hooravond begroting", "plek": "Raadzaal Huis voor de Stad",
               "start": dt.datetime(2026, 10, 27, 18, 30), "soort": "overig"}]
    tekst = whaller.raad_bericht(verg, indeling, INST, {"bijzonder": ["Amendement Betaalbaar sporten (@martijn_buurman)"]},
                                 whaller.raad_vooraf(vooraf, verg))
    assert "Raadsvergadering 27-10-2026" in tekst
    assert "Vooraf om 18.30 uur: hooravond begroting in de Raadzaal Huis voor de Stad." in tekst
    assert "Moties vreemd aan de orde van de dag</strong>" in tekst
    assert "Participatieverordening (@bert_van_sas)" in tekst
    assert "Amendement Betaalbaar sporten" in tekst


def test_woensdag_waarschuwing():
    verg = notubiz.vergadering(1434686)
    verg["start"] = dt.datetime(2026, 9, 23, 19, 30)
    tekst = whaller.raad_bericht(verg, whaller.raad_indeling(verg, [], RAADSLEDEN), INST, {})
    assert "Let op, dit is een woensdag!" in tekst


def test_fractie():
    datum = whaller.fractie_datum(dt.date(2026, 10, 26), INST)
    assert datum == dt.date(2026, 10, 26)
    tekst = whaller.fractie_bericht(datum, INST, {"tijd": "19.30", "mededelingen": ["Peter schuift aan"],
                                                  "ingebracht": []},
                                    [("Raadsvergadering 27-10-2026", "https://w/bericht", ["Amendement"])])
    assert "#fractievergadering</strong></span>&nbsp;26-10-2026" in tekst
    assert "we beginnen om 19.30 uur" in tekst
    assert '<a href="https://w/bericht">Raadsvergadering 27-10-2026</a><ol><li>Amendement</li></ol>' in tekst
    assert "Kaartjes te versturen deze week" in tekst
    zonder = whaller.fractie_bericht(datum, INST, {"tijd": "19.00"}, [])
    assert "Let op" not in zonder


def test_checklist():
    regels = whaller.checklist("fractie", dt.date(2026, 10, 26), INST, {"tijd": "19.30"})
    assert any("van 19.30 tot 21.30 uur" in r for r in regels)


if __name__ == "__main__":
    for naam, f in list(globals().items()):
        if naam.startswith("test_"):
            f()
            print("ok", naam)
