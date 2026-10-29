#!/usr/bin/env bash
# Fase 1: Promonitor verhuist naar Buurmanitor, onder https://buurmanitor.neighbour.nu/politiek.
#
#   bash ~/buurmanitor/app/deploy/overstap-politiek.sh proef      kopie bekijken, Promonitor blijft gewoon draaien
#   bash ~/buurmanitor/app/deploy/overstap-politiek.sh overstap   definitief overstappen
#   bash ~/buurmanitor/app/deploy/overstap-politiek.sh terug      alles terugzetten zoals het was
#
# proef     Kopieert de database van Promonitor naar de PostgreSQL van de cockpit (database promonitor) en zet
#           /politiek in Buurmanitor aan, met de automatische taken UIT. Promonitor draait gewoon door en blijft
#           de echte. Wat je in /politiek verandert, gaat bij de overstap verloren: kijk, maar werk nog in Promonitor.
#           Opnieuw draaien maakt een verse kopie.
# overstap  Stopt Promonitor, maakt een laatste kopie, controleert per tabel of alles is overgekomen, zet de
#           automatische taken in /politiek AAN, stuurt promonitor.neighbour.nu door naar /politiek, laat de
#           nachtelijke back-up voortaan uit de cockpit-database komen en zet de oude database stil (niet weg).
# terug     Kopieert de database terug naar Promonitor (met alles wat je in /politiek deed), zet Caddy en de
#           back-up terug en start Promonitor weer. /politiek gaat uit.
#
# Geheimen komen nooit in beeld: sleutels en wachtwoorden gaan rechtstreeks van ~/promonitor/.env naar ~/buurmanitor/.env.
set -euo pipefail

MODUS=${1:-}
case "$MODUS" in proef|overstap|terug) ;; *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;; esac

COCKPIT=~/cockpit
PM=~/promonitor
BM=~/buurmanitor
ENV=$BM/.env
OVERGESTAPT=$BM/.politiek-overgestapt   # bestaat na een geslaagde overstap
KOPIE=$BM/.politiek-kopie               # bestaat zodra deze script de database promonitor in de cockpit maakte
CADDYBLOK=$BM/politiek-caddyblok.txt    # het oorspronkelijke Caddy-blok van promonitor.neighbour.nu
OUDE_HOST=promonitor.neighbour.nu
STEMPEL=$(date +%F-%H%M)

[ -f "$ENV" ] || { echo "$ENV bestaat niet. Is Buurmanitor geïnstalleerd?"; exit 1; }
[ -f "$PM/.env" ] && [ -f "$PM/docker-compose.yml" ] || { echo "Promonitor niet gevonden in $PM."; exit 1; }
[ -f "$COCKPIT/docker-compose.yml" ] || { echo "De cockpit niet gevonden in $COCKPIT."; exit 1; }
mkdir -p "$PM/backups" && chmod 700 "$PM/backups"

waarde() { grep -m1 "^$1=" "$2" 2>/dev/null | cut -d= -f2- || true; }
zet() {  # zet NAAM WAARDE in ~/buurmanitor/.env (vervangt of voegt toe); de waarde komt niet in beeld
  python3 - "$ENV" "$1" "$2" <<'PY'
import sys
pad, naam, waarde = sys.argv[1:4]
regels = open(pad, encoding="utf-8").read().splitlines()
if any(r.startswith(naam + "=") for r in regels):
    regels = [f"{naam}={waarde}" if r.startswith(naam + "=") else r for r in regels]
else:
    regels.append(f"{naam}={waarde}")
open(pad, "w", encoding="utf-8").write("\n".join(regels) + "\n")
PY
  chmod 600 "$ENV"
}
cockpit_db() { (cd "$COCKPIT" && docker compose exec -T db "$@"); }
oude_db() { (cd "$PM" && docker compose exec -T db "$@"); }
SU=$(cockpit_db printenv POSTGRES_USER 2>/dev/null | tr -d '\r' || true); SU=${SU:-postgres}

# Aantal rijen per tabel, om oud en nieuw te vergelijken.
TELLING="SELECT table_name || ' ' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM public.%I', table_name), false, true, '')))[1]::text
 FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
vergelijk() {  # vergelijk NAAM_VAN_DE_STAP STRENG(1/0)
  local a b
  a=$(mktemp); b=$(mktemp)
  oude_db psql -U promonitor -d promonitor -Atc "$TELLING" > "$a"
  cockpit_db psql -U promonitor -d promonitor -Atc "$TELLING" > "$b"
  if diff -q "$a" "$b" >/dev/null; then
    echo "   Alle $(wc -l < "$a") tabellen hebben in oud en nieuw evenveel rijen."
    echo "   Bijvoorbeeld: $(grep -E '^(stukken|kennis[a-z_]*|documenten) ' "$a" | tr '\n' ' ' || true)"
    rm -f "$a" "$b"; return 0
  fi
  echo "   Verschil tussen oud (links) en nieuw (rechts):"; diff "$a" "$b" | sed 's/^/     /' || true
  rm -f "$a" "$b"
  [ "$2" = 1 ] && return 1
  echo "   (In de proef is dat normaal: Promonitor draait door en haalt intussen nieuwe stukken op.)"
}

