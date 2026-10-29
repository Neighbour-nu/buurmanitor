"""Tests voor de herkomstcontrole, het proxygeheim, vertrouwelijke stukken en het schema op een lege database.

Draaien: python -m tests.test_beveiliging
De databasetests draaien alleen als TEST_POLITIEK_DATABASE_URL naar een lege, wegwerpbare PostgreSQL-database wijst.
"""
import os

os.environ.setdefault("POLITIEK_DATABASE_URL", "postgresql://niemand@localhost/niets")  # main leest dit bij het importeren
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["TOEGESTANE_HOSTS"] = "buurmanitor.neighbour.nu"

from fastapi.testclient import TestClient  # noqa: E402

from app.politiek import kennisbank, main  # noqa: E402
from app.politiek.schema import MIGRATIES  # noqa: E402

EIGEN = {"promonitor.neighbour.nu", "buurmanitor.neighbour.nu"}


def test_herkomst_exact():
    ok = main.herkomst_toegestaan
    assert ok("https://promonitor.neighbour.nu", None, EIGEN)
    assert ok(None, "https://promonitor.neighbour.nu/agenda?week=3", EIGEN)
    assert ok("https://buurmanitor.neighbour.nu", None, EIGEN)
    # Vroeger kwamen deze erdoor omdat alleen naar een stukje tekst werd gekeken.
    assert not ok("https://promonitor.neighbour.nu.kwaadwillend.nl", None, EIGEN)
    assert not ok(None, "https://kwaadwillend.nl/?//promonitor.neighbour.nu", EIGEN)
    # Geen herkomst, of herkomst "null", wordt geweigerd.
    assert not ok(None, None, EIGEN)
    assert not ok("null", None, EIGEN)
    # Alleen https.
    assert not ok("http://promonitor.neighbour.nu", None, EIGEN)


def test_extra_host_uit_omgeving():
    assert "buurmanitor.neighbour.nu" in main.TOEGESTANE_HOSTS


def test_proxygeheim_verplicht():
    klant = TestClient(main.app)
    zonder = klant.get("/vragen", headers={"Remote-User": "martijn"})
    assert zonder.status_code == 401 and "proxy" in zonder.text
    fout = klant.get("/vragen", headers={"Remote-User": "martijn", "X-Proxy-Geheim": "verkeerd"})
    assert fout.status_code == 401
    # Met het juiste geheim maar zonder Remote-User: nog steeds niet ingelogd.
    anoniem = klant.get("/vragen", headers={"X-Proxy-Geheim": "test-geheim"})
    assert anoniem.status_code == 401 and "ingelogd" in anoniem.text


def test_open_paden_blijven_open():
    klant = TestClient(main.app)
    assert klant.get("/static/style.css").status_code == 200


def test_post_van_andere_site_geweigerd():
    klant = TestClient(main.app)
    kop = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim", "Origin": "https://kwaadwillend.nl"}
    assert klant.post("/inbox/alles-gezien", headers=kop).status_code == 403


def _db():
    import psycopg  # pylint: disable=import-outside-toplevel
    from psycopg.rows import dict_row  # pylint: disable=import-outside-toplevel
    return psycopg.connect(os.environ["TEST_POLITIEK_DATABASE_URL"], autocommit=True, row_factory=dict_row)


def test_schema_op_lege_database():
    if not os.environ.get("TEST_POLITIEK_DATABASE_URL"):
        print("  (overgeslagen: geen TEST_POLITIEK_DATABASE_URL)")
        return
    with _db() as c:
        for sql in MIGRATIES + MIGRATIES:  # twee keer: moet ook bij een herstart goed gaan
            c.execute(sql)


def test_vertrouwelijk_standaard_buiten_claude():
    if not os.environ.get("TEST_POLITIEK_DATABASE_URL"):
        print("  (overgeslagen: geen TEST_POLITIEK_DATABASE_URL)")
        return
    with _db() as c:
        for sql in MIGRATIES:
            c.execute(sql)
        c.execute("DELETE FROM documenten")
        c.execute("""INSERT INTO documenten (titel, soort, pad, tekst, vertrouwelijk) VALUES
            ('Openbaar stuk', 'raadsstuk', 'test/openbaar', 'Het zwembad krijgt een nieuwe exploitant in Helmond-Noord.', false),
            ('Fractiestrategie', 'notulen', 'test/geheim', 'Strategie over het zwembad: we wachten tot na de begroting.', true)""")
        q = lambda sql, p=None: c.execute(sql, p).fetchall()  # noqa: E731
        standaard = kennisbank.context(q, "zwembad exploitant strategie")
        assert "Openbaar stuk" in standaard and "Fractiestrategie" not in standaard
        assert kennisbank.VERTROUWELIJK_REGEL not in standaard
        met = kennisbank.context(q, "zwembad exploitant strategie", vertrouwelijk=True)
        assert "Fractiestrategie" in met and kennisbank.VERTROUWELIJK_REGEL in met


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
