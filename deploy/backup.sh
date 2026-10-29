#!/usr/bin/env bash
# Dagelijkse back-up van de database buurmanitor (in de PostgreSQL van de cockpit) instellen.
# Eenmalig draaien:  bash ~/buurmanitor/app/deploy/backup.sh  (installeer.sh doet dit ook)
# Daarna maakt cron elke nacht om 03:37 een back-up in ~/buurmanitor/backups en bewaart de laatste 60.
set -euo pipefail
mkdir -p ~/buurmanitor/backups
chmod 700 ~/buurmanitor/backups
cat > ~/buurmanitor/backup-buurmanitor.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd ~/cockpit
doel=~/buurmanitor/backups/buurmanitor-$(date +%F).sql.gz
docker compose exec -T db pg_dump -U buurmanitor --no-owner buurmanitor | gzip -9 > "$doel.tmp"
mv "$doel.tmp" "$doel"
ls -1t ~/buurmanitor/backups/buurmanitor-*.sql.gz | tail -n +61 | xargs -r rm --
SH
chmod 700 ~/buurmanitor/backup-buurmanitor.sh
regel="37 3 * * * $HOME/buurmanitor/backup-buurmanitor.sh >> $HOME/buurmanitor/backups/backup.log 2>&1"
# Zonder bestaande crontab geven crontab -l en grep een foutcode; "|| true" voorkomt dat het script dan stopt.
{ crontab -l 2>/dev/null | grep -v 'backup-buurmanitor.sh' || true; echo "$regel"; } | crontab -
echo "== Eerste back-up maken"
~/buurmanitor/backup-buurmanitor.sh
ls -lh ~/buurmanitor/backups/ | tail -n 3
echo "KLAAR. Elke nacht om 03:37 een nieuwe back-up, de laatste 60 blijven bewaard."
