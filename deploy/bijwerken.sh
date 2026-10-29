#!/usr/bin/env bash
# Nieuwe versie van Buurmanitor ophalen en starten:  bash ~/buurmanitor/app/deploy/bijwerken.sh
set -euo pipefail
cd ~/buurmanitor/app && git pull --ff-only
if ! cmp -s ~/buurmanitor/app/deploy/docker-compose.yml ~/buurmanitor/docker-compose.yml; then
  cp ~/buurmanitor/docker-compose.yml ~/buurmanitor/docker-compose.yml.vorige
  cp ~/buurmanitor/app/deploy/docker-compose.yml ~/buurmanitor/docker-compose.yml
  echo "docker-compose.yml bijgewerkt (vorige versie: docker-compose.yml.vorige)"
fi
cd ~/buurmanitor && docker compose up -d --build
sleep 5 && docker compose ps && docker compose logs --tail=5 app
