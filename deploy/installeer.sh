#!/usr/bin/env bash
# Buurmanitor fase 0: de portal installeren naast de cockpit (Doellijn) en Promonitor.
# Eenmalig draaien op de server:  bash ~/buurmanitor/app/deploy/installeer.sh
# Opnieuw draaien is veilig: wat er al staat, wordt overgeslagen.
#
# Vooraf:
#  - de code staat in ~/buurmanitor/app (git clone, zie README);
#  - het proxygeheim is ingesteld (bash ~/cockpit/doellijn-app/deploy/proxygeheim.sh);
#  - de DNS van buurmanitor.neighbour.nu wijst naar deze server.
set -euo pipefail

PORTAL_HOST="${PORTAL_HOST:-buurmanitor.neighbour.nu}"
AUTH_HOST="${AUTH_HOST:-auth.neighbour.nu}"
COCKPIT=~/cockpit
DOEL=~/buurmanitor

[ -d "$DOEL/app/app" ] || { echo "De code staat niet in $DOEL/app. Clone eerst de repository (zie README)."; exit 1; }
[ -f "$COCKPIT/docker-compose.yml" ] || { echo "Map $COCKPIT met de cockpit niet gevonden."; exit 1; }
grep -q '^PROXY_GEHEIM=' "$COCKPIT/.env" || { echo "Eerst het proxygeheim instellen: bash ~/cockpit/doellijn-app/deploy/proxygeheim.sh"; exit 1; }
[ -f "$COCKPIT/docker-compose.override.yml" ] || { echo "Caddy kent het proxygeheim nog niet. Draai eerst: bash ~/cockpit/doellijn-app/deploy/proxygeheim.sh"; exit 1; }

