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
        # Nachtelijk opruimen: oud en afgehandeld weg, recent en verwijderde meldingen (tegen terugkomen) blijven.
        c.execute("DELETE FROM routes")
        c.execute("INSERT INTO routes (sleutel, duur, bijgewerkt) VALUES ('oud', 5, now() - interval '40 days'), ('vers', 5, now())")
        c.execute("""INSERT INTO meldingen (niveau, bron, titel, sleutel, status, afgehandeld) VALUES
                     ('normaal', 'test', 'Oud opgelost', 'o1', 'opgelost', now() - interval '400 days'),
                     ('normaal', 'test', 'Oud verwijderd', 'o2', 'verwijderd', now() - interval '400 days')""")
        uitslag = main._opruimen(c)  # pylint: disable=protected-access
        assert "1 routes" in uitslag and "1 meldingen" in uitslag, uitslag
        assert [r["sleutel"] for r in c.execute("SELECT sleutel FROM routes")] == ["vers"]
        assert c.execute("SELECT 1 FROM meldingen WHERE sleutel = 'o2'").fetchone()
        assert "opruimen" in planner.TAKEN


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
        for pad in ["/", "/iris", "/iris?g=0", "/gezondheid", "/api/meldingen?na=0"]:
            assert k.get(pad, headers=KOP).status_code == 200, pad
        assert k.get("/gezond").text == "ok"
        # De knop Vernieuwen op Vandaag: haalt mail, agenda's en nieuws meteen op (hier nagebootst).
        from app.kern import planner  # pylint: disable=import-outside-toplevel
        gestart, oud = [], planner.nu_draaien
        planner.nu_draaien = lambda pool, naam: gestart.append(naam)
        try:
            assert 'action="/vandaag/vernieuwen"' in k.get("/", headers=KOP).text
            assert k.post("/vandaag/vernieuwen", headers=KOP).status_code == 403
            vorm = KOP | {"Origin": "https://buurmanitor.neighbour.nu"}
            r = k.post("/vandaag/vernieuwen", headers=vorm | {"X-Vandaag": "1"})
            assert r.json() == {"gestart": ["agenda", "post", "nieuws", "socials"]} and gestart == ["agenda", "post", "nieuws", "socials"]
            r = k.post("/vandaag/vernieuwen", headers=vorm, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/"
            stand = k.get("/api/vernieuwen?sinds=0", headers=KOP).json()["taken"]
            assert set(stand) == {"agenda", "post", "nieuws", "socials"} and set(stand.values()) <= {"klaar", "bezig", "wacht"}
        finally:
            planner.nu_draaien = oud
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
    """Meldingen op Vandaag afvinken, archiveren, verwijderen en terugzetten, met de mappen onder de lijst
    (binnen dezelfde app als test_schermen_starten). Het oude scherm Meldingen verwijst naar Vandaag."""
    form = KOP | {"Origin": "https://buurmanitor.neighbour.nu", "Referer": "https://buurmanitor.neighbour.nu/"}

    def lijst(pagina):
        return pagina.split('id="meldingen"')[1].split('class="meldingmappen"')[0]
    with _db() as c:
        c.execute("DELETE FROM meldingen")
        ids = [meldingen.melden(c, "belangrijk", "test", f"Melding {n}", sleutel=f"m{n}") for n in range(4)]
    pagina = k.get("/", headers=KOP).text
    assert all(f"Melding {n}" in lijst(pagina) for n in range(4)) and "✓ Opgelost" in pagina and "Alles gelezen" in pagina
    for m in ("Open", "Opgelost", "Archief", "Prullenbak"):
        assert f"<span>{m}</span>" in pagina
    assert 'href="/?meldingen=opgelost#meldingen"' in pagina and "<b>4</b>/4" in pagina
    assert ">Meldingen<" not in pagina.split('class="menu"')[1].split("</nav>")[0]  # geen eigen tabblad meer
    assert 'href="/meldingen" title="Alle meldingen beheren' in pagina
    # Afvinken, archiveren en verwijderen: weg uit Open, uit de banner, en terug op Vandaag.
    for mid, actie in zip(ids, ("opgelost", "archief", "verwijder")):
        r = k.post(f"/meldingen/{mid}/{actie}", headers=form, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "https://buurmanitor.neighbour.nu/"
    with _db() as c:
        assert [m["id"] for m in meldingen.open_meldingen(c)] == [ids[3]]
        assert [m["id"] for m in meldingen.nieuw_sinds(c, 0)] == [ids[3]]
        n = meldingen.aantallen(c)
        assert (n["open"], n["opgelost"], n["archief"], n["verwijderd"], n["ongelezen"]) == (1, 1, 1, 1, 1)
        assert meldingen.melden(c, "belangrijk", "test", "Melding 2 opnieuw", sleutel="m2") is None  # verwijderd blijft bewaard
    open_ = lijst(k.get("/", headers=KOP).text)
    assert "Melding 3" in open_ and "Melding 0" not in open_
    for status, titel in (("opgelost", "Melding 0"), ("archief", "Melding 1"), ("verwijderd", "Melding 2")):
        l_ = lijst(k.get(f"/?meldingen={status}", headers=KOP).text)
        assert titel in l_ and "Melding 3" not in l_, status
    assert "Terugzetten" in k.get("/?meldingen=verwijderd", headers=KOP).text
    assert "Terug naar Open" in k.get("/?meldingen=opgelost", headers=KOP).text
    # Terugzetten: weer in Open (al gelezen), niet opnieuw in de banner.
    r = k.post(f"/meldingen/{ids[2]}/terug", headers=form | {"Referer": "https://buurmanitor.neighbour.nu/?meldingen=verwijderd"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/?meldingen=verwijderd")
    with _db() as c:
        assert meldingen.aantallen(c)["open"] == 2 and [m["id"] for m in meldingen.nieuw_sinds(c, 0)] == [ids[3]]
    # Alles gelezen, onbekende actie of melding, of een formulier van een andere site.
    assert k.post("/meldingen/alles-gelezen", headers=form, follow_redirects=False).headers["location"] == "/#meldingen"
    assert k.post(f"/meldingen/{ids[3]}/weg", headers=form).status_code == 404
    assert k.post("/meldingen/999999/opgelost", headers=form).status_code == 404
    assert k.post(f"/meldingen/{ids[3]}/opgelost", headers=KOP | {"Origin": "https://kwaadwillend.nl"}).status_code == 403
    assert k.get("/?meldingen=onzin", headers=KOP).status_code == 200

    # Het beheerscherm: filteren, meerdere tegelijk, prullenbak legen, bronnen stil.
    with _db() as c:
        c.execute("DELETE FROM meldingen")
        ids = [meldingen.melden(c, niveau, bron, titel, tekst, sleutel=sl) for niveau, bron, titel, tekst, sl in (
            ("belangrijk", "clara", "Nieuwe uitnodiging", "Overleg Upper Spire", "u1"), ("normaal", "bram", "Mail van Jan", "", None),
            ("urgent", "gezondheid", "Schijf bijna vol", "nog 3 GB", "g1"), ("belangrijk", "clara", "Botsing morgen", "", "b1"))]
    beheer = k.get("/meldingen", headers=KOP).text
    assert all(t in beheer for t in ("Nieuwe uitnodiging", "Mail van Jan", "Schijf bijna vol", "Clara (agenda) (2)", "Server"))
    gefilterd = k.get("/meldingen?bron=clara&q=upper", headers=KOP).text
    assert "Nieuwe uitnodiging" in gefilterd and "Botsing morgen" not in gefilterd and "Mail van Jan" not in gefilterd
    assert "Schijf bijna vol" in k.get("/meldingen?niveau=urgent", headers=KOP).text
    assert "Schijf bijna vol" not in k.get("/meldingen?niveau=urgent&map=archief", headers=KOP).text
    r = k.post("/meldingen/bulk", headers=form, data={"actie": "archief", "id": [str(ids[0]), str(ids[1])]}, follow_redirects=False)
    assert r.status_code == 303 and "2+meldingen+gearchiveerd" in r.headers["location"]
    r = k.post("/meldingen/bulk", headers=form, data={"actie": "verwijder", "alle": "1", "bron": "clara"}, follow_redirects=False)
    assert "1+melding+naar+de+prullenbak" in r.headers["location"] and "bron=clara" in r.headers["location"]
    assert k.post("/meldingen/bulk", headers=form, data={"actie": "onzin"}).status_code == 400
    assert "Vink+eerst" in k.post("/meldingen/bulk", headers=form, data={"actie": "gelezen"}, follow_redirects=False).headers["location"]
    with _db() as c:
        n = meldingen.aantallen(c)
        assert (n["open"], n["archief"], n["verwijderd"]) == (1, 2, 1)
        c.execute("UPDATE meldingen SET status = 'verwijderd' WHERE id = %s", (ids[1],))  # zonder sleutel
    assert "Prullenbak legen" in k.get("/meldingen?map=verwijderd", headers=KOP).text
    k.post("/meldingen/legen", headers=form)
    with _db() as c:
        assert meldingen.aantallen(c)["verwijderd"] == 0
        assert meldingen.melden(c, "belangrijk", "clara", "Botsing morgen", sleutel="b1") is None  # komt niet terug
        assert c.execute("SELECT count(*) AS n FROM meldingen WHERE id = %s", (ids[1],)).fetchone()["n"] == 0
        assert [m["id"] for m in meldingen.nieuw_sinds(c, 0)] == [ids[2]]
    r = k.post("/meldingen/stil", headers=form, data={"stil_gezondheid": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/meldingen/instellingen?melding=Opgeslagen.#bronnen"
    assert 'name="stil_gezondheid"' in k.get("/meldingen/instellingen", headers=KOP).text
    with _db() as c:
        assert meldingen.stil(c) == {"gezondheid"} and meldingen.nieuw_sinds(c, 0) == []
        c.execute("DELETE FROM instellingen WHERE sleutel = 'meldingen_stil'")
        c.execute("DELETE FROM meldingen")

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


def test_datum_kort():
    nu = dt.datetime(2026, 11, 4, 15, 0, tzinfo=config.TZ)
    assert web.datum_kort(dt.datetime(2026, 11, 4, 9, 5, tzinfo=config.TZ), nu) == "09.05"
    assert web.datum_kort(dt.datetime(2026, 11, 2, 9, 5, tzinfo=config.TZ), nu) == "ma"
    assert web.datum_kort(dt.datetime(2026, 10, 20, 9, 5, tzinfo=config.TZ), nu) == "20-10"
    assert web.datum_kort(None) == ""


def test_vernieuw_stand():
    nu = dt.datetime(2026, 11, 1, 23, 0, tzinfo=dt.timezone.utc)
    rijen = [{"naam": "agenda", "laatst": nu + dt.timedelta(seconds=3), "bezig_sinds": None},
             {"naam": "post", "laatst": nu - dt.timedelta(minutes=4), "bezig_sinds": nu},
             {"naam": "nieuws", "laatst": nu - dt.timedelta(minutes=10), "bezig_sinds": None}]
    assert main.vernieuw_stand(rijen, nu, ["agenda", "post", "nieuws", "onbekend"]) == {
        "agenda": "klaar", "post": "bezig", "nieuws": "wacht", "onbekend": "wacht"}



def test_snelheid_meten():
    from app.kern import snelheid  # pylint: disable=import-outside-toplevel
    assert snelheid._soort("/bram/bericht/123/map") == "/bram/bericht/#/map"  # pylint: disable=protected-access
    snelheid.noteer("GET", "/bram/bericht/1", 1500)
    snelheid.noteer("GET", "/bram/bericht/2", 500)
    o = snelheid.overzicht()
    r = next(x for x in o["per_pad"] if x["pad"] == "GET /bram/bericht/#")
    assert r["aantal"] >= 2 and r["max"] >= 1500 and o["traag"][0]["pad"] == "/bram/bericht/1"


def test_planner_taak_niet_dubbel_tegelijk():
    import threading  # pylint: disable=import-outside-toplevel
    gestart, los = [], threading.Event()
    oud = planner.draai
    planner.draai = lambda pool, naam, forceer=False: gestart.append(naam) or los.wait(5)
    try:
        assert planner._in_thread(None, "x") is True  # pylint: disable=protected-access
        assert planner._in_thread(None, "x") is False  # pylint: disable=protected-access
        assert "x" in planner.bezig()
        los.set()
        for _ in range(50):
            if "x" not in planner.bezig():
                break
            threading.Event().wait(0.02)
        assert "x" not in planner.bezig() and gestart == ["x"]
    finally:
        planner.draai = oud
