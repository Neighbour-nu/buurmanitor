#!/usr/bin/env bash
# Nachtelijke, versleutelde kopie van alles wat nodig is om de server opnieuw op te bouwen, naar OVHcloud Object Storage.
# Draait als root via cron (04:15, na de nachtelijke databasedumps). Instellen: bash ~/buurmanitor/app/deploy/extern.sh
#
# Wat gaat mee:
#  - de nachtelijke dumps van de Doellijn, Promonitor en Buurmanitor;
#  - een volledige dump van de PostgreSQL van de cockpit (ook Miniflux en eventueel n8n);
#  - de n8n-gegevens (werkstromen, versleutelde inloggegevens, het volume);
#  - de .env-bestanden, Caddyfile, docker-compose-bestanden en de Authelia-configuratie.
# Alles wordt door restic versleuteld met het wachtwoord uit /etc/buurmanitor-extern.env.
set -uo pipefail

ENV=/etc/buurmanitor-extern.env
H=${BUURMANITOR_HOME:-/home/ubuntu}
EXTRA=$H/buurmanitor/backups-extra
STATUS=$H/buurmanitor/backups/extern-status.json
[ -f "$ENV" ] || { echo "$ENV ontbreekt; draai eerst extern.sh"; exit 1; }
set -a; . "$ENV"; set +a
mkdir -p "$EXTRA" && chmod 700 "$EXTRA"
waarschuwingen=()
echo "== $(date '+%F %T') begin"

# 1. Volledige dump van de PostgreSQL van de cockpit (alle databases en gebruikers).
if [ -d "$H/cockpit" ]; then
  SU=$(cd "$H/cockpit" && docker compose exec -T db printenv POSTGRES_USER 2>/dev/null | tr -d '\r')
  if (cd "$H/cockpit" && docker compose exec -T db pg_dumpall -U "${SU:-postgres}") | gzip -9 > "$EXTRA/cockpit-postgres.sql.gz.tmp" \
     && [ -s "$EXTRA/cockpit-postgres.sql.gz.tmp" ]; then
    mv "$EXTRA/cockpit-postgres.sql.gz.tmp" "$EXTRA/cockpit-postgres.sql.gz"
  else
    rm -f "$EXTRA/cockpit-postgres.sql.gz.tmp"; waarschuwingen+=("dump PostgreSQL cockpit")
  fi

  # 2. n8n: werkstromen en inloggegevens (versleuteld, zoals n8n ze opslaat) plus het volume.
  if (cd "$H/cockpit" && docker compose exec -T n8n sh -c \
        'n8n export:workflow --all --output=/tmp/bm-werkstromen.json >/dev/null 2>&1 &&
         n8n export:credentials --all --output=/tmp/bm-inloggegevens.json >/dev/null 2>&1') \
     && (cd "$H/cockpit" && docker compose cp n8n:/tmp/bm-werkstromen.json "$EXTRA/n8n-werkstromen.json" >/dev/null \
         && docker compose cp n8n:/tmp/bm-inloggegevens.json "$EXTRA/n8n-inloggegevens.json" >/dev/null); then
    (cd "$H/cockpit" && docker compose exec -T n8n rm -f /tmp/bm-werkstromen.json /tmp/bm-inloggegevens.json) || true
  else
    waarschuwingen+=("export n8n")
  fi
  if ! docker run --rm -v cockpit_n8n_data:/d:ro -v "$EXTRA":/uit alpine:3 tar czf /uit/n8n-volume.tgz -C /d . 2>/dev/null; then
    waarschuwingen+=("volume n8n")
  fi
fi

# 3. Naar Object Storage. Alleen paden die bestaan.
paden=()
for p in "$H/cockpit/backups" "$H/promonitor/backups" "$H/buurmanitor/backups" "$EXTRA" \
         "$H/cockpit/.env" "$H/promonitor/.env" "$H/buurmanitor/.env" \
         "$H/cockpit/Caddyfile" "$H/cockpit/docker-compose.yml" "$H/cockpit/docker-compose.override.yml" \
         "$H/cockpit/authelia" "$H/promonitor/docker-compose.yml" "$H/buurmanitor/docker-compose.yml" "$H/.ssh/config"; do
  [ -e "$p" ] && paden+=("$p")
done
uitvoer=$(restic backup --tag nacht --host buurmanitor-server "${paden[@]}" 2>&1)
code=$?
echo "$uitvoer" | tail -n 6
snapshot=$(echo "$uitvoer" | awk '/snapshot [0-9a-f]+ saved/{print $2}' | tail -n1)
[ $code -eq 0 ] || waarschuwingen+=("restic backup (code $code)")

# 4. Bewaren: 14 dagen, 8 weken, 12 maanden. Opruimen op zondag (dat kost wat langer).
if [ $code -eq 0 ]; then
  if [ "$(date +%u)" = 7 ]; then
    restic forget --tag nacht --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune >/dev/null 2>&1 || waarschuwingen+=("opruimen")
  else
    restic forget --tag nacht --keep-daily 14 --keep-weekly 8 --keep-monthly 12 >/dev/null 2>&1 || waarschuwingen+=("opruimen")
  fi
fi

# 5. Status voor de pagina Gezondheid van Buurmanitor.
ok=$([ $code -eq 0 ] && echo true || echo false)
w=$(printf '%s\n' "${waarschuwingen[@]:-}" | python3 -c 'import json,sys; print(json.dumps([r for r in sys.stdin.read().splitlines() if r]))')
printf '{"tijd": "%s", "ok": %s, "snapshot": "%s", "waarschuwingen": %s}\n' "$(date -Iseconds)" "$ok" "${snapshot:-}" "$w" > "$STATUS.tmp"
mv "$STATUS.tmp" "$STATUS"
chown 1000:1000 "$STATUS" 2>/dev/null || true
echo "== $(date '+%F %T') klaar: ok=$ok, waarschuwingen: ${waarschuwingen[*]:-geen}"
[ $code -eq 0 ]
