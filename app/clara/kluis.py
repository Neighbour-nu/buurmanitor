"""De kluis: tokens en geheime agendalinks staan versleuteld in de database.

De sleutel (KLUIS_SLEUTEL) staat alleen in ~/buurmanitor/.env, niet in de database en dus ook niet in de back-ups
van de database. Raakt de sleutel kwijt, dan koppel je de agenda's opnieuw; er gaan geen afspraken verloren, want
die staan in Google en Outlook zelf.
"""
from cryptography.fernet import Fernet, InvalidToken

from ..kern import config


class KluisFout(Exception):
    pass


def _fernet():
    if not config.KLUIS_SLEUTEL:
        raise KluisFout("Er is nog geen kluissleutel. Draai op de server: bash ~/buurmanitor/app/deploy/clara.sh")
    try:
        return Fernet(config.KLUIS_SLEUTEL.encode())
    except ValueError as e:
        raise KluisFout("De kluissleutel heeft een ongeldige vorm. Draai clara.sh opnieuw.") from e


def actief():
    return bool(config.KLUIS_SLEUTEL)


def sluit(tekst):
    return _fernet().encrypt(tekst.encode()).decode()


def open_(geheim):
    try:
        return _fernet().decrypt(geheim.encode()).decode()
    except InvalidToken as e:
        raise KluisFout("Dit token past niet bij de kluissleutel. Koppel deze agenda opnieuw.") from e
