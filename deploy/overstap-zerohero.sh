#!/usr/bin/env bash
# Fase 2: de Doellijn verhuist naar Buurmanitor, onder https://buurmanitor.neighbour.nu/zerohero.
#
#   bash ~/buurmanitor/app/deploy/overstap-zerohero.sh proef      /zerohero aanzetten, de oude Doellijn blijft draaien
#   bash ~/buurmanitor/app/deploy/overstap-zerohero.sh overstap   definitief overstappen
#   bash ~/buurmanitor/app/deploy/overstap-zerohero.sh terug      alles terugzetten zoals het was
#
# De database doellijn staat al in de PostgreSQL van de cockpit en verhuist dus niet; er wordt niets gekopieerd.
#
# proef     Neemt de instellingen van de oude app over (rechtstreeks uit de container, zonder dat ze in beeld komen)
#           en zet /zerohero aan, met de achtergrondtaken UIT. Je werkt hier met de echte gegevens: wat je in
#           /zerohero doet, zie je ook in de oude Doellijn, en andersom. n8n praat nog met de oude app.
# overstap  Stopt de oude app (en zorgt dat die niet vanzelf terugkomt), geeft de portal op het netwerk ook de naam
#           cockpit-app (zodat n8n zonder aanpassing de portal bereikt), zet de achtergrondtaken in /zerohero AAN,
#           controleert vanuit n8n of /intern werkt en stuurt cockpit.zerohero.one door naar /zerohero.
#           Lukt een stap niet, dan draait de oude Doellijn weer zoals voorheen.
# terug     Zet de oude Doellijn, n8n en Caddy terug zoals het was. /zerohero gaat uit.
set -euo pipefail

MODUS=${1:-}
case "$MODUS" in proef|overstap|terug) ;; *) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;; esac

COCKPIT=~/cockpit
BM=~/buurmanitor
ENV=$BM/.env
ZENV=$BM/zerohero.env
HULP="python3 $BM/app/deploy/envbestand.py"
OVERGESTAPT=$BM/.zerohero-overgestapt   # bestaat na een geslaagde overstap (ook de oude deploy-scripts kijken hiernaar)
CADDYBLOK=$BM/zerohero-caddyblok.txt    # het oorspronkelijke Caddy-blok van cockpit.zerohero.one
OVERRIDE=$COCKPIT/docker-compose.override.yml
OUDE_HOST=cockpit.zerohero.one
STEMPEL=$(date +%F-%H%M)

[ -f "$ENV" ] || { echo "$ENV bestaat niet. Is Buurmanitor geïnstalleerd?"; exit 1; }
[ -f "$COCKPIT/docker-compose.yml" ] || { echo "De cockpit niet gevonden in $COCKPIT."; exit 1; }
touch "$ZENV" && chmod 600 "$ZENV"
PORTAL=$(grep -m1 '^PORTAL_HOST=' "$ENV" | cut -d= -f2-); PORTAL=${PORTAL:-buurmanitor.neighbour.nu}

zet_env() { printf '%s\n' "$2" | $HULP zet "$ENV" "$1"; }   # in ~/buurmanitor/.env (voor docker-compose zelf)
zet_z() { printf '%s\n' "$2" | $HULP zet "$ZENV" "$1"; }    # in zerohero.env (voor de app)
oude_app() { (cd "$COCKPIT" && docker compose "$@"); }

