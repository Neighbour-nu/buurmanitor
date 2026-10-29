#!/usr/bin/env bash
# Na de overstap van Promonitor (fase 1) en de Doellijn (fase 2): de oude apps van de server halen.
#
#   bash ~/buurmanitor/app/deploy/opruimen.sh
#
# Wat het doet, na een controle dat Politiek en ZEROHERO in Buurmanitor goed werken en na jouw bevestiging:
#  1. Laatste back-up van de oude Promonitor-database (de stand van vóór de overstap) in ~/promonitor/backups.
#  2. Promonitor weg: de containers, het image, de oude database (volume) en het eigen netwerk.
#  3. De oude Doellijn-container en het image weg. Het profiel "oud" in ~/cockpit/docker-compose.override.yml blijft,
#     zodat "docker compose up -d" in ~/cockpit hem niet opnieuw maakt.
#  4. Oude bouwbestanden van Docker (build cache) en losse, ongebruikte image-lagen weg.
#
# Blijft staan: alle databases in de cockpit (doellijn, promonitor, buurmanitor), de nachtelijke back-ups en de
# back-up buiten de server, ~/promonitor/backups (de back-up van Politiek komt daar nog steeds terecht) en de code in
# ~/promonitor/app en ~/cockpit/doellijn-app. Na het opruimen werkt "terug" in de overstapscripts niet meer.
set -euo pipefail
BM=~/buurmanitor
PM=~/promonitor
COCKPIT=~/cockpit
STEMPEL=$(date +%F-%H%M)

[ -f "$BM/.politiek-overgestapt" ] || { echo "Politiek is nog niet overgestapt (overstap-politiek.sh). Er is niets veranderd."; exit 1; }
[ -f "$BM/.zerohero-overgestapt" ] || { echo "ZEROHERO is nog niet overgestapt (overstap-zerohero.sh). Er is niets veranderd."; exit 1; }
[ -f "$BM/.opgeruimd" ] && { echo "Al opgeruimd op $(cat "$BM/.opgeruimd")."; exit 0; }

echo "== Controle: werken Politiek en ZEROHERO in Buurmanitor?"
(cd "$BM" && docker compose exec -T app python - <<'PY'
import os, sys, urllib.request
kop = {"Remote-User": "controle", "X-Proxy-Geheim": os.environ.get("PROXY_GEHEIM", "")}
for pad in ["/", "/politiek/", "/politiek/inbox", "/zerohero/", "/zerohero/inbox"]:
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000" + pad, headers=kop), timeout=60)
    if r.status != 200 or "BUURMANITOR" not in r.read().decode("utf-8", "replace"):
        sys.exit(f"   {pad} werkt niet goed; ik ruim niets op.")
    print(f"   {pad} werkt")
PY
)
n=$(cd "$COCKPIT" && docker compose exec -T db psql -U promonitor -d promonitor -Atc "SELECT count(*) FROM stukken" 2>/dev/null | tr -d '\r' || true)
[ -n "$n" ] && [ "$n" -gt 0 ] || { echo "   De database promonitor in de cockpit lijkt leeg of onbereikbaar; ik ruim niets op."; exit 1; }
echo "   De database promonitor in de cockpit heeft $n stukken."
ls -1t "$PM"/backups/promonitor-*.sql.gz >/dev/null 2>&1 || { echo "   Geen back-up van Politiek gevonden in $PM/backups; ik ruim niets op."; exit 1; }

echo
echo "== Ruimte nu"
df -h / | tail -n 1 | awk '{print "   Schijf: " $3 " gebruikt, " $4 " vrij"}'
docker system df | sed 's/^/   /'
echo
echo "Hierna zijn de oude Promonitor en de oude Doellijn-container definitief weg en werkt 'terug' niet meer."
read -r -p "Typ OPRUIMEN om door te gaan: " ok
[ "$ok" = OPRUIMEN ] || { echo "Niets veranderd."; exit 0; }

zoek() {  # zoek PROJECT DIENST: container-id's (ook gestopte), ook als het profiel de dienst verbergt
  docker ps -aq --filter "label=com.docker.compose.project=$1" --filter "label=com.docker.compose.service=$2"
}

echo "== 1. Laatste back-up van de oude Promonitor-database"
if [ -n "$(zoek promonitor db)" ]; then
  (cd "$PM" && docker compose up -d db) >/dev/null 2>&1
  for _ in $(seq 1 30); do (cd "$PM" && docker compose exec -T db pg_isready -U promonitor -d promonitor) >/dev/null 2>&1 && break; sleep 2; done
  doel=$PM/backups/oude-promonitor-db-$STEMPEL.sql.gz
  (cd "$PM" && docker compose exec -T db pg_dump -U promonitor --no-owner promonitor) | gzip -9 > "$doel.tmp"
  mv "$doel.tmp" "$doel"
  echo "   $doel ($(du -h "$doel" | cut -f1))"
else
  echo "   De oude database bestaat niet meer; overgeslagen."
fi

echo "== 2. Promonitor weg (containers, image, oude database, netwerk)"
(cd "$PM" && docker compose down --rmi local --volumes --remove-orphans 2>&1 | sed 's/^/   /')
if [ -z "$(docker ps -aq --filter ancestor=postgres:16-alpine)" ]; then
  docker image rm postgres:16-alpine >/dev/null 2>&1 && echo "   Image postgres:16-alpine weg (werd alleen door Promonitor gebruikt)." || true
fi

echo "== 3. Oude Doellijn-container en image weg"
for cid in $(zoek cockpit cockpit-app); do
  img=$(docker inspect -f '{{.Image}}' "$cid")
  docker rm -f "$cid" >/dev/null && echo "   Container weg."
  docker image rm "$img" >/dev/null 2>&1 && echo "   Image weg." || echo "   Image wordt nog gebruikt; blijft staan."
done
grep -q 'profiles: \["oud"\]' "$COCKPIT/docker-compose.override.yml" \
  && echo "   Het profiel 'oud' blijft staan: 'docker compose up -d' in ~/cockpit maakt hem niet opnieuw." \
  || echo "   LET OP: het profiel 'oud' ontbreekt in ~/cockpit/docker-compose.override.yml. Laat het me weten."

echo "== 4. Oude bouwbestanden en losse image-lagen weg"
docker builder prune -af 2>/dev/null | tail -n 1 | sed 's/^/   /'
docker image prune -f 2>/dev/null | tail -n 1 | sed 's/^/   /'

date '+%F %H:%M' > "$BM/.opgeruimd"
echo
echo "== Ruimte nu"
df -h / | tail -n 1 | awk '{print "   Schijf: " $3 " gebruikt, " $4 " vrij"}'
docker system df | sed 's/^/   /'
echo
echo "KLAAR. Alleen Buurmanitor en de cockpit (Caddy, Authelia, database, n8n, Miniflux, rss-bridge) draaien nog."
