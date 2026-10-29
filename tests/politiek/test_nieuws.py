"""Test de nieuwsradar zonder netwerk: feeds lezen, feed zoeken op een website, Google News-titels.

Draaien: python -m tests.test_nieuws
"""
import os

from app.politiek import nieuws

MAP = os.path.join(os.path.dirname(__file__), "fixtures")


def _bestand(naam):
    with open(os.path.join(MAP, naam), "rb") as f:
        return f.read()


def test_google_news():
    nieuws._haal = lambda url, maximum=0: (_bestand("google_news.xml"), "application/rss+xml", url)
    berichten = nieuws.lees_feed("https://news.google.com/rss/search?q=x", "google")
    assert len(berichten) == 4
    eerste = berichten[0]
    assert eerste["titel"] == "Raad Helmond stemt over sportaccommodaties: clubs vrezen hogere huur"
    assert eerste["bronnaam"] == "Eindhovens Dagblad"
    assert eerste["datum"].year == 2026 and "<" not in eerste["samenvatting"]


def test_zoek_feed():
    def nep(url, maximum=0):
        if url.endswith("/feed/"):
            return b"<?xml version='1.0'?><rss></rss>", "application/rss+xml", url
        return _bestand("site.html"), "text/html", url
    nieuws._haal = nep
    assert nieuws.zoek_feed("https://ditishelmond.nl") == "https://ditishelmond.nl/feed/"


def test_google_url():
    url = nieuws.feed_url({"soort": "google", "url": '"PRO Helmond"'})
    assert url.startswith(nieuws.GOOGLE) and "when%3A3d" in url


def test_sleutel_dubbel():
    a = nieuws.sleutel("Raad Helmond stemt over sportaccommodaties: clubs vrezen hogere huur")
    b = nieuws.sleutel("Raad Helmond stemt over sportaccommodaties; clubs vrezen hogere huur")
    assert a == b


if __name__ == "__main__":
    for naam, functie in list(globals().items()):
        if naam.startswith("test_"):
            functie()
            print("ok", naam)
