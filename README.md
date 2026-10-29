# Buurmanitor

De persoonlijke portal van Martijn Buurman: één login en één overzicht voor politiek, privé, ZEROHERO, FB/IM en
Upper Spire, met een team van zes persona's (Iris, Clara, Bram, Noor, Vera en Jesse). Het ontwerp staat in het
project Personal Assistant (ontwerp versie 0.6).

## Stand: fase 3 (Clara, agendabeheer)

- Inloggen via Caddy en Authelia (wachtwoord plus TOTP), met het proxygeheim en een exacte herkomstcontrole.
- Vandaag: meldingen, de werkruimtes Politiek en ZEROHERO, het team en de server.
- Politiek: Promonitor één op één onder `/politiek` (map `app/politiek`), in de huisstijl van de portal.
- ZEROHERO: de Doellijn één op één onder `/zerohero` (map `app/zerohero`), in de huisstijl van de portal. De
  achtergrondtaken (Google, MailerLite, YouTube, Meta, Threads, Ticket Tailor, WooCommerce, Claude-rekening en het
  weekplan van de adviseur) starten nu bij het opstarten, niet pas bij het eerste bezoek aan het dashboard.
- Iris, de coördinator, om mee te praten. Ze ziet in fase 0 alleen wat de portal zelf weet.
- Meldingen op drie niveaus, als banner en als systeemmelding op je scherm; urgent ook via Telegram (optioneel).
- Eén planner voor alle automatische taken, met een slot in PostgreSQL tegen dubbel draaien.
- Gezondheid: geheugen, schijf, back-ups, verbruik van Claude en de taken.

Huisstijl: Barlow en Barlow Condensed (zelf gehost, OFL), blauw #2563B8 met donkerblauw #14427E voor tekst.
Het logo (twee buurhuizen) is eigen werk.

- Clara (Agenda): al je agenda's in één week, met dubbele boekingen, krappe overgangen, open uitnodigingen en vrije
  blokken. Uitnodigingen beantwoorden en blokken inplannen kan, maar alleen na jouw klik. Praat met Clara over je agenda;
  stelt ze een tijdstip voor, dan verschijnt dat als knop.

Volgende fases: Bram (4), Noor (5), Vera (6) en Jesse (7).

## Opbouw

- `app/kern`: instellingen, database, toegang, planner, meldingen, Claude-client, gezondheid.
- `app/kern/prefix.py`: draait een werkruimte-app onder een voorvoegsel (/politiek) zonder de code ervan te
  veranderen; links, formulieren en doorverwijzingen krijgen het voorvoegsel erbij.
- `app/clara`: bronnen (Google, Microsoft 365, ICS, raadsagenda uit Notubiz), synchroniseren (planner-taak `agenda`,
  elke 5 minuten), analyse (pure functies), gesprek en schermen. Tokens en geheime agendalinks staan versleuteld in de
  database met `KLUIS_SLEUTEL` uit `.env`.
- `app/zerohero`: de Doellijn, met de database `doellijn` (die al in de PostgreSQL van de cockpit stond). Alle
  instellingen staan met `ZEROHERO_` ervoor in `~/buurmanitor/zerohero.env`. n8n roept `/intern/...` aan op
  `cockpit-app`; na de overstap draagt de portal die naam ook en geeft `/intern/...` door aan de werkruimte.
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
| Clara: kluis en apps | `bash ~/buurmanitor/app/deploy/clara.sh` |
| Sleutels ZEROHERO | `bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh claude` (of admin, google, mailerlite, meta, threads, tickettailor, youtube, wordpress) |
| Logboek | `cd ~/buurmanitor && docker compose logs --tail=100 app` |
| Back-up | elke nacht 03:37 in `~/buurmanitor/backups`, de laatste 60 |
| Back-up Politiek | elke nacht 03:27 in `~/promonitor/backups` (na de overstap uit de cockpit-database) |
| Back-up ZEROHERO | elke nacht 03:17 in `~/cockpit/backups`, ongewijzigd |

## Clara: apps bij Google en Microsoft

Clara koppelt met je eigen app bij Google en bij Microsoft. Die maak je één keer aan; daarna zet je de gegevens met
`bash ~/buurmanitor/app/deploy/clara.sh` op de server (de geheimen zie je niet terwijl je ze plakt).

**Google** (console.cloud.google.com, ingelogd met je Workspace-account van neighbour.nu):