echo "== 1. DNS controleren"
IP=$(curl -4 -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
gevonden=$(getent ahostsv4 "$PORTAL_HOST" | awk '{print $1; exit}' || true)
if [ "$gevonden" = "$IP" ]; then
  echo "   $PORTAL_HOST wijst naar deze server ($IP)."
elif [ "${DNS_OVERSLAAN:-0}" != 1 ]; then
  echo "   $PORTAL_HOST wijst naar '${gevonden:-niets}', niet naar $IP. Controleer het A-record en draai dit script opnieuw."
  exit 1
fi

echo "== 2. Netwerk van de cockpit opzoeken"
CADDY_ID=$(cd "$COCKPIT" && docker compose ps -q caddy)
[ -n "$CADDY_ID" ] || { echo "Caddy draait niet in $COCKPIT."; exit 1; }
NETWERK=$(docker inspect "$CADDY_ID" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
echo "   Caddy zit in netwerk: $NETWERK"

echo "== 3. Instellingen en geheimen ($DOEL/.env)"
mkdir -p "$DOEL/backups" && chmod 700 "$DOEL/backups"
GEHEIM=$(grep -m1 '^PROXY_GEHEIM=' "$COCKPIT/.env" | cut -d= -f2-)
if [ ! -f "$DOEL/.env" ]; then
  umask 077
  cat > "$DOEL/.env" <<EOF
BUURMANITOR_DB_PASSWORD=$(openssl rand -hex 24)
PORTAL_HOST=$PORTAL_HOST
AUTH_URL=https://$AUTH_HOST
COCKPIT_NETWERK=$NETWERK
PROXY_GEHEIM=$GEHEIM
TOEGESTANE_HOSTS=
ANTHROPIC_API_KEY=
MAANDPLAFOND_EUR=20
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
  umask 022
  echo "   .env aangemaakt."
else
  sed -i "s|^PROXY_GEHEIM=.*|PROXY_GEHEIM=$GEHEIM|" "$DOEL/.env"
  echo "   .env bestaat al, blijft staan (proxygeheim bijgewerkt)."
fi
chmod 600 "$DOEL/.env"
cp "$DOEL/app/deploy/docker-compose.yml" "$DOEL/docker-compose.yml"
touch "$DOEL/zerohero.env" && chmod 600 "$DOEL/zerohero.env"
PW=$(grep -m1 '^BUURMANITOR_DB_PASSWORD=' "$DOEL/.env" | cut -d= -f2-)

echo "== 4. Database buurmanitor in de PostgreSQL van de cockpit"
cd "$COCKPIT"
SU=$(docker compose exec -T db printenv POSTGRES_USER 2>/dev/null | tr -d '\r' || true)
SU=${SU:-postgres}
docker compose exec -T db psql -U "$SU" -d postgres -v ON_ERROR_STOP=1 -v pw="$PW" -q <<'SQL'
SELECT format('CREATE ROLE buurmanitor LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'buurmanitor') \gexec
ALTER ROLE buurmanitor WITH LOGIN PASSWORD :'pw';
SELECT 'CREATE DATABASE buurmanitor OWNER buurmanitor'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'buurmanitor') \gexec
SQL
echo "   Database en gebruiker buurmanitor staan klaar."

echo "== 5. Buurmanitor bouwen en starten (eerste keer duurt een paar minuten)"
cd "$DOEL"
docker compose config --quiet && echo "   compose OK"
docker compose up -d --build
status=onbekend
for i in $(seq 1 30); do
  status=$(docker inspect "$(docker compose ps -q app)" --format '{{.State.Health.Status}}' 2>/dev/null || echo onbekend)
  [ "$status" = healthy ] && break
  sleep 3
done
echo "   App: $status"
[ "$status" = healthy ] || { docker compose logs --tail=30 app; echo "De app start niet goed. Zie de regels hierboven."; exit 1; }

echo "== 6. Authelia: $PORTAL_HOST toevoegen"
cd "$COCKPIT"
CONF=authelia/config/configuration.yml
ALS=""
if [ ! -w "$CONF" ] || [ ! -w authelia/config ] || [ ! -w Caddyfile ] || [ ! -w . ]; then
  ALS=sudo
  echo "   Sommige bestanden zijn van root; deze stappen gaan met sudo."
fi
$ALS cp -n "$CONF" "$CONF.bak-buurmanitor" || true
NIEUW=$(mktemp)
$ALS cat "$CONF" | PORTAL_HOST="$PORTAL_HOST" python3 -c '
import os, sys
s = sys.stdin.read()
host = os.environ["PORTAL_HOST"]
dom = host.split(".", 1)[1]
if f"\x27{host}\x27" not in s:
    anker = "  rules:\n"
    assert anker in s, "Kan rules: niet vinden in de Authelia-configuratie"
    s = s.replace(anker, anker + f"    - domain:\n        - \x27{host}\x27\n      policy: two_factor\n", 1)
    print("   Regel voor " + host + " toegevoegd.", file=sys.stderr)
else:
    print("   Authelia kende " + host + " al.", file=sys.stderr)
assert f"domain: \x27{dom}\x27" in s, "Authelia heeft nog geen cookie voor " + dom + "; installeer eerst Promonitor"
sys.stdout.write(s)
' > "$NIEUW"
[ -s "$NIEUW" ] || { rm -f "$NIEUW"; echo "Authelia-configuratie kon niet worden bijgewerkt; er is niets veranderd."; exit 1; }
$ALS cp "$NIEUW" "$CONF"
rm -f "$NIEUW"
AUTHELIA_IMAGE=$(docker compose config --images | grep -m1 authelia || echo authelia/authelia:4.38)
if ! docker run --rm -v "$PWD/authelia/config:/config" -v "$PWD/authelia/secrets:/secrets:ro" \
     -e AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=/secrets/jwt \
     -e AUTHELIA_SESSION_SECRET_FILE=/secrets/session \
     -e AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=/secrets/storage \
     "$AUTHELIA_IMAGE" authelia validate-config --config /config/configuration.yml >/dev/null; then
  $ALS cp "$CONF.bak-buurmanitor" "$CONF"
  echo "Authelia-configuratie klopt niet; de oude versie is teruggezet. Er is niets kapot."
  exit 1
fi

echo "== 7. Caddy: $PORTAL_HOST naar de portal, met het proxygeheim"
$ALS cp -n Caddyfile Caddyfile.bak-buurmanitor || true
if ! $ALS grep -q "^$PORTAL_HOST {" Caddyfile; then
  $ALS tee -a Caddyfile >/dev/null <<EOF

$PORTAL_HOST {
	import login
	reverse_proxy buurmanitor-app:8000 {
		header_up X-Proxy-Geheim {env.PROXY_GEHEIM}
	}
}
EOF
  echo "   Caddyfile aangevuld."
else
  echo "   Caddy kende $PORTAL_HOST al."
fi
if ! docker run --rm -e PROXY_GEHEIM=x -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 \
     caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  $ALS cp Caddyfile.bak-buurmanitor Caddyfile
  echo "Caddyfile klopt niet; de oude versie is teruggezet. Er is niets kapot."
  exit 1
fi

echo "== 8. Authelia en Caddy herstarten"
docker compose restart authelia
sleep 6
docker compose restart caddy
sleep 8

echo "== 9. Controle"
code=$(curl -s -o /dev/null -w '%{http_code}' "https://$PORTAL_HOST/" || echo fout)
echo "   https://$PORTAL_HOST/ geeft $code (302 = doorgestuurd naar inloggen, goed)."
for h in cockpit.zerohero.one promonitor.neighbour.nu; do
  echo "   $h geeft $(curl -s -o /dev/null -w '%{http_code}' "https://$h/" || echo fout) (302 is goed)."
done

echo "== 10. Nachtelijke back-up van de database buurmanitor"
bash "$DOEL/app/deploy/backup.sh"

echo
echo "KLAAR. Open https://$PORTAL_HOST en log in zoals bij Promonitor."
echo "Zet daarna de Claude-sleutel voor Iris:  bash $DOEL/app/deploy/sleutel.sh"