wacht_oude_db() {
  for _ in $(seq 1 30); do oude_db pg_isready -U promonitor -d promonitor >/dev/null 2>&1 && return 0; sleep 2; done
  echo "De database van Promonitor start niet."; return 1
}

kopieer_naar_cockpit() {  # Promonitor-database (PG16) -> database promonitor in de cockpit (PG17)
  local pw dump
  pw=$(waarde POLITIEK_DB_PASSWORD "$ENV")
  [ -n "$pw" ] || { pw=$(openssl rand -hex 24); zet POLITIEK_DB_PASSWORD "$pw"; }
  if [ ! -f "$KOPIE" ] && [ -n "$(cockpit_db psql -U "$SU" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'promonitor'")" ]; then
    echo "In de cockpit bestaat al een database promonitor die niet door dit script is gemaakt. Ik stop, er is niets veranderd."
    exit 1
  fi
  (cd "$PM" && docker compose up -d db) >/dev/null 2>&1
  wacht_oude_db
  dump=$PM/backups/overstap-$MODUS-$STEMPEL.sql.gz
  echo "   Kopie maken van de database van Promonitor ($dump)"
  oude_db pg_dump -U promonitor --no-owner --no-acl promonitor | gzip -9 > "$dump.tmp"
  mv "$dump.tmp" "$dump"
  cockpit_db psql -U "$SU" -d postgres -v ON_ERROR_STOP=1 -v pw="$pw" -q <<'SQL'
SELECT format('CREATE ROLE promonitor LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'promonitor') \gexec
ALTER ROLE promonitor WITH LOGIN PASSWORD :'pw';
DROP DATABASE IF EXISTS promonitor WITH (FORCE);
CREATE DATABASE promonitor OWNER promonitor;
SQL
  touch "$KOPIE"
  gunzip -c "$dump" | cockpit_db psql -U promonitor -d promonitor -v ON_ERROR_STOP=1 -q >/dev/null
  echo "   Database promonitor staat in de cockpit."
}

instellingen_overnemen() {  # sleutels en adressen van Promonitor naar Buurmanitor, met POLITIEK_ ervoor
  local n w aantal=0
  for n in ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD MAIL_AAN MAIL_VAN; do
    w=$(waarde "$n" "$PM/.env")
    [ -n "$w" ] && { zet "POLITIEK_$n" "$w"; aantal=$((aantal + 1)); }
  done
  zet POLITIEK_DATABASE_URL "postgresql://promonitor:$(waarde POLITIEK_DB_PASSWORD "$ENV")@cockpit-db-1/promonitor"
  echo "   $aantal instellingen van Promonitor overgenomen (Claude-sleutel, Telegram, mail)."
}

buurmanitor_herstarten() {
  (cd "$BM" && docker compose up -d app 2>&1 | tail -n 2)
  local s=onbekend
  for _ in $(seq 1 40); do
    s=$(cd "$BM" && docker inspect "$(docker compose ps -q app)" --format '{{.State.Health.Status}}' 2>/dev/null || echo onbekend)
    [ "$s" = healthy ] && break
    sleep 3
  done
  echo "   Buurmanitor: $s"
  [ "$s" = healthy ] || { (cd "$BM" && docker compose logs --tail=40 app); return 1; }
}

politiek_controleren() {  # vraagt /politiek/ op binnen de container, zoals Caddy dat doet
  (cd "$BM" && docker compose exec -T app python - <<'PY'
import os, sys, urllib.request
kop = {"Remote-User": "controle", "X-Proxy-Geheim": os.environ.get("PROXY_GEHEIM", "")}
for pad in ["/politiek/", "/politiek/inbox", "/politiek/agenda", "/"]:
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000" + pad, headers=kop), timeout=30)
    t = r.read().decode("utf-8", "replace")
    if r.status != 200 or "BUURMANITOR" not in t:
        sys.exit(f"   {pad} werkt niet goed ({r.status})")
    print(f"   {pad} werkt")
PY
  )
}

ALS=""
[ -w "$COCKPIT/Caddyfile" ] && [ -w "$COCKPIT" ] || ALS=sudo
caddy_toepassen() {  # controleren en laden; bij een fout de vorige versie terug
  if ! docker run --rm -e PROXY_GEHEIM=x -v "$COCKPIT/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 \
       caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    $ALS cp "$COCKPIT/Caddyfile.bak-politiek-$STEMPEL" "$COCKPIT/Caddyfile"
    echo "   De nieuwe Caddyfile klopt niet; de vorige versie is teruggezet."; return 1
  fi
  (cd "$COCKPIT" && { docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || docker compose restart caddy >/dev/null; })
  sleep 3
}
caddy_blok() {  # caddy_blok weg|terug
  $ALS cp "$COCKPIT/Caddyfile" "$COCKPIT/Caddyfile.bak-politiek-$STEMPEL"
  local nieuw bron; nieuw=$(mktemp); bron=$(mktemp)
  $ALS cat "$COCKPIT/Caddyfile" > "$bron"
  if ! python3 - "$1" "$OUDE_HOST" "$CADDYBLOK" "$(waarde PORTAL_HOST "$ENV")" "$bron" > "$nieuw" <<'PY'
import re, sys
modus, host, bewaar, portal, bron = sys.argv[1:6]
regels = open(bron, encoding="utf-8").read().split("\n")
start = next((i for i, r in enumerate(regels) if re.match(r"^" + re.escape(host) + r"\s*\{\s*$", r)), None)
if start is None:
    sys.exit(f"Blok voor {host} niet gevonden in de Caddyfile")
diepte, eind = 0, None
for i in range(start, len(regels)):
    diepte += regels[i].count("{") - regels[i].count("}")
    if diepte == 0:
        eind = i
        break
if eind is None:
    sys.exit("Einde van het blok niet gevonden")
oud = regels[start:eind + 1]
if modus == "weg":
    if any("redir " in r and "/politiek" in r for r in oud):
        print("   Caddy stuurde al door naar /politiek.", file=sys.stderr)
        nieuw = oud
    else:
        open(bewaar, "w", encoding="utf-8").write("\n".join(oud) + "\n")
        nieuw = [f"{host} {{", f"\tredir https://{portal}/politiek{{uri}} 302", "}"]
else:
    nieuw = open(bewaar, encoding="utf-8").read().rstrip("\n").split("\n")
sys.stdout.write("\n".join(regels[:start] + nieuw + regels[eind + 1:]))
PY
  then rm -f "$nieuw" "$bron"; echo "   Caddyfile kon niet worden aangepast; er is niets veranderd."; return 1; fi
  rm -f "$bron"
  [ -s "$nieuw" ] || { rm -f "$nieuw"; echo "   Caddyfile kon niet worden aangepast; er is niets veranderd."; return 1; }
  $ALS cp "$nieuw" "$COCKPIT/Caddyfile"; rm -f "$nieuw"
  caddy_toepassen
}

backup_uit_cockpit() {  # nachtelijke back-up (03:27) voortaan uit de cockpit-database; zelfde map en bestandsnamen
  cat > "$PM/backup-promonitor.sh" <<'SH'
#!/usr/bin/env bash
# Na de overstap naar Buurmanitor: de database promonitor staat in de PostgreSQL van de cockpit.
set -euo pipefail
cd ~/cockpit
doel=~/promonitor/backups/promonitor-$(date +%F).sql.gz
docker compose exec -T db pg_dump -U promonitor --no-owner promonitor | gzip -9 > "$doel.tmp"
mv "$doel.tmp" "$doel"
ls -1t ~/promonitor/backups/promonitor-*.sql.gz | tail -n +61 | xargs -r rm --
SH
  chmod 700 "$PM/backup-promonitor.sh"
  "$PM/backup-promonitor.sh"
  echo "   Back-up komt voortaan uit de cockpit: $(ls -1t "$PM"/backups/promonitor-*.sql.gz | head -1)"
}

# ---------------------------------------------------------------------------------------------------------------
if [ "$MODUS" = proef ]; then
  [ -f "$OVERGESTAPT" ] && { echo "De overstap is al gedaan. Een proef zou je echte gegevens overschrijven; ik stop."; exit 1; }
  echo "== Proef: kopie van Promonitor onder /politiek (Promonitor blijft draaien)"
  kopieer_naar_cockpit
  vergelijk proef 0
  instellingen_overnemen
  zet POLITIEK_PLANNER 0
  buurmanitor_herstarten
  politiek_controleren
  echo
  echo "KLAAR. Kijk op https://$(waarde PORTAL_HOST "$ENV")/politiek"
  echo "Dit is een kopie zonder automatische taken. Werk nog in Promonitor; wat je hier verandert, gaat bij de overstap verloren."
  echo "Bevalt het? Dan:  bash $BM/app/deploy/overstap-politiek.sh overstap"
  exit 0
fi

if [ "$MODUS" = overstap ]; then
  [ -f "$OVERGESTAPT" ] && { echo "De overstap is al gedaan (sinds $(cat "$OVERGESTAPT"))."; exit 0; }
  echo "== 1. Promonitor stoppen (de database blijft nog even aan)"
  (cd "$PM" && docker compose stop app)
  echo "== 2. Laatste kopie naar de cockpit"
  kopieer_naar_cockpit
  if ! vergelijk overstap 1; then
    (cd "$PM" && docker compose start app)
    echo "De kopie is niet compleet. Promonitor draait weer; er is verder niets veranderd."; exit 1
  fi
  echo "== 3. /politiek met de automatische taken aan"
  instellingen_overnemen
  zet POLITIEK_PLANNER 1
  if ! buurmanitor_herstarten || ! politiek_controleren; then
    zet POLITIEK_PLANNER 0; zet POLITIEK_DATABASE_URL ""; buurmanitor_herstarten || true
    (cd "$PM" && docker compose start app)
    echo "/politiek werkt niet goed. Promonitor draait weer zoals voorheen; er is verder niets veranderd."; exit 1
  fi
  echo "== 4. $OUDE_HOST doorsturen naar /politiek"
  caddy_blok weg || echo "   LET OP: doorsturen lukte niet. $OUDE_HOST geeft nu een foutmelding; werk via /politiek en laat het mij weten."
  echo "   $OUDE_HOST geeft nu $(curl -s -o /dev/null -w '%{http_code} naar %{redirect_url}' "https://$OUDE_HOST/agenda" || echo fout)"
  echo "== 5. Nachtelijke back-up uit de cockpit"
  backup_uit_cockpit
  echo "== 6. Oude database stilzetten (blijft bewaard voor 'terug')"
  (cd "$PM" && docker compose stop db)
  date '+%F %H:%M' > "$OVERGESTAPT"
  echo
  echo "KLAAR. Promonitor draait nu als werkruimte Politiek: https://$(waarde PORTAL_HOST "$ENV")/politiek"
  echo "Oude links (https://$OUDE_HOST/...) komen vanzelf op de goede plek uit."
  echo "Draai niet meer ~/promonitor/app/deploy/bijwerken.sh; nieuwe versies komen via bash $BM/app/deploy/bijwerken.sh"
  echo "Terug naar de oude situatie kan altijd:  bash $BM/app/deploy/overstap-politiek.sh terug"
  exit 0
fi

# terug
[ -f "$OVERGESTAPT" ] || [ -f "$KOPIE" ] || { echo "Er is niets om terug te zetten."; exit 0; }
if [ ! -f "$OVERGESTAPT" ]; then
  echo "== Alleen een proef gedaan: /politiek uitzetten (Promonitor is altijd de echte gebleven)"
  zet POLITIEK_PLANNER 0; zet POLITIEK_DATABASE_URL ""
  buurmanitor_herstarten
  echo "KLAAR. De portal linkt weer naar https://$OUDE_HOST"; exit 0
fi
echo "== 1. /politiek uitzetten, zodat er niets meer verandert"
zet POLITIEK_PLANNER 0; zet POLITIEK_DATABASE_URL ""
buurmanitor_herstarten
echo "== 2. Database terugkopiëren naar Promonitor"
(cd "$PM" && docker compose up -d db) >/dev/null
wacht_oude_db
dump=$PM/backups/terug-$STEMPEL.sql.gz
# pg_dump van PostgreSQL 17 schrijft SET transaction_timeout; PostgreSQL 16 kent dat niet.
cockpit_db pg_dump -U promonitor --no-owner --no-acl promonitor | grep -v '^SET transaction_timeout' | gzip -9 > "$dump.tmp"
mv "$dump.tmp" "$dump"
oude_db psql -U promonitor -d postgres -v ON_ERROR_STOP=1 -q -c 'DROP DATABASE IF EXISTS promonitor WITH (FORCE)' -c 'CREATE DATABASE promonitor OWNER promonitor'
gunzip -c "$dump" | oude_db psql -U promonitor -d promonitor -v ON_ERROR_STOP=1 -q >/dev/null
vergelijk terug 1 || { echo "De terugkopie is niet compleet. Kopie staat in $dump; Promonitor is nog niet gestart."; exit 1; }
echo "== 3. Caddy: $OUDE_HOST weer naar Promonitor"
if [ -f "$CADDYBLOK" ]; then caddy_blok terug || echo "   LET OP: Caddy terugzetten lukte niet. Zet $COCKPIT/Caddyfile.bak-politiek-* terug."; fi
echo "== 4. Back-up weer uit de database van Promonitor"
bash "$PM/app/deploy/backup.sh" >/dev/null && echo "   Back-up hersteld."
echo "== 5. Promonitor starten"
(cd "$PM" && docker compose up -d)
rm -f "$OVERGESTAPT"
echo
echo "KLAAR. Promonitor draait weer op https://$OUDE_HOST, met alles wat je in /politiek hebt gedaan."
