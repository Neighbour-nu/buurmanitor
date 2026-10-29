#!/usr/bin/env bash
# Sleutels van de werkruimte ZEROHERO (de Doellijn) instellen, zonder dat ze in beeld of in je geschiedenis komen.
# Vervangt na de overstap de losse scripts in ~/cockpit/doellijn-app/deploy (claude.sh, google.sh, enzovoort).
#
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh claude         Claude API-sleutel (adviseur, artikelen)
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh admin          Anthropic admin-sleutel (echte Claude-rekening)
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh google         Google Analytics en Search Console (JSON-bestand)
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh mailerlite     MailerLite API-sleutel
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh meta           Meta-token (Instagram en Facebook)
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh threads        Threads-token
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh tickettailor   Ticket Tailor API-sleutel
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh youtube        YouTube API-sleutel
#   bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh wordpress      WordPress-gebruiker en applicatiewachtwoord
#
# De waarden komen in ~/buurmanitor/zerohero.env (alleen leesbaar voor jou); daarna start Buurmanitor opnieuw.
set -euo pipefail
BM=~/buurmanitor
ENVB=$BM/zerohero.env
HULP="python3 $BM/app/deploy/envbestand.py"
[ -d "$BM/app" ] || { echo "Buurmanitor niet gevonden in $BM."; exit 1; }
touch "$ENVB" && chmod 600 "$ENVB"

geheim() {  # geheim NAAM "vraag"
  local w
  read -r -s -p "$2 (wordt niet getoond): " w; echo
  [ -n "$w" ] || { echo "Niets ingevuld; er is niets veranderd."; exit 1; }
  printf '%s\n' "$w" | $HULP zet "$ENVB" "ZEROHERO_$1"
  unset w
}
gewoon() {  # gewoon NAAM "vraag" [standaard]
  local w
  read -r -p "$2${3:+ [$3]}: " w
  w=${w:-${3:-}}
  [ -n "$w" ] && printf '%s\n' "$w" | $HULP zet "$ENVB" "ZEROHERO_$1"
  return 0
}

case "${1:-}" in
  claude) geheim ANTHROPIC_API_KEY "Claude API-sleutel (begint met sk-ant-)" ;;
  admin) geheim ANTHROPIC_ADMIN_KEY "Anthropic admin-sleutel (begint met sk-ant-admin)" ;;
  mailerlite) geheim MAILERLITE_API_KEY "MailerLite API-sleutel" ;;
  meta) geheim META_TOKEN "Meta-token van de systeemgebruiker" ;;
  threads) geheim THREADS_TOKEN "Threads-token" ;;
  tickettailor) geheim TT_API_KEY "Ticket Tailor API-sleutel (begint met sk_)" ;;
  youtube) geheim YOUTUBE_API_KEY "YouTube API-sleutel" ;;
  wordpress)
    gewoon WP_USER "WordPress-gebruikersnaam"
    geheim WP_APP_PASSWORD "WordPress-applicatiewachtwoord (24 tekens, zonder spaties)" ;;
  google)
    BESTAND=$BM/google-sleutel.json
    if [ ! -f "$BESTAND" ]; then
      echo "Kopieer eerst de JSON-sleutel van het serviceaccount naar de server, bijvoorbeeld vanaf je Mac:"
      echo "  scp ~/Downloads/<sleutelbestand>.json zerohero:~/buurmanitor/google-sleutel.json"
      echo "en draai dit script daarna opnieuw."; exit 1
    fi
    python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d.get('type')=='service_account' and d.get('private_key'); print('Sleutel van:', d['client_email'])" "$BESTAND" \
      || { echo "Dit is geen geldige serviceaccount-sleutel."; exit 1; }
    base64 -w0 "$BESTAND" | $HULP zet "$ENVB" ZEROHERO_GOOGLE_SLEUTEL_B64
    $HULP heeft "$ENVB" ZEROHERO_GA4_PROPERTY || printf '441520596\n' | $HULP zet "$ENVB" ZEROHERO_GA4_PROPERTY
    shred -u "$BESTAND" 2>/dev/null || rm -f "$BESTAND"
    echo "   Sleutel opgeslagen; het losse bestand is gewist." ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

echo "   Opgeslagen in $ENVB."
cd "$BM" && docker compose up -d app >/dev/null 2>&1
echo "Buurmanitor start opnieuw met de nieuwe instelling. Kijk over een halve minuut in ZEROHERO (Dashboard of Adviseur)."