instellingen_overnemen() {
  local cid aantal
  cid=$(cd "$COCKPIT" && docker compose ps -aq cockpit-app)
  [ -n "$cid" ] || { echo "De container van de oude Doellijn (cockpit-app) is niet gevonden."; exit 1; }
  aantal=$(docker inspect "$cid" --format '{{range .Config.Env}}{{println .}}{{end}}' | $HULP container "$ZENV" ZEROHERO_)
  # De database heet in het netwerk van de cockpit "db"; Buurmanitor gebruikt de containernaam, net als voor zijn eigen database.
  python3 - "$ZENV" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = re.sub(r"^(ZEROHERO_DATABASE_URL=['\"]?postgres(?:ql)?://[^@\n]*@)db([:/])", r"\1cockpit-db-1\2", s, flags=re.M)
open(p, "w", encoding="utf-8").write(s)
PY
  $HULP heeft "$ZENV" ZEROHERO_DATABASE_URL || { echo "De oude app heeft geen DATABASE_URL; ik stop, er is niets veranderd."; exit 1; }
  echo "   $aantal instellingen van de oude Doellijn overgenomen (database, sleutels, koppelingen)."
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

zerohero_controleren() {  # vraagt de schermen op binnen de container, zoals Caddy dat doet
  (cd "$BM" && docker compose exec -T app python - <<'PY'
import os, sys, urllib.request
kop = {"Remote-User": "controle", "X-Proxy-Geheim": os.environ.get("PROXY_GEHEIM", "")}
for pad in ["/zerohero/", "/zerohero/inbox", "/zerohero/concepten", "/zerohero/dashboard", "/"]:
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000" + pad, headers=kop), timeout=60)
    t = r.read().decode("utf-8", "replace")
    if r.status != 200 or "BUURMANITOR" not in t:
        sys.exit(f"   {pad} werkt niet goed ({r.status})")
    print(f"   {pad} werkt")
r = urllib.request.urlopen("http://127.0.0.1:8000/intern/weging/leerpunten", timeout=30)
print("   /intern (voor n8n) werkt" if r.status == 200 else sys.exit("   /intern werkt niet"))
PY
  )
}

n8n_controleren() {  # vanuit de n8n-container: komt http://cockpit-app:8000/intern/... nu bij de portal uit?
  (cd "$COCKPIT" && docker compose exec -T n8n node -e '
fetch("http://cockpit-app:8000/intern/weging/leerpunten").then(async r => {
  // Alleen de portal heeft /static/logo.svg; de oude Doellijn geeft daar 404.
  const portal = (await fetch("http://cockpit-app:8000/static/logo.svg").then(x => x.status).catch(() => 0)) === 200;
  console.log("   n8n bereikt /intern: " + r.status + (portal ? " (bij de portal)" : " (NIET bij de portal)"));
  process.exit(r.status === 200 && portal ? 0 : 1);
}).catch(e => { console.log("   n8n bereikt cockpit-app niet: " + e.message); process.exit(1); });')
}

ALS=""
[ -w "$COCKPIT/Caddyfile" ] && [ -w "$COCKPIT" ] || ALS=sudo
caddy_blok() {  # caddy_blok weg|terug
  local nieuw bron
  nieuw=$(mktemp); bron=$(mktemp)
  $ALS cp "$COCKPIT/Caddyfile" "$COCKPIT/Caddyfile.bak-zerohero-$STEMPEL"
  $ALS cat "$COCKPIT/Caddyfile" > "$bron"
  if ! python3 "$BM/app/deploy/caddyblok.py" "$1" "$bron" "$OUDE_HOST" "$CADDYBLOK" "https://$PORTAL/zerohero" > "$nieuw" || [ ! -s "$nieuw" ]; then
    rm -f "$nieuw" "$bron"; echo "   Caddyfile kon niet worden aangepast; er is niets veranderd."; return 1
  fi
  rm -f "$bron"
  $ALS cp "$nieuw" "$COCKPIT/Caddyfile"; rm -f "$nieuw"
  if ! docker run --rm -e PROXY_GEHEIM=x -v "$COCKPIT/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 \
       caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    $ALS cp "$COCKPIT/Caddyfile.bak-zerohero-$STEMPEL" "$COCKPIT/Caddyfile"
    echo "   De nieuwe Caddyfile klopt niet; de vorige versie is teruggezet."; return 1
  fi
  oude_app exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || oude_app restart caddy >/dev/null
  sleep 3
}

# De oude app krijgt in docker-compose.override.yml het profiel "oud": een gewone "docker compose up -d" in ~/cockpit
# start hem dan niet meer. Anders zouden er twee containers met de naam cockpit-app op het netwerk staan.
profiel() {  # profiel aan|uit
  [ -f "$OVERRIDE" ] || printf '# Docker Compose leest dit bestand automatisch mee.\nservices:\n' > "$OVERRIDE"
  cp "$OVERRIDE" "$OVERRIDE.bak-zerohero-$STEMPEL"
  python3 - "$1" "$OVERRIDE" <<'PY'
import sys
modus, pad = sys.argv[1:3]
regels = open(pad, encoding="utf-8").read().split("\n")
merk = '    profiles: ["oud"]  # overstap-zerohero.sh: de Doellijn draait in Buurmanitor'
regels = [r for r in regels if r != merk]
if modus == "aan":
    if "  cockpit-app:" in regels:
        regels.insert(regels.index("  cockpit-app:") + 1, merk)
    else:
        i = regels.index("services:")
        regels[i + 1:i + 1] = ["  cockpit-app:", merk]
open(pad, "w", encoding="utf-8").write("\n".join(regels))
PY
  if ! (cd "$COCKPIT" && docker compose config --quiet); then
    cp "$OVERRIDE.bak-zerohero-$STEMPEL" "$OVERRIDE"; echo "   docker-compose.override.yml klopt niet; teruggezet."; return 1
  fi
}

oude_terug() {  # na een mislukte stap: alles weer naar de oude Doellijn
  zet_env ZEROHERO_ALIAS ""; zet_z ZEROHERO_ACHTERGROND 0
  buurmanitor_herstarten || true
  profiel uit || true
  oude_app up -d cockpit-app >/dev/null 2>&1 || oude_app start cockpit-app
}

# ---------------------------------------------------------------------------------------------------------------
if [ "$MODUS" = proef ]; then
  [ -f "$OVERGESTAPT" ] && { echo "De overstap is al gedaan (sinds $(cat "$OVERGESTAPT"))."; exit 0; }
  echo "== Proef: /zerohero aanzetten (de oude Doellijn blijft draaien)"
  instellingen_overnemen
  zet_z ZEROHERO_ACHTERGROND 0
  zet_env ZEROHERO_ALIAS ""
  buurmanitor_herstarten
  zerohero_controleren
  echo
  echo "KLAAR. Kijk op https://$PORTAL/zerohero"
  echo "Dit zijn de echte gegevens: wat je hier doet, zie je ook in de oude Doellijn. De achtergrondtaken staan hier uit"
  echo "en n8n praat nog met de oude app."
  echo "Bevalt het? Dan:  bash $BM/app/deploy/overstap-zerohero.sh overstap"
  exit 0
fi

if [ "$MODUS" = overstap ]; then
  [ -f "$OVERGESTAPT" ] && { echo "De overstap is al gedaan (sinds $(cat "$OVERGESTAPT"))."; exit 0; }
  echo "== 1. Instellingen van de oude Doellijn (opnieuw) overnemen"
  instellingen_overnemen
  echo "== 2. Oude Doellijn stoppen"
  oude_app stop cockpit-app
  profiel aan || { oude_app start cockpit-app; echo "Er is verder niets veranderd."; exit 1; }
  echo "   Gestopt; een gewone 'docker compose up -d' in ~/cockpit start hem niet meer."
  echo "== 3. Portal wordt ook cockpit-app, met de achtergrondtaken aan"
  zet_env ZEROHERO_ALIAS cockpit-app
  zet_z ZEROHERO_ACHTERGROND 1
  if ! buurmanitor_herstarten || ! zerohero_controleren || ! n8n_controleren; then
    oude_terug
    echo "De overstap is niet gelukt. De oude Doellijn draait weer zoals voorheen; er is verder niets veranderd."; exit 1
  fi
  echo "== 4. $OUDE_HOST doorsturen naar /zerohero"
  caddy_blok weg || echo "   LET OP: doorsturen lukte niet. $OUDE_HOST geeft nu de portal; werk via /zerohero en laat het mij weten."
  echo "   $OUDE_HOST geeft nu $(curl -s -o /dev/null -w '%{http_code} naar %{redirect_url}' "https://$OUDE_HOST/inbox" || echo fout)"
  echo "== 5. Oude deploy-scripts uitschakelen"
  date '+%F %H:%M' > "$OVERGESTAPT"
  if git -C "$COCKPIT/doellijn-app" pull -q --ff-only 2>/dev/null; then
    echo "   De scripts in ~/cockpit/doellijn-app/deploy stoppen nu met een verwijzing naar Buurmanitor."
  else
    echo "   LET OP: ~/cockpit/doellijn-app kon niet worden bijgewerkt. Gebruik de scripts daar niet meer."
  fi
  echo
  echo "KLAAR. De Doellijn draait nu als werkruimte ZEROHERO: https://$PORTAL/zerohero"
  echo "Oude links (https://$OUDE_HOST/...) en de links in Telegram komen vanzelf op de goede plek uit."
  echo "n8n werkt zonder aanpassing door. De nachtelijke back-up (03:17) blijft zoals hij was."
  echo "Sleutels wijzigen:  bash $BM/app/deploy/zerohero-sleutel.sh"
  echo "Terug naar de oude situatie kan altijd:  bash $BM/app/deploy/overstap-zerohero.sh terug"
  exit 0
fi

# terug
if [ ! -f "$OVERGESTAPT" ]; then
  echo "== Alleen een proef gedaan: /zerohero uitzetten (de oude Doellijn is altijd blijven draaien)"
  $HULP leeg "$ZENV" ZEROHERO_DATABASE_URL
  buurmanitor_herstarten
  echo "KLAAR. De portal linkt weer naar https://$OUDE_HOST"; exit 0
fi
echo "== 1. /zerohero uit, de portal geeft de naam cockpit-app terug"
$HULP leeg "$ZENV" ZEROHERO_DATABASE_URL
zet_z ZEROHERO_ACHTERGROND 0
zet_env ZEROHERO_ALIAS ""
buurmanitor_herstarten
echo "== 2. Oude Doellijn starten"
profiel uit
oude_app up -d cockpit-app
echo "== 3. Caddy: $OUDE_HOST weer naar de oude Doellijn"
if [ -f "$CADDYBLOK" ]; then caddy_blok terug || echo "   LET OP: Caddy terugzetten lukte niet. Zet $COCKPIT/Caddyfile.bak-zerohero-* terug."; fi
rm -f "$OVERGESTAPT"
echo
echo "KLAAR. De Doellijn draait weer op https://$OUDE_HOST; n8n praat weer met de oude app."
