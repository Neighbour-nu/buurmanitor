#!/usr/bin/env bash
# Back-up buiten de server instellen: elke nacht een versleutelde kopie naar OVHcloud Object Storage (met restic).
# Eenmalig draaien:  bash ~/buurmanitor/app/deploy/extern.sh
#
# Vooraf in het beheerscherm van OVHcloud: een Object Storage-container (bucket) met een S3-gebruiker
# (zie README, paragraaf "Back-up buiten de server"). Houd bij de hand: de regio, de bucketnaam,
# de access key en de secret key.
# Opnieuw draaien is veilig: een bestaande instelling blijft staan (het wachtwoord wordt nooit vervangen).
set -euo pipefail
ENV=/etc/buurmanitor-extern.env
SCRIPT="$HOME/buurmanitor/app/deploy/extern-backup.sh"

echo "== 1. restic installeren"
if ! command -v restic >/dev/null; then
  sudo apt-get update -qq && sudo apt-get install -y -qq restic
fi
restic version

if sudo test -f "$ENV"; then
  echo "== 2. Instellingen bestaan al in $ENV; die blijven staan."
else
  echo "== 2. Gegevens van je Object Storage"
  read -r -p "Regio (bijvoorbeeld de voor Frankfurt, gra voor Gravelines) [de]: " regio; regio=${regio:-de}
  read -r -p "Naam van de bucket: " bucket
  read -r -p "Access key: " sleutel
  read -r -s -p "Secret key (wordt niet getoond): " geheim; echo
  [ -n "$bucket" ] && [ -n "$sleutel" ] && [ -n "$geheim" ] || { echo "Bucket, access key en secret key zijn nodig."; exit 1; }
  wachtwoord=$(openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-40)
  TMP=$(mktemp)
  cat > "$TMP" <<EOF
RESTIC_REPOSITORY=s3:https://s3.$regio.io.cloud.ovh.net/$bucket
RESTIC_PASSWORD=$wachtwoord
AWS_ACCESS_KEY_ID=$sleutel
AWS_SECRET_ACCESS_KEY=$geheim
AWS_DEFAULT_REGION=$regio
EOF
  sudo install -m 600 -o root -g root "$TMP" "$ENV"
  rm -f "$TMP"
  echo
  echo "################################################################################"
  echo "  WACHTWOORD VAN DE BACK-UP (zonder dit wachtwoord is de back-up onleesbaar):"
  echo
  echo "     $wachtwoord"
  echo
  echo "  Zet dit NU in je wachtwoordmanager, onder 'Buurmanitor back-up (restic)'."
  echo "  Stuur het niet door in een chat. Het staat ook in $ENV op de server, maar"
  echo "  als de server verloren gaat, heb je het uit je wachtwoordmanager nodig."
  echo "################################################################################"
  read -r -p "Druk op Enter als je het wachtwoord hebt opgeslagen... " _
  clear || true
fi

echo "== 3. Opslag voorbereiden"
if sudo bash -c "set -a; . $ENV; restic cat config >/dev/null 2>&1"; then
  echo "   De opslag bestaat al."
else
  sudo bash -c "set -a; . $ENV; restic init" || { echo "De opslag kon niet worden aangemaakt. Klopt de regio, bucket en sleutel?"; exit 1; }
fi

echo "== 4. Eerste kopie maken (kan een paar minuten duren)"
sudo bash "$SCRIPT"

echo "== 5. Elke nacht om 04:15 (na de databasedumps)"
regel="15 4 * * * /bin/bash $SCRIPT >> /var/log/buurmanitor-extern.log 2>&1"
{ sudo crontab -l 2>/dev/null | grep -v 'extern-backup.sh' || true; echo "$regel"; } | sudo crontab -
sudo crontab -l | grep extern-backup.sh

echo "== 6. Kopieën in de opslag"
sudo bash -c "set -a; . $ENV; restic snapshots --compact" | tail -n 5

echo
echo "KLAAR. Elke nacht om 04:15 een versleutelde kopie naar Object Storage."
echo "De stand zie je in Buurmanitor op de pagina Gezondheid; het logboek staat in /var/log/buurmanitor-extern.log."
