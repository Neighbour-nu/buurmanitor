#!/usr/bin/env bash
# Sleutels voor Buurmanitor instellen, zonder dat ze in je scherm of geschiedenis komen:
#   bash ~/buurmanitor/app/deploy/sleutel.sh
# Vraagt om de Claude-sleutel (werkruimte Assistent in de Claude Console) en, optioneel, Telegram.
# Leeg laten (Enter) houdt de huidige waarde.
set -euo pipefail
ENV=~/buurmanitor/.env
[ -f "$ENV" ] || { echo "$ENV bestaat niet. Draai eerst installeer.sh."; exit 1; }

zet() {  # zet NAAM WAARDE in .env (vervangt of voegt toe)
  local naam=$1 waarde=$2
  if grep -q "^$naam=" "$ENV"; then
    python3 - "$ENV" "$naam" "$waarde" <<'PY'
import sys
pad, naam, waarde = sys.argv[1:4]
regels = open(pad, encoding="utf-8").read().splitlines()
regels = [f"{naam}={waarde}" if r.startswith(naam + "=") else r for r in regels]
open(pad, "w", encoding="utf-8").write("\n".join(regels) + "\n")
PY
  else
    printf '%s=%s\n' "$naam" "$waarde" >> "$ENV"
  fi
}

read -r -s -p "Claude API-sleutel (begint met sk-ant-, Enter = niet wijzigen): " k; echo
[ -n "$k" ] && zet ANTHROPIC_API_KEY "$k" && echo "   Claude-sleutel opgeslagen."
read -r -p "Maandplafond voor Claude in euro (Enter = niet wijzigen): " p
[ -n "$p" ] && zet MAANDPLAFOND_EUR "$p" && echo "   Maandplafond opgeslagen."
read -r -s -p "Telegram-bottoken (optioneel, Enter = niet wijzigen): " t; echo
[ -n "$t" ] && zet TELEGRAM_BOT_TOKEN "$t" && echo "   Telegram-bottoken opgeslagen."
read -r -p "Telegram-chat-id (optioneel, Enter = niet wijzigen): " ch
[ -n "$ch" ] && zet TELEGRAM_CHAT_ID "$ch" && echo "   Telegram-chat-id opgeslagen."
chmod 600 "$ENV"
cd ~/buurmanitor && docker compose up -d app >/dev/null && echo "Buurmanitor opnieuw gestart met de nieuwe instellingen."
