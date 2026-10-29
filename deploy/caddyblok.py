"""Eén site-blok in de Caddyfile vervangen door een doorverwijzing, en later het oorspronkelijke blok terugzetten.

  python3 caddyblok.py weg   CADDYFILE HOST BEWAARBESTAND DOEL   > nieuwe Caddyfile
        vervangt het blok "HOST { ... }" door "redir DOEL{uri} 302" en bewaart het oude blok in BEWAARBESTAND
  python3 caddyblok.py terug CADDYFILE HOST BEWAARBESTAND        > nieuwe Caddyfile
        zet het bewaarde blok terug op de plek van het huidige blok voor HOST

Het blok begint met een regel "HOST {" zonder inspringing; het einde wordt gevonden door accolades te tellen
(plaatshouders als {uri} en {env.X} openen en sluiten op dezelfde regel). Exitcode 1 met een melding als het niet lukt.
"""
import re
import sys


def blok(regels, host):
    start = next((i for i, r in enumerate(regels) if re.match(r"^" + re.escape(host) + r"\s*\{\s*$", r)), None)
    if start is None:
        sys.exit(f"Blok voor {host} niet gevonden in de Caddyfile")
    diepte = 0
    for i in range(start, len(regels)):
        diepte += regels[i].count("{") - regels[i].count("}")
        if diepte == 0:
            return start, i
    sys.exit(f"Einde van het blok voor {host} niet gevonden")


def main(argv):
    modus, bron, host, bewaar = argv[1:5]
    regels = open(bron, encoding="utf-8").read().split("\n")
    start, eind = blok(regels, host)
    oud = regels[start:eind + 1]
    if modus == "weg":
        doel = argv[5]
        if any(r.strip().startswith("redir ") for r in oud):
            print(f"   Caddy stuurde {host} al door.", file=sys.stderr)
            nieuw = oud
        else:
            with open(bewaar, "w", encoding="utf-8") as f:
                f.write("\n".join(oud) + "\n")
            nieuw = [f"{host} {{", f"\tredir {doel}{{uri}} 302", "}"]
    elif modus == "terug":
        nieuw = open(bewaar, encoding="utf-8").read().rstrip("\n").split("\n")
    else:
        sys.exit(__doc__)
    sys.stdout.write("\n".join(regels[:start] + nieuw + regels[eind + 1:]))


if __name__ == "__main__":
    main(sys.argv)
