"""Werkruimte ZEROHERO: proxygeheim en herkomstcontrole (zonder database, zonder netwerk).

Draaien in een eigen proces: python -m pytest tests/zerohero (zie tests/alles.sh).
"""
import os

os.environ.setdefault("ZEROHERO_DATABASE_URL", "postgresql://niemand@localhost/niets")  # main leest dit bij het importeren
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["TOEGESTANE_HOSTS"] = ""
os.environ["PORTAL_HOST"] = "buurmanitor.neighbour.nu"

from fastapi.testclient import TestClient  # noqa: E402

from app.zerohero import main  # noqa: E402

EIGEN = {"cockpit.zerohero.one", "buurmanitor.neighbour.nu"}


def test_herkomst_exact():
    ok = main.herkomst_toegestaan
    assert ok("https://cockpit.zerohero.one", None, EIGEN)
    assert ok(None, "https://cockpit.zerohero.one/inbox?status=nieuw", EIGEN)
    assert ok("https://buurmanitor.neighbour.nu", None, EIGEN)
    assert not ok("https://cockpit.zerohero.one.kwaadwillend.nl", None, EIGEN)
    assert not ok(None, "https://kwaadwillend.nl/?//cockpit.zerohero.one", EIGEN)
    assert not ok(None, None, EIGEN)
    assert not ok("null", None, EIGEN)
    assert not ok("http://cockpit.zerohero.one", None, EIGEN)


def test_portal_is_de_eigen_host():
    # In Buurmanitor komen formulieren van de portal; de oude cockpit stuurt na de overstap alleen door.
    assert main.TOEGESTANE_HOSTS == {"buurmanitor.neighbour.nu"}


def test_proxygeheim_verplicht():
    klant = TestClient(main.app)
    assert klant.get("/inbox", headers={"Remote-User": "martijn"}).status_code == 401
    assert klant.get("/inbox", headers={"Remote-User": "martijn", "X-Proxy-Geheim": "fout"}).status_code == 401
    assert klant.get("/inbox", headers={"X-Proxy-Geheim": "test-geheim"}).status_code == 401


def test_intern_en_gezond_blijven_bereikbaar_voor_n8n():
    klant = TestClient(main.app, raise_server_exceptions=False)
    # Zonder geheim komt n8n nog bij /intern; de eigen controle daar weigert zonder X-Doellijn-Secret.
    r = klant.post("/intern/mailerlite", json={})
    assert r.status_code != 401
    assert klant.get("/static/style.css").status_code == 200


def test_post_van_andere_site_geweigerd():
    klant = TestClient(main.app, raise_server_exceptions=False)
    kop = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim", "Origin": "https://kwaadwillend.nl"}
    assert klant.post("/items/status", headers=kop, data={"ids": "1", "status": "bewaard"}).status_code == 403
    assert klant.post("/links/1/weg", headers=kop).status_code == 403


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
