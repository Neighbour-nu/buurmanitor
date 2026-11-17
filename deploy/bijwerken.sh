#!/usr/bin/env bash
# Nieuwe versie van Buurmanitor ophalen en starten:  bash ~/buurmanitor/app/deploy/bijwerken.sh
set -euo pipefail
cd ~/buurmanitor/app && git pull --ff-only
if ! cmp -s ~/buurmanitor/app/deploy/docker-compose.yml ~/buurmanitor/docker-compose.yml; then
  cp ~/buurmanitor/docker-compose.yml ~/buurmanitor/docker-compose.yml.vorige
  cp ~/buurmanitor/app/deploy/docker-compose.yml ~/buurmanitor/docker-compose.yml
  echo "docker-compose.yml bijgewerkt (vorige versie: docker-compose.yml.vorige)"
fi
# zerohero.env moet bestaan (docker-compose leest het); leeg tot de overstap van de Doellijn.
touch ~/buurmanitor/zerohero.env && chmod 600 ~/buurmanitor/zerohero.env
# Signal: map voor de koppelsleutels (alleen voor jou leesbaar) en de nieuwste signal-cli.
# (Docker kan de map al als root hebben gemaakt; dan met sudo, en anders niet stoppen.)
mkdir -p ~/buurmanitor/signal 2>/dev/null || sudo -n mkdir -p ~/buurmanitor/signal || true
chmod 700 ~/buurmanitor/signal 2>/dev/null || sudo -n chmod 700 ~/buurmanitor/signal 2>/dev/null \
  || echo "Let op: zet de rechten van ~/buurmanitor/signal zelf op 700:  sudo chmod 700 ~/buurmanitor/signal"
cd ~/buurmanitor && (docker compose pull -q signal || true) && docker compose up -d --build
sleep 5 && docker compose ps && docker compose logs --tail=5 app
