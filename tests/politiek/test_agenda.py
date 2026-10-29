"""Test de deadlines en het stappenplan van de Agenda zonder netwerk.

Draaien: python -m tests.test_agenda
"""
import datetime as dt

from app.politiek import agenda


def test_deadlines_commissie():
    dinsdag = dt.date(2026, 11, 3)
    d = agenda.deadlines("commissie", dinsdag, {"agenda_technische_vragen_dagen": "7", "fractie_dag": "maandag"})
    assert d["technisch"] == dt.date(2026, 10, 27)
    assert d["standpunt"] == dt.date(2026, 11, 2)   # maandag ervoor
    assert d["bijdrage"] == dt.date(2026, 11, 1)    # zondag ervoor


def test_deadlines_raad_zonder_technische_vragen():
    d = agenda.deadlines("raad", dt.date(2026, 10, 27), {})
    assert "technisch" not in d and "motie" not in d and d["standpunt"] == dt.date(2026, 10, 26)


def test_technische_vragen_uit():
    assert "technisch" not in agenda.deadlines("commissie", dt.date(2026, 11, 3), {"agenda_technische_vragen_dagen": "0"})


def test_dag_voor_zelfde_dag():
    # Een vergadering op maandag: het standpunt moet de maandag ervoor, niet dezelfde dag.
    assert agenda._dag_voor(dt.date(2026, 11, 2), "maandag") == dt.date(2026, 10, 26)


def test_formele_punten():
    assert agenda.is_formeel("Benoemen van de heer D. Eindhoven als burgercommissielid namens Helder Helmond")
    assert agenda.is_formeel("Beëdiging en toelating nieuw raadslid")
    assert not agenda.is_formeel("Afwegingskader sportaccommodaties")


PERSONEN = [{"naam": "Martijn Buurman", "whaller": "martijn_buurman", "rol": "raadslid", "portefeuilles": "digitalisering, pleegzorg, mantelzorg"},
            {"naam": "Bert van Sas", "whaller": "bert_van_sas", "rol": "raadslid", "portefeuilles": "milieu, luchtkwaliteit"},
            {"naam": "Stijn van Hoek", "whaller": "StijnvanHoek", "rol": "burgercommissielid", "portefeuilles": "sport"}]


def test_voorstel_dossier_en_portefeuille():
    dossiers = [{"naam": "Sportaccommodaties", "zoektermen": "afwegingskader, deprivatisering", "woordvoerder": "Martijn Buurman"}]
    assert agenda.voorstel("Afwegingskader sportaccommodaties", PERSONEN, dossiers) == ("martijn_buurman", "dossier Sportaccommodaties")
    assert agenda.voorstel("Visie op digitale dienstverlening", PERSONEN, []) == ("martijn_buurman", "portefeuille digitalisering")
    # In de raad alleen raadsleden: Stijn valt af.
    assert agenda.voorstel("Sportpark Brandevoort", PERSONEN, [], alleen={"martijn_buurman", "bert_van_sas"}) == ("", "")


def test_persoon_bij_naam():
    assert agenda.persoon_bij_naam("Bert", PERSONEN)["whaller"] == "bert_van_sas"
    assert agenda.persoon_bij_naam("Martijn Buurman", PERSONEN)["whaller"] == "martijn_buurman"
    assert agenda.persoon_bij_naam("Onbekend", PERSONEN) is None


def test_zoek_punt():
    verg = [{"punten": [
        {"soort": "commissie", "onderwerp": "Afwegingskader sportaccommodaties", "sleutel": "afwegingskader sportaccommodaties"},
        {"soort": "raad", "rubriek": "bespreek", "onderwerp": "Afwegingskader sportaccommodaties", "sleutel": "afwegingskader sportaccommodaties"},
        {"soort": "raad", "rubriek": "bespreek", "onderwerp": "Participatieverordening", "sleutel": "participatieverordening"}]}]
    assert agenda.zoek_punt("Afwegingskader sportaccommodaties", "Raad 27-10-2026", verg)["soort"] == "raad"
    assert agenda.zoek_punt("Afwegingskader sport-accommodaties", "Commissie", verg)["soort"] == "commissie"
    assert agenda.zoek_punt("Begroting 2027", "Raad", verg) is None


def test_mijn_deadlines():
    p = {"woordvoerder": "martijn_buurman", "zit_erbij": "", "datum": dt.date(2026, 11, 3), "open": ["technisch", "bijdrage"],
         "deadlines": {"technisch": dt.date(2026, 10, 27), "bijdrage": dt.date(2026, 11, 1)}}
    uit = agenda.mijn_deadlines([{"punten": [p]}], "martijn_buurman", dt.date(2026, 10, 26), dagen=1)
    assert [x["stap"] for x in uit] == ["technisch"]
    assert not agenda.mijn_deadlines([{"punten": [p]}], "n.engbers", dt.date(2026, 10, 26), dagen=7)


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
