# Buurmanitor

De persoonlijke portal van Martijn Buurman: één login en één overzicht voor politiek, privé, ZEROHERO, FB/IM en
Upper Spire, met een team van zes persona's (Iris, Clara, Bram, Noor, Vera en Jesse). Het ontwerp staat in het
project Personal Assistant (ontwerp versie 0.6).

## Stand: fase 0

- Inloggen via Caddy en Authelia (wachtwoord plus TOTP), met het proxygeheim en een exacte herkomstcontrole.
- Vandaag: meldingen, werkruimtes (Promonitor en de Doellijn, nog als eigen app), het team en de server.
- Iris, de coördinator, om mee te praten. Ze ziet in fase 0 alleen wat de portal zelf weet.
- Meldingen op drie niveaus, als banner en als systeemmelding op je scherm; urgent ook via Telegram (optioneel).
- Eén planner voor alle automatische taken, met een slot in PostgreSQL tegen dubbel draaien.
- Gezondheid: geheugen, schijf, back-ups, verbruik van Claude en de taken.

Volgende fases: Promonitor (1) en de Doellijn (2) verhuizen naar /politiek en /zerohero; daarna Clara (3),
Bram (4), Noor (5), Vera (6) en Jesse (7).

## Opbouw

- `app/kern`: instellingen, database, toegang, planner, meldingen, Claude-client, gezondheid.
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

## Tests

```
pip install -r requirements.txt pytest httpx
python -m pytest tests
```

Met `TEST_DATABASE_URL` naar een lege, wegwerpbare PostgreSQL-database draaien ook de database- en schermtests.