1. Maak een project, bijvoorbeeld `Buurmanitor`, en zet bij API's en services de **Google Calendar API** aan.
2. Google Auth Platform › Branding: app-naam `Buurmanitor`, je eigen e-mailadres.
3. Audience: staan neighbour.nu en zerohero.one in **dezelfde** Workspace, kies **Internal**. Zijn het twee aparte
   Workspaces, kies **External** en zet de status op **In production** (niet Testing: dan vervalt de toegang na 7 dagen).
   Je ziet bij het koppelen dan een waarschuwing dat de app niet geverifieerd is; die kun je voor je eigen app negeren.
4. Data access: voeg de scopes `.../auth/calendar.readonly` en `.../auth/calendar.events` toe.
5. Clients › Create client › Web application. Authorized redirect URI:
   `https://buurmanitor.neighbour.nu/clara/koppel/google/terug`. Kopieer client-id en client-geheim.

**Microsoft** (entra.microsoft.com, ingelogd in een organisatie waar je beheerder bent, bijvoorbeeld IV-professional):

1. App-registraties › Nieuwe registratie. Naam `Buurmanitor`. Ondersteunde accounttypen: **Accounts in een
   organisatiemap (multitenant)**. Omleidings-URI (Web): `https://buurmanitor.neighbour.nu/clara/koppel/microsoft/terug`.
2. Verificatie: voeg nog een omleidings-URI toe: `https://buurmanitor.neighbour.nu/clara/koppel/microsoft/beheerder`.
3. Certificaten en geheimen › Nieuw clientgeheim (24 maanden; zet de vervaldatum in je agenda). Kopieer de **waarde**.
4. API-machtigingen › Microsoft Graph › Gedelegeerd: `Calendars.Read`, `Calendars.ReadWrite`, `User.Read`,
   `offline_access`, `openid`, `email`. Klik in je eigen organisatie op **Beheerderstoestemming verlenen**.
5. Kopieer bij Overzicht de **toepassings-id (client-id)**.

Bij andere organisaties (Upper Spire, gemeente Helmond) moet hun beheerder de app goedkeuren: Microsoft staat gebruikers
standaard niet meer toe om zelf agendatoegang aan een app te geven. Op Agenda › Koppelingen staat een link voor die
beheerder. Komt die toestemming er niet, gebruik dan een agendalink (ICS) of de raadsagenda uit Notubiz.

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

## Overstap van de Doellijn naar /zerohero

De database `doellijn` verhuist niet; alleen de app wisselt. Ook hier in twee stappen:

```
bash ~/buurmanitor/app/deploy/overstap-zerohero.sh proef
bash ~/buurmanitor/app/deploy/overstap-zerohero.sh overstap
```

- `proef` neemt de instellingen rechtstreeks over uit de container van de oude app (zonder dat ze in beeld komen) en
  zet /zerohero aan, zonder achtergrondtaken. Je werkt met de echte gegevens; n8n praat nog met de oude app.
- `overstap` stopt de oude app en geeft die in `~/cockpit/docker-compose.override.yml` het profiel `oud`, zodat een
  gewone `docker compose up -d` in `~/cockpit` hem niet meer start. De portal krijgt op het netwerk ook de naam
  `cockpit-app`, de achtergrondtaken gaan aan, vanuit de n8n-container wordt gecontroleerd dat `/intern` bij de
  portal uitkomt, en `cockpit.zerohero.one` stuurt door naar `/zerohero`. Lukt een stap niet, dan draait de oude
  Doellijn weer. De scripts in `~/cockpit/doellijn-app/deploy` stoppen daarna met een verwijzing hierheen.
- `terug` zet de oude Doellijn, het profiel en Caddy terug.

## Opruimen na beide overstappen

```
bash ~/buurmanitor/app/deploy/opruimen.sh
```

Controleert eerst dat Politiek en ZEROHERO in Buurmanitor werken en dat er een back-up van Politiek is, vraagt om
bevestiging (typ OPRUIMEN) en haalt dan weg: de oude Promonitor (containers, image, oude database en netwerk; eerst
nog een laatste back-up daarvan), de oude Doellijn-container en het image, en de build cache van Docker. Daarna
werkt `terug` in de overstapscripts niet meer. De databases in de cockpit en alle back-ups blijven staan.

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

Clara wordt getest met nagebootste antwoorden van Google, Microsoft en ICS; er is geen echt account nodig.
Met `TEST_DATABASE_URL`, `TEST_POLITIEK_DATABASE_URL` en `TEST_ZEROHERO_DATABASE_URL` naar drie lege, wegwerpbare
PostgreSQL-databases draaien ook de database-, scherm- en koppelingstests. `alles.sh` draait de portal, Politiek en de koppeling in aparte
processen, omdat ze bij het laden elk hun eigen instellingen uit de omgeving lezen. Voor ZEROHERO maakt de test zelf
de basistabellen aan uit `tests/zerohero/basis_schema.sql` (op de server bestaan die al).
