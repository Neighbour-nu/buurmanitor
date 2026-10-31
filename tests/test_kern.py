"""Tests voor de kern van Buurmanitor (zonder netwerk en zonder Claude).

Draaien: pip install -r requirements.txt pytest httpx && python -m pytest tests
De databasetests draaien alleen als TEST_DATABASE_URL naar een lege, wegwerpbare PostgreSQL-database wijst.
"""
import datetime as dt
import gzip
import os
import tempfile
import time

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://niemand@localhost/niets"))
os.environ["PROXY_GEHEIM"] = "test-geheim"
os.environ["BUURMANITOR_PLANNER"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import main, personas  # noqa: E402
from app.kern import config, gezondheid, meldingen, planner, web  # noqa: E402
from app.schema import MIGRATIES  # noqa: E402

TZ = config.TZ
KOP = {"Remote-User": "martijn", "X-Proxy-Geheim": "test-geheim"}
DB = bool(os.environ.get("TEST_DATABASE_URL"))


def test_herkomst():
    ok = web.herkomst_toegestaan
    eigen = {"buurmanitor.neighbour.nu"}
    assert ok("https://buurmanitor.neighbour.nu", None, eigen)
    assert ok(None, "https://buurmanitor.neighbour.nu/iris?g=3", eigen)
    assert not ok("https://buurmanitor.neighbour.nu.kwaadwillend.nl", None, eigen)
    assert not ok(None, None, eigen) and not ok("null", None, eigen)
    assert not ok("http://buurmanitor.neighbour.nu", None, eigen)


def test_toegang_alleen_via_caddy():
    k = TestClient(main.app)
    assert k.get("/meldingen", headers={"Remote-User": "martijn"}).status_code == 401
    assert k.get("/meldingen", headers={"X-Proxy-Geheim": "test-geheim"}).status_code == 401
    assert k.get("/static/style.css").status_code == 200


def test_planner_periodiek():
    regel = {"elke": dt.timedelta(minutes=15), "om": None}
    nu = dt.datetime(2026, 10, 29, 12, 0, tzinfo=TZ)
    assert planner.aan_de_beurt(regel, nu, None)
    assert not planner.aan_de_beurt(regel, nu, nu - dt.timedelta(minutes=10))
    assert planner.aan_de_beurt(regel, nu, nu - dt.timedelta(minutes=15))


def test_planner_dagelijks():
    regel = {"elke": None, "om": dt.time(7, 30)}
    vroeg = dt.datetime(2026, 10, 29, 7, 0, tzinfo=TZ)
    later = dt.datetime(2026, 10, 29, 9, 0, tzinfo=TZ)
    assert not planner.aan_de_beurt(regel, vroeg, None)
    assert planner.aan_de_beurt(regel, later, None)
    assert planner.aan_de_beurt(regel, later, later - dt.timedelta(days=1))
    assert not planner.aan_de_beurt(regel, later, later.replace(hour=7, minute=31))


def test_backups_lezen():
    with tempfile.TemporaryDirectory() as basis:
        os.makedirs(os.path.join(basis, "promonitor"))
        os.makedirs(os.path.join(basis, "leeg"))
        pad = os.path.join(basis, "promonitor", "promonitor-2026-10-29.sql.gz")
        with gzip.open(pad, "wb") as f:
            f.write(b"-- dump")
        oud = os.path.join(basis, "promonitor", "promonitor-2026-10-25.sql.gz")
        with gzip.open(oud, "wb") as f:
            f.write(b"-- oud")
        os.utime(oud, (time.time() - 4 * 86400,) * 2)
        uit = {b["naam"]: b for b in gezondheid.backups(basis)}
        assert uit["promonitor"]["ok"] and uit["promonitor"]["aantal"] == 2
        assert not uit["leeg"]["ok"] and uit["leeg"]["aantal"] == 0


def test_extern_status():
    import json  # pylint: disable=import-outside-toplevel
    with tempfile.TemporaryDirectory() as basis:
        assert not gezondheid.extern(basis)["ingericht"]
        os.makedirs(os.path.join(basis, "buurmanitor"))
        pad = os.path.join(basis, "buurmanitor", "extern-status.json")
        nu = dt.datetime.now(TZ)
        with open(pad, "w", encoding="utf-8") as f:
            json.dump({"tijd": nu.isoformat(), "ok": True, "snapshot": "abc", "waarschuwingen": []}, f)
        assert gezondheid.extern(basis)["ok"]
        with open(pad, "w", encoding="utf-8") as f:
            json.dump({"tijd": (nu - dt.timedelta(days=3)).isoformat(), "ok": True, "waarschuwingen": []}, f)
        e = gezondheid.extern(basis)
        assert e["ingericht"] and not e["ok"]


def test_personas_compleet():
    alle = personas.alle()
    assert list(alle)[:6] == ["iris", "clara", "bram", "noor", "vera", "jesse"]
    tekst = personas.systeemtekst(alle["iris"])
    assert "Iris" in tekst and "gedachtestreepjes" in tekst and "—" not in tekst


def _db():
    import psycopg  # pylint: disable=import-outside-toplevel
    from psycopg.rows import dict_row  # pylint: disable=import-outside-toplevel
    return psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row)


