# Buurmanitor

De persoonlijke portal van Martijn Buurman: één login en één overzicht voor politiek, privé, ZEROHERO, FB/IM en
Upper Spire, met een team van zes persona's (Iris, Clara, Bram, Noor, Vera en Jesse). Het ontwerp staat in het
project Personal Assistant (ontwerp versie 0.6).

## Stand: fase 1 (Promonitor als werkruimte Politiek)

- Inloggen via Caddy en Authelia (wachtwoord plus TOTP), met het proxygeheim en een exacte herkomstcontrole.
- Vandaag: meldingen, werkruimtes (Politiek onder /politiek, de Doellijn nog als eigen app), het team en de server.
- Politiek: Promonitor één op één onder `/politiek` (map `app/politiek`), in de huisstijl van de portal.
- Iris, de coördinator, om mee te praten. Ze ziet in fase 0 alleen wat de portal zelf weet.
- Meldingen op drie niveaus, als banner en als systeemmelding op je scherm; urgent ook via Telegram (optioneel).
- Eén planner voor alle automatische taken, met een slot in PostgreSQL tegen dubbel draaien.
- Gezondheid: geheugen, schijf, back-ups, verbruik van Claude en de taken.

Huisstijl: Barlow en Barlow Condensed (zelf gehost, OFL), blauw #2563B8 met donkerblauw #14427E voor tekst.
Het logo (twee buurhuizen) is eigen werk.

Volgende fases: de Doellijn verhuist naar /zerohero (2); daarna Clara (3), Bram (4), Noor (5), Vera (6) en Jesse (7).

## Opbouw

- `app/kern`: instellingen, database, toegang, planner, meldingen, Claude-client, gezondheid.
- `app/kern/prefix.py`: draait een werkruimte-app onder een voorvoegsel (/politiek) zonder de code ervan te
  veranderen; links, formulieren en doorverwijzingen krijgen het voorvoegsel erbij.
- `app/politiek`: Promonitor, met een eigen database (`promonitor`, ook in de PostgreSQL van de cockpit) en eigen
  instellingen met `POLITIEK_` ervoor (eigen Claude-sleutel, Telegram en mail).
- `app/personas/*.yaml`: de beschrijving van elke persona. Aanpassen en `bijwerken.sh` draaien is genoeg.
- `app/schema.py`: tabellen van de portal; migraties draaien bij elke start.
- `deploy/`: installeren, bijwerken, back-up en sleutels.
- De database `buurmanitor` draait in de bestaande PostgreSQL 17 van de cockpit; er is geen extra databasecontainer.

## Installeren op de server (eenmalig)

1. Een alleen-lezen deploy-sleutel voor deze repository maken:

   ```
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/buurmanitor_deploy -C buurmanitor-server
   printf '\nHost github-buurmanitor\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/buurmanitor_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
   cat ~/.ssh/buurmanitor_deploy.pub
   ```

   Zet de getoonde regel op GitHub bij deze repository onder Settings, Deploy keys (zonder schrijfrechten).

2. De code ophalen en installeren:

   ```
   mkdir -p ~/buurmanitor
   git clone github-buurmanitor:Neighbour-nu/buurmanitor.git ~/buurmanitor/app
   bash ~/buurmanitor/app/deploy/installeer.sh
   bash ~/buurmanitor/app/deploy/sleutel.sh
   ```

   `installeer.sh` controleert de DNS, maakt de database, start de app, voegt de portal toe aan Authelia en Caddy
   (met controle en terugzetten bij een fout) en zet de nachtelijke back-up klaar (03:37).
   `sleutel.sh` vraagt verborgen om de Claude-sleutel (werkruimte Assistent) en optioneel Telegram.

## Beheer

| Wat | Hoe |
|---|---|
| Nieuwe versie | `bash ~/buurmanitor/app/deploy/bijwerken.sh` |
| Sleutels wijzigen | `bash ~/buurmanitor/app/deploy/sleutel.sh` |
| Logboek | `cd ~/buurmanitor && docker compose logs --tail=100 app` |
| Back-up | elke nacht 03:37 in `~/buurmanitor/backups`, de laatste 60 |
| Back-up Politiek | elke nacht 03:27 in `~/promonitor/backups` (na de overstap uit de cockpit-database) |

## Overstap van Promonitor naar /politiek

Na `bijwerken.sh` staat de code klaar, maar draait Promonitor nog gewoon. De overstap gaat in twee stappen:

```
bash ~/buurmanitor/app/deploy/overstap-politiek.sh proef
bash ~/buurmanitor/app/deploy/overstap-politiek.sh overstap
```

- `proef` kopieert de database naar de cockpit en zet /politiek aan, zonder automatische taken. Promonitor blijft
  de echte; wat je in de proef verandert, gaat bij de overstap verloren. Opnieuw draaien geeft een verse kopie.
- `overstap` stopt Promonitor, maakt een laatste kopie, vergelijkt per tabel het aantal rijen, zet de automatische
  taken in /politiek aan, stuurt `promonitor.neighbour.nu` door naar `/politiek` (oude links blijven werken), laat
  de nachtelijke back-up uit de cockpit-database komen en zet de oude database stil. Lukt een stap niet, dan gaat
  alles terug naar Promonitor.
- `terug` zet alles terug zoals het was, inclusief wat je intussen in /politiek hebt gedaan.

Na de overstap niet meer `~/promonitor/app/deploy/bijwerken.sh` draaien. De oude database (volume van
`~/promonitor`) blijft bewaard voor `terug`; opruimen kan als alles een paar weken goed draait.

## Back-up buiten de server

Elke nacht om 04:15 gaat een versleutelde kopie naar OVHcloud Object Storage (met restic): de databasedumps van
alle drie de apps, een volledige dump van de PostgreSQL van de cockpit (ook Miniflux), de n8n-gegevens, de
.env-bestanden, de Caddyfile, de docker-compose-bestanden en de Authelia-configuratie. Bewaard: 14 dagen, 8 weken,
12 maanden. De stand staat op de pagina Gezondheid; het logboek in `/var/log/buurmanitor-extern.log`.

Instellen (eenmalig):

1. In het beheerscherm van OVHcloud: Public Cloud, je project (of maak er een aan), Object Storage,
   Create Object Container. Kies Standard, een andere regio dan de server (bijvoorbeeld Frankfurt, `de`) en maak bij
   de stap gebruiker een nieuwe Object Storage-gebruiker aan. Noteer bij Object Storage users de access key en
   secret key.
2. Op de server: `bash ~/buurmanitor/app/deploy/extern.sh`. Het script vraagt regio, bucket en sleutels, maakt een
   wachtwoord voor de versleuteling (bewaar dat in je wachtwoordmanager), maakt de eerste kopie en zet de nachtelijke
   taak klaar.

Terugzetten (bijvoorbeeld op een nieuwe server, met het wachtwoord uit je wachtwoordmanager):

```
sudo apt install restic
export RESTIC_REPOSITORY=s3:https://s3.de.io.cloud.ovh.net/<bucket> AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
restic snapshots                      # vraagt om het wachtwoord
restic restore latest --target ~/terug
```

## Tests

```
pip install -r requirements.txt pytest httpx
bash tests/alles.sh
```

Met `TEST_DATABASE_URL` en `TEST_POLITIEK_DATABASE_URL` naar twee lege, wegwerpbare PostgreSQL-databases draaien
ook de database-, scherm- en koppelingstests. `alles.sh` draait de portal, Politiek en de koppeling in aparte
processen, omdat ze bij het laden elk hun eigen instellingen uit de omgeving lezen.
