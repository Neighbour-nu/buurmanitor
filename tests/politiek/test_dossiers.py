"""Test de zoektermen van een dossier.

Draaien: python -m tests.test_dossiers
"""
from app.politiek import dossiers


def test_termen():
    assert dossiers.termen("sportaccommodaties, afwegingskader\nRV 069; ab") == ["sportaccommodaties", "afwegingskader", "RV 069"]


def test_patronen_naam_als_terug():
    assert dossiers._patronen({"zoektermen": "", "naam": "Baroniehof"}) == ["%Baroniehof%"]


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
