"""De indeling van een commissieavond uit de PDF van de fractie (woordvoerders per agendapunt).

De fixture is de echte PDF van 10-11-2026, met de namen erin gezet met Invullen en ondertekenen (Acrobat);
het logo en de lettertypen zijn eruit gehaald om hem klein te houden.
"""
import datetime as dt
import os

from app.politiek import agenda, indeling, whaller

PDF = os.path.join(os.path.dirname(__file__), "fixtures", "indeling_commissie_10-11-2026.pdf")
PERSONEN = [{"naam": n, "whaller": w, "rol": "raadslid"} for n, w in (
    ("Niek Engbers", "n.engbers"), ("Petra Verdonk", "PetraVerdonk"), ("Jeanette Hendriks", "JeanetteHendriks"),
    ("Murat Yalcin", "murat_yalcin"), ("Martijn Buurman", "martijn_buurman"), ("Stijn van Hoek", "StijnvanHoek"))]


def _pdf():
    with open(PDF, "rb") as f:
        return f.read()


def test_pdf_lezen():
    r = indeling.lees_pdf(_pdf())
    assert r["datum"] == dt.date(2026, 11, 10)
    assert [(p["commissie"], p["nummer"], p["namen"]) for p in r["punten"]] == [
        ("Commissie A", "4", ["Niek", "Petra"]), ("Commissie A", "5", ["Jeanette", "Petra"]), ("Commissie A", "6", ["Niek", "Murat"]),
        ("Commissie B", "4", ["Martijn", "Stijn"]), ("Commissie B", "5", ["Stijn", "Bert"]), ("Commissie B", "6", ["Stijn", "Bert"])]
    assert r["punten"][0]["onderwerp"] == "Afsluiten grondexploitatie Cacaofabriek e.o. - (19.05-19.50 uur)"
    assert r["opmerkingen"] == ["Sanae Voorzitter", "Deliana met vakantie"]  # hoort bij geen agendapunt


def test_namen_in_de_tekst_zelf():
    """Als de namen gewoon achter het agendapunt zijn getypt (of als los stuk tekst op dezelfde regel)."""
    paginas = [[(56, 700, "Commissie B", "pagina"), (57, 680, "Datum 17-11-2026", "pagina"),
                (57, 600, "4. Bestedingsplan Wonen - (19.05-19.50 uur) Martijn en Stijn", "pagina"),
                (57, 560, "5.", "pagina"), (94, 560, "Aanvraag krediet politiekantoor - (20.00-20.45 uur)", "pagina"),
                (400, 559, "Stijn & Murat", "pagina"),
                (57, 520, "6. Sluiting", "pagina"), (57, 480, "7. Opinierend stuk zonder namen", "pagina")]]
    r = indeling.lees(paginas, voornamen=["Martijn", "Stijn"])
    assert r["datum"] == dt.date(2026, 11, 17)
    assert [(p["onderwerp"], p["namen"]) for p in r["punten"]] == [
        ("Bestedingsplan Wonen - (19.05-19.50 uur)", ["Martijn", "Stijn"]),
        ("Aanvraag krediet politiekantoor - (20.00-20.45 uur)", ["Stijn", "Murat"])]
    assert indeling.splits_namen("Niek, Petra en Murat") == ["Niek", "Petra", "Murat"]


def test_koppelen_aan_de_agenda():
    pdf = indeling.lees_pdf(_pdf())
    punten = [{"onderwerp": o, "woordvoerder": "", "zit_erbij": "", "bron": ""} for o in (
        "Afsluiten grondexploitatie Cacaofabriek e.o.", "Aanpak evenementen locatieprofielen Helmond Centrum",
        "GREX Nieuw Brandevoort", "Bestedingsplan Wonen", "Aanvraag krediet t.b.v. aankoop voormalige politiekantoor",
        "Vaststelling Omgevingsplanwijziging Burgemeester Krollaan 101", "Iets wat niet in de PDF staat")]
    over = indeling.koppel(punten, pdf, PERSONEN, agenda.persoon_bij_naam)
    assert over == []
    assert [(p["pdf"]["woordvoerder"], p["pdf"]["zit_erbij"]) for p in punten[:6]] == [
        ("n.engbers", "PetraVerdonk"), ("JeanetteHendriks", "PetraVerdonk"), ("n.engbers", "murat_yalcin"),
        ("martijn_buurman", "StijnvanHoek"), ("StijnvanHoek", ""), ("StijnvanHoek", "")]
    assert punten[4]["pdf"]["onbekend"] == ["Bert"] and "pdf" not in punten[6]
    # Wat niet op de agenda staat, komt terug (bijvoorbeeld een PDF van een andere avond).
    over = indeling.koppel([{"onderwerp": "Iets anders", "woordvoerder": "", "zit_erbij": "", "bron": ""}], pdf, PERSONEN,
                           agenda.persoon_bij_naam)
    assert len(over) == 6 and whaller.sleutel(over[0]["onderwerp"]) == "afsluiten grondexploitatie cacaofabriek e o"