def test_database_kern():
    if not DB:
        print("  (overgeslagen: geen TEST_DATABASE_URL)")
        return
    with _db() as c:
        for sql in MIGRATIES + MIGRATIES:
            c.execute(sql)
        c.execute("DELETE FROM meldingen")
        a = meldingen.melden(c, "belangrijk", "test", "Eerste", sleutel="dubbel")
        b = meldingen.melden(c, "belangrijk", "test", "Nog eens", sleutel="dubbel")
        assert a and b is None
        assert [m["titel"] for m in meldingen.nieuw_sinds(c, 0)] == ["Eerste"]


def test_planner_met_slot():
    if not DB:
        print("  (overgeslagen: geen TEST_DATABASE_URL)")
        return
    from psycopg_pool import ConnectionPool  # pylint: disable=import-outside-toplevel
    from psycopg.rows import dict_row  # pylint: disable=import-outside-toplevel
    pool = ConnectionPool(os.environ["TEST_DATABASE_URL"], min_size=1, max_size=2, open=True,
                          kwargs={"row_factory": dict_row, "autocommit": True})
    teller = []
    planner.TAKEN["test_taak"] = {"elke": dt.timedelta(hours=1), "om": None, "functie": lambda c: teller.append(1) or "gedaan"}
    try:
        with pool.connection() as c:
            c.execute("DELETE FROM taken WHERE naam = 'test_taak'")
        assert planner.draai(pool, "test_taak") == "gedaan"
        assert planner.draai(pool, "test_taak") is None  # binnen het uur: niet nog eens
        # Houdt een ander proces het slot vast, dan doet deze niets.
        with pool.connection() as ander:
            ander.execute("SELECT pg_advisory_lock(hashtext('buurmanitor:taak:test_taak'))")
            assert planner.draai(pool, "test_taak", forceer=True) is None
            ander.execute("SELECT pg_advisory_unlock(hashtext('buurmanitor:taak:test_taak'))")
        assert len(teller) == 1
    finally:
        planner.TAKEN.pop("test_taak", None)
        pool.close()


def test_schermen_starten():
    if not DB:
        print("  (overgeslagen: geen TEST_DATABASE_URL)")
        return
    with TestClient(main.app) as k:
        for pad in ["/", "/iris", "/iris?g=0", "/meldingen", "/gezondheid", "/api/meldingen?na=0"]:
            assert k.get(pad, headers=KOP).status_code == 200, pad
        assert k.get("/gezond").text == "ok"
        # Iris zonder sleutel: het gesprek wordt bewaard met een nette melding, geen fout.
        r = k.post("/iris", headers=KOP | {"Origin": "https://buurmanitor.neighbour.nu"}, data={"vraag": "Hoi Iris"},
                   follow_redirects=False)
        assert r.status_code == 303
        pagina = k.get(r.headers["location"].split("#")[0], headers=KOP).text
        assert "Hoi Iris" in pagina and "Claude-sleutel" in pagina
        # Formulier van een andere site: geweigerd.
        assert k.post("/meldingen/alles-gelezen", headers=KOP | {"Origin": "https://kwaadwillend.nl"}).status_code == 403
        _meldingen_afvinken_archiveren_verwijderen(k)


