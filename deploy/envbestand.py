"""Hulpje voor de deploy-scripts: waarden in een env-bestand zetten zonder dat ze in beeld of in de proceslijst komen.

  python3 envbestand.py zet BESTAND NAAM            waarde komt via stdin (één regel)
  python3 envbestand.py leeg BESTAND NAAM           zet NAAM op een lege waarde
  python3 envbestand.py container BESTAND VOORVOEGSEL
        stdin: de omgeving van een container (docker inspect, één NAAM=waarde per regel). Schrijft elke variabele van
        de app als VOORVOEGSEL+NAAM in BESTAND; systeemvariabelen van het image en de proxyinstellingen blijven weg.
  python3 envbestand.py heeft BESTAND NAAM          exitcode 0 als NAAM een niet-lege waarde heeft

Waarden worden tussen enkele aanhalingstekens geschreven: Docker Compose neemt ze dan letterlijk over (een $ of een
spatie in een wachtwoord blijft zoals hij is). Het bestand krijgt rechten 600.
"""
import os
import re
import sys

OVERSLAAN = {"PATH", "HOME", "HOSTNAME", "LANG", "LC_ALL", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "PYTHON_PIP_VERSION",
             "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256", "PYTHONDONTWRITEBYTECODE",
             "PYTHONUNBUFFERED", "TZ", "PROXY_GEHEIM", "TOEGESTANE_HOSTS", "TERM"}
NAAM = re.compile(r"^[A-Z][A-Z0-9_]*$")


def citeer(w):
    if "'" not in w and "\n" not in w:
        return f"'{w}'"
    return '"' + w.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("\n", "\\n") + '"'


def lees(pad):
    try:
        return open(pad, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return []


def schrijf(pad, regels):
    oud = os.umask(0o077)
    try:
        with open(pad + ".nieuw", "w", encoding="utf-8") as f:
            f.write("\n".join(regels) + ("\n" if regels else ""))
        os.chmod(pad + ".nieuw", 0o600)
        os.replace(pad + ".nieuw", pad)
    finally:
        os.umask(oud)


def zet(regels, naam, waarde):
    nieuw = f"{naam}={citeer(waarde)}"
    if any(r.startswith(naam + "=") for r in regels):
        return [nieuw if r.startswith(naam + "=") else r for r in regels]
    return regels + [nieuw]


def waarde_van(regels, naam):
    for r in regels:
        if r.startswith(naam + "="):
            w = r.split("=", 1)[1]
            if len(w) >= 2 and w[0] == w[-1] and w[0] in "'\"":
                w = w[1:-1]
            return w
    return ""


def main(argv):
    actie, pad = argv[1], argv[2]
    regels = lees(pad)
    if actie == "zet":
        naam = argv[3]
        assert NAAM.match(naam), naam
        schrijf(pad, zet(regels, naam, sys.stdin.readline().rstrip("\n")))
    elif actie == "leeg":
        schrijf(pad, zet(regels, argv[3], ""))
    elif actie == "heeft":
        return 0 if waarde_van(regels, argv[3]) else 1
    elif actie == "container":
        voor = argv[3]
        aantal = 0
        for r in sys.stdin.read().splitlines():
            if "=" not in r:
                continue
            naam, w = r.split("=", 1)
            if naam in OVERSLAAN or not NAAM.match(naam) or naam.startswith(voor):
                continue
            regels = zet(regels, voor + naam, w)
            aantal += 1
        schrijf(pad, regels)
        print(aantal)
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
