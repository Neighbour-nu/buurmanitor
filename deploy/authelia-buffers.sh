#!/usr/bin/env bash
# Authelia: grotere buffers, zodat lange terugkeeradressen (bijvoorbeeld van Microsoft na het koppelen van een agenda)
# niet meer stranden op "431 Request Header Fields Too Large". Standaard is dat 4 kB; dit zet het op 16 kB.
#
#   bash ~/buurmanitor/app/deploy/authelia-buffers.sh
#
# Maakt eerst een reservekopie, controleert de nieuwe configuratie en zet bij een fout de oude terug.
set -euo pipefail
COCKPIT="${COCKPIT:-$HOME/cockpit}"
cd "$COCKPIT"
CONF=authelia/config/configuration.yml
[ -e "$CONF" ] || { echo "Kan $COCKPIT/$CONF niet vinden."; exit 1; }
ALS=""
if [ ! -w "$CONF" ] || [ ! -w authelia/config ]; then
  ALS=sudo
  echo "De Authelia-configuratie is van root; deze stappen gaan met sudo."
fi

echo "== 1. Wat Authelia zelf meldt (laatste 431-achtige regels, als die er zijn)"
docker compose logs --tail=300 authelia 2>/dev/null | grep -i -E "buffer|header|431" | tail -3 || true

echo "== 2. Buffers op 16 kB zetten"
RESERVE="$CONF.bak-buffers-$(date +%Y%m%d%H%M%S)"
$ALS cp "$CONF" "$RESERVE"
NIEUW=$(mktemp)
$ALS cat "$CONF" | python3 -c '
import re, sys
regels = sys.stdin.read().split("\n")
GROOT = 16384
start = next((i for i, r in enumerate(regels) if re.match(r"^server:\s*(#.*)?$", r)), None)
if start is None:
    regels += ["", "server:", "  buffers:", f"    read: {GROOT}", f"    write: {GROOT}"]
    print("   Blok server: toegevoegd.", file=sys.stderr)
else:
    eind = start + 1
    while eind < len(regels) and (regels[eind].strip() == "" or regels[eind].startswith((" ", "#"))):
        eind += 1
    blok = range(start + 1, eind)
    b = next((i for i in blok if re.match(r"^  buffers:\s*(#.*)?$", regels[i])), None)
    if b is None:
        regels[start + 1:start + 1] = ["  buffers:", f"    read: {GROOT}", f"    write: {GROOT}"]
        print("   buffers: toegevoegd onder server:.", file=sys.stderr)
    else:
        i, gezien = b + 1, set()
        while i < eind and (regels[i].startswith("    ") or regels[i].strip() == ""):
            m = re.match(r"^    (read|write):\s*(\d+)", regels[i])
            if m:
                gezien.add(m.group(1))
                if int(m.group(2)) < GROOT:
                    regels[i] = f"    {m.group(1)}: {GROOT}"
            i += 1
        for naam in ("read", "write"):
            if naam not in gezien:
                regels.insert(b + 1, f"    {naam}: {GROOT}")
        print("   Bestaande buffers bijgewerkt.", file=sys.stderr)
sys.stdout.write("\n".join(regels))
' > "$NIEUW"
[ -s "$NIEUW" ] || { rm -f "$NIEUW"; echo "Kon de configuratie niet bijwerken; er is niets veranderd."; exit 1; }
$ALS cp "$NIEUW" "$CONF"
rm -f "$NIEUW"

echo "== 3. Controleren"
AUTHELIA_IMAGE=$(docker compose config --images | grep -m1 authelia || echo authelia/authelia:4.38)
if ! docker run --rm -v "$PWD/authelia/config:/config" -v "$PWD/authelia/secrets:/secrets:ro" \
     -e AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=/secrets/jwt \
     -e AUTHELIA_SESSION_SECRET_FILE=/secrets/session \
     -e AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=/secrets/storage \
     "$AUTHELIA_IMAGE" authelia validate-config --config /config/configuration.yml; then
  $ALS cp "$RESERVE" "$CONF"
  echo "De nieuwe configuratie klopt niet; de oude is teruggezet. Er is niets kapot."
  exit 1
fi
$ALS grep -n -A3 "^  buffers:" "$CONF" || true

echo "== 4. Authelia herstarten"
docker compose restart authelia
sleep 6
docker compose ps authelia
echo "KLAAR. Probeer het koppelen nu opnieuw. Reservekopie: $COCKPIT/$RESERVE"