def _meldingen_afvinken_archiveren_verwijderen(k):
    """Meldingen afvinken, archiveren, verwijderen en terugzetten (binnen dezelfde app als test_schermen_starten)."""
    form = KOP | {"Origin": "https://buurmanitor.neighbour.nu", "Referer": "https://buurmanitor.neighbour.nu/meldingen"}
    with _db() as c:
        c.execute("DELETE FROM meldingen")
        ids = [meldingen.melden(c, "belangrijk", "test", f"Melding {n}", sleutel=f"m{n}") for n in range(4)]
    pagina = k.get("/meldingen", headers=KOP).text
    assert all(f"Melding {n}" in pagina for n in range(4)) and "Gelezen" in pagina and "✓ Opgelost" in pagina
    for m in ("Open", "Opgelost", "Archief", "Prullenbak"):
        assert f"<b>{m}</b>" in pagina
    assert 'href="/meldingen?map=opgelost"' in pagina and "4 nieuw" in pagina
    # Afvinken, archiveren en verwijderen: weg uit Open, uit de banner en uit het dashboard.
    for mid, actie in zip(ids, ("opgelost", "archief", "verwijder")):
        r = k.post(f"/meldingen/{mid}/{actie}", headers=form, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/meldingen")
    with _db() as c:
        assert [m["id"] for m in meldingen.open_meldingen(c)] == [ids[3]]
        assert [m["id"] for m in meldingen.nieuw_sinds(c, 0)] == [ids[3]]
        n = meldingen.aantallen(c)
        assert (n["open"], n["opgelost"], n["archief"], n["verwijderd"], n["ongelezen"]) == (1, 1, 1, 1, 1)
        # Verwijderd blijft bewaard: dezelfde sleutel komt niet opnieuw.
        assert meldingen.melden(c, "belangrijk", "test", "Melding 2 opnieuw", sleutel="m2") is None
    open_ = k.get("/meldingen", headers=KOP).text.split('id="lijst"')[1].split("<aside")[0]
    assert "Melding 3" in open_ and "Melding 0" not in open_
    for status, titel in (("opgelost", "Melding 0"), ("archief", "Melding 1"), ("verwijderd", "Melding 2")):
        lijst = k.get(f"/meldingen?map={status}", headers=KOP).text.split('id="lijst"')[1].split("<aside")[0]
        assert titel in lijst and "Melding 3" not in lijst, status
    assert "Terugzetten" in k.get("/meldingen?map=verwijderd", headers=KOP).text
    # Terugzetten: weer in Open (al gelezen), niet opnieuw in de banner.
    assert k.post(f"/meldingen/{ids[2]}/terug", headers=form, follow_redirects=False).status_code == 303
    with _db() as c:
        assert meldingen.aantallen(c)["open"] == 2 and [m["id"] for m in meldingen.nieuw_sinds(c, 0)] == [ids[3]]
    # Onbekende actie of melding, of een formulier van een andere site: geweigerd.
    assert k.post(f"/meldingen/{ids[3]}/weg", headers=form).status_code == 404
    assert k.post("/meldingen/999999/opgelost", headers=form).status_code == 404
    assert k.post(f"/meldingen/{ids[3]}/opgelost", headers=KOP | {"Origin": "https://kwaadwillend.nl"}).status_code == 403
    assert k.get("/meldingen?map=onzin", headers=KOP).status_code == 200


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)


def test_voorvoegsel_herschrijft_html():
    from app.kern.prefix import herschrijf_html, herschrijf_pad
    h = lambda t: herschrijf_html(t, "/zerohero")  # noqa: E731
    assert h('<a href="/inbox">') == '<a href="/zerohero/inbox">'
    assert h('<form action="/items/status">') == '<form action="/zerohero/items/status">'
    assert h('<button hx-post="/concepten/3/weg" hx-get="/x">') == '<button hx-post="/zerohero/concepten/3/weg" hx-get="/zerohero/x">'
    assert h("""onchange="location='/dashboard/cijfers?d='+v" """) == """onchange="location='/zerohero/dashboard/cijfers?d='+v" """
    assert h("location.href = '/a'; fetch('/links', {})") == "location.href = '/zerohero/a'; fetch('/zerohero/links', {})"
    # Ongemoeid: al met voorvoegsel, protocol-relatief, volledige adressen, ankers.
    for t in ['<a href="/zerohero/x">', '<a href="/zerohero">', '<a href="//cdn.x/y">', '<a href="https://x.nl/">', '<a href="#top">']:
        assert h(t) == t
    assert herschrijf_pad("/filter#regels", "/zerohero") == "/zerohero/filter#regels"
    assert herschrijf_pad("/zerohero?x=1", "/zerohero") == "/zerohero?x=1"
    assert herschrijf_pad("https://x.nl/a", "/zerohero") == "https://x.nl/a"


def test_doorgeven_stuurt_alleen_intern_door():
    import asyncio
    from app.kern.prefix import Doorgeven
    gezien = []

    def maak(naam):
        async def asgi(scope, receive, send):
            gezien.append((naam, scope["path"]))
        return asgi
    d = Doorgeven(maak("portal"), "/intern/", maak("zerohero"))
    for pad in ["/intern/weging/leerpunten", "/internet", "/", "/zerohero/intern/x"]:
        asyncio.run(d({"type": "http", "path": pad}, None, None))
    assert gezien == [("zerohero", "/intern/weging/leerpunten"), ("portal", "/internet"), ("portal", "/"),
                      ("portal", "/zerohero/intern/x")]
