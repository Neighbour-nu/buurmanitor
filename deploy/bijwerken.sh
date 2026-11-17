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
# Docker kan de map als root hebben gemaakt; dan kan signal-cli er niet in schrijven. Terug naar jou (met sudo), anders niet stoppen.
mkdir -p ~/buurmanitor/signal 2>/dev/null || sudo -n mkdir -p ~/buurmanitor/signal || true
if [ "$(stat -c %u ~/buurmanitor/signal)" != "$(id -u)" ]; then
  sudo -n chown -R "$(id -u):$(id -g)" ~/buurmanitor/signal 2>/dev/null \
    || echo "Let op: ~/buurmanitor/signal is niet van jou. Doe:  sudo chown -R $(id -un): ~/buurmanitor/signal"
fi
chmod 700 ~/buurmanitor/signal 2>/dev/null || true
cd ~/buurmanitor && (docker compose pull -q signal || true) && docker compose up -d --build
sleep 5 && docker compose ps && docker compose logs --tail=5 app
