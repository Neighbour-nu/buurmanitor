#!/usr/bin/env bash
# Clara (agenda's) klaarzetten: de kluissleutel voor tokens en de gegevens van je eigen apps bij Google en Microsoft.
#
#   bash ~/buurmanitor/app/deploy/clara.sh
#
# - De kluissleutel wordt de eerste keer vanzelf gemaakt (en daarna nooit meer veranderd).
# - Daarna vraagt het script om de client-id en het client-geheim van de Google-app en van de Microsoft-app,
#   en om de sleutel van OpenRouteService (voor reistijd).
#   Het geheim zie je niet terwijl je het plakt. Leeg laten (Enter) houdt de huidige waarde.
# - Hoe je die apps aanmaakt, staat in de README onder "Clara: apps bij Google en Microsoft".
set -euo pipefail
BM=~/buurmanitor
ENV=$BM/.env
HULP="python3 $BM/app/deploy/envbestand.py"
[ -f "$ENV" ] || { echo "$ENV bestaat niet. Is Buurmanitor geïnstalleerd?"; exit 1; }

if $HULP heeft "$ENV" KLUIS_SLEUTEL; then
  echo "== Kluissleutel staat er al (blijft ongewijzigd)."
else
  # Een Fernet-sleutel: 32 willekeurige bytes, base64 in de url-veilige variant.
  openssl rand -base64 32 | tr '+/' '-_' | $HULP zet "$ENV" KLUIS_SLEUTEL
  echo "== Kluissleutel gemaakt. Bewaar ~/buurmanitor/.env goed; zonder deze sleutel moet je de agenda's opnieuw koppelen."
fi

vraag() {  # vraag NAAM "omschrijving" geheim(0/1)
  local w
  if [ "$3" = 1 ]; then read -r -s -p "$2 (wordt niet getoond, Enter = niet wijzigen): " w; echo
  else read -r -p "$2 (Enter = niet wijzigen): " w; fi
  w=$(printf '%s' "$w" | tr -d '[:space:]')
  if [ -n "$w" ]; then printf '%s\n' "$w" | $HULP zet "$ENV" "$1"; echo "   $1 opgeslagen."; fi
}

echo
echo "== Google-app (voor de agenda's van neighbour.nu en zerohero.one)"
echo "   Terugkeeradres in de Google-app: https://$(grep -m1 '^PORTAL_HOST=' "$ENV" | cut -d= -f2- | tr -d "'\"")/clara/koppel/google/terug"
vraag GOOGLE_CLIENT_ID "Google client-id (eindigt op .apps.googleusercontent.com)" 0
vraag GOOGLE_CLIENT_SECRET "Google client-geheim (begint meestal met GOCSPX-)" 1

echo
echo "== Microsoft-app (voor IV-professional, Upper Spire en de gemeenteraad)"
echo "   Terugkeeradressen in de Microsoft-app: https://$(grep -m1 '^PORTAL_HOST=' "$ENV" | cut -d= -f2- | tr -d "'\"")/clara/koppel/microsoft/terug"
echo "   en .../clara/koppel/microsoft/beheerder"
vraag MS_CLIENT_ID "Microsoft toepassings-id (client-id, een GUID)" 0
vraag MS_CLIENT_SECRET "Microsoft clientgeheim (de waarde, niet het geheim-id)" 1

echo
echo "== OpenRouteService (reistijd naar afspraken buiten de deur)"
echo "   Sleutel aanmaken: account op openrouteservice.org, dan in het dashboard bij API Keys een sleutel (Standard)."
vraag ORS_API_KEY "OpenRouteService-sleutel" 1

chmod 600 "$ENV"
cd "$BM" && docker compose up -d app >/dev/null 2>&1
echo
echo "KLAAR. Buurmanitor start opnieuw. Open Agenda › Koppelingen in de portal en koppel je agenda's."
