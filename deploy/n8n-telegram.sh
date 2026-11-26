#!/usr/bin/env bash
# Laat n8n de tikken op keepersnieuws-knoppen van de ZEROHERO Doellijn-bot doorsturen naar Buurmanitor.
#
#   bash ~/buurmanitor/app/deploy/n8n-telegram.sh            aanpassen (vraagt om een n8n API-sleutel, niet getoond)
#   bash ~/buurmanitor/app/deploy/n8n-telegram.sh controle   laat zien hoe het nu staat
#   bash ~/buurmanitor/app/deploy/n8n-telegram.sh terug      de aanpassing weer weghalen
#
# Alles gebeurt op de server; de werkstroom wordt niet geëxporteerd of getoond. Zie n8n_telegram.py voor de details.
set -euo pipefail
BM=~/buurmanitor
[ -d "$BM/app" ] || { echo "Buurmanitor niet gevonden in $BM."; exit 1; }
[ -d ~/cockpit ] || { echo "De cockpit (met n8n) niet gevonden in ~/cockpit."; exit 1; }
exec python3 "$BM/app/deploy/n8n_telegram.py" "${1:-aan}"
