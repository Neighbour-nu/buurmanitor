"""Databaseschema en startgegevens van Promonitor.

Migraties draaien bij elke start en zijn veilig om opnieuw uit te voeren (IF NOT EXISTS).
"""

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS instellingen (
        sleutel text PRIMARY KEY,
        waarde text,
        uitleg text,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS personen (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        whaller text,
        rol text,
        portefeuilles text,
        volgorde int NOT NULL DEFAULT 50,
        actief boolean NOT NULL DEFAULT true)""",
    """CREATE TABLE IF NOT EXISTS documenten (
        id serial PRIMARY KEY,
        titel text NOT NULL,
        soort text NOT NULL,
        pad text UNIQUE,
        tekst text NOT NULL,
        woorden int,
        bron_bestand text,
        vertrouwelijk boolean NOT NULL DEFAULT false,
        toegevoegd timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now(),
        zoek tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector('dutch', coalesce(titel, '')), 'A') ||
            setweight(to_tsvector('dutch', left(tekst, 900000)), 'B')) STORED)""",
    "CREATE INDEX IF NOT EXISTS documenten_zoek ON documenten USING gin (zoek)",
    """CREATE TABLE IF NOT EXISTS eenmalig (
        naam text PRIMARY KEY,
        uitgevoerd timestamptz NOT NULL DEFAULT now())""",
    # Wie spreekt bij welk agendapunt (commissie) of in de raad. sleutel = genormaliseerd onderwerp.
    """CREATE TABLE IF NOT EXISTS toewijzingen (
        id serial PRIMARY KEY,
        soort text NOT NULL,
        datum date NOT NULL,
        sleutel text NOT NULL,
        onderwerp text NOT NULL,
        woordvoerder text NOT NULL DEFAULT '',
        zit_erbij text NOT NULL DEFAULT '',
        bijgewerkt timestamptz NOT NULL DEFAULT now(),
        UNIQUE (soort, datum, sleutel))""",
    # Wat Martijn per Whaller-bericht invult (fractiepunten, bijzondere punten, links).
    """CREATE TABLE IF NOT EXISTS whaller_invoer (
        sleutel text PRIMARY KEY,
        gegevens jsonb NOT NULL DEFAULT '{}',
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # Alles wat binnenkomt: raadsinformatie uit Notubiz, later ook nieuws. extern_id is uniek per bron.
    """CREATE TABLE IF NOT EXISTS stukken (
        id serial PRIMARY KEY,
        bron text NOT NULL,
        extern_id text NOT NULL UNIQUE,
        soort text NOT NULL,
        titel text NOT NULL,
        nummer text NOT NULL DEFAULT '',
        datum date,
        gewijzigd timestamp,
        fracties text NOT NULL DEFAULT '',
        document_url text NOT NULL DEFAULT '',
        document_id bigint,
        context text NOT NULL DEFAULT '',
        pro boolean NOT NULL DEFAULT false,
        gezien boolean NOT NULL DEFAULT false,
        status text NOT NULL DEFAULT '',
        tekst_id int,
        tekst_fout text,
        toegevoegd timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz)""",
    "CREATE INDEX IF NOT EXISTS stukken_datum ON stukken (datum DESC)",
    "ALTER TABLE stukken ADD COLUMN IF NOT EXISTS bronnaam text NOT NULL DEFAULT ''",
    "ALTER TABLE stukken ADD COLUMN IF NOT EXISTS score int",
    "ALTER TABLE stukken ADD COLUMN IF NOT EXISTS reden text",
    "ALTER TABLE stukken ADD COLUMN IF NOT EXISTS onderwerp text",
    "ALTER TABLE stukken ADD COLUMN IF NOT EXISTS actie text",
    "ALTER TABLE stukken ADD COLUMN IF NOT EXISTS negeer_reden text",
    "ALTER TABLE stukken ADD COLUMN IF NOT EXISTS extra jsonb",
    """CREATE TABLE IF NOT EXISTS dossiers (
        id serial PRIMARY KEY,
        naam text NOT NULL UNIQUE,
        zoektermen text,
        standpunt text,
        notitie text,
        woordvoerder text,
        status text NOT NULL DEFAULT 'actief',
        inzicht jsonb,
        analyse_status text,
        analyse_fout text,
        analyse_tijd timestamptz,
        model text,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS stemadviezen (
        id serial PRIMARY KEY,
        notubiz_id int NOT NULL UNIQUE,
        datum date NOT NULL,
        titel text,
        url text,
        items jsonb NOT NULL DEFAULT '[]',
        toelichting text,
        status text NOT NULL DEFAULT 'leeg',
        model text,
        fout text,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS begrotingsanalyses (
        id serial PRIMARY KEY,
        titel text,
        thema text NOT NULL,
        vraag text,
        documenten jsonb NOT NULL DEFAULT '[]',
        inzicht jsonb,
        status text NOT NULL DEFAULT 'bezig',
        model text,
        fout text,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS notulen (
        id serial PRIMARY KEY,
        datum date NOT NULL,
        status text NOT NULL DEFAULT 'bezig',
        plaats text,
        aanwezig jsonb NOT NULL DEFAULT '[]',
        afwezig jsonb NOT NULL DEFAULT '[]',
        voorzitter text,
        notulist text,
        bron_naam text,
        transcript text,
        agenda text,
        aanwijzingen text,
        inhoud jsonb,
        model text,
        fout text,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS fractie_acties (
        id serial PRIMARY KEY,
        notulen_id int REFERENCES notulen(id) ON DELETE SET NULL,
        punt text,
        actie text NOT NULL,
        wie text,
        wanneer text,
        status text NOT NULL DEFAULT 'open',
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        afgerond timestamptz)""",
    "ALTER TABLE fractie_acties ADD COLUMN IF NOT EXISTS bewerkt boolean NOT NULL DEFAULT false",
    "ALTER TABLE fractie_acties ADD COLUMN IF NOT EXISTS wacht_op text",
    "ALTER TABLE fractie_acties ALTER COLUMN status SET DEFAULT 'nieuw'",
    "UPDATE fractie_acties SET status = 'nieuw' WHERE status = 'open'",
    "UPDATE fractie_acties SET status = 'afgerond' WHERE status = 'klaar'",
    "ALTER TABLE fractie_acties ADD COLUMN IF NOT EXISTS gewijzigd timestamptz NOT NULL DEFAULT now()",
    """CREATE TABLE IF NOT EXISTS agenda_voortgang (
        sleutel text PRIMARY KEY,
        onderwerp text NOT NULL,
        gedaan jsonb NOT NULL DEFAULT '[]',
        nvt jsonb NOT NULL DEFAULT '[]',
        notitie text,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS stukken_bron_score ON stukken (bron, score)",
    """CREATE TABLE IF NOT EXISTS bronnen (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        url text NOT NULL UNIQUE,
        soort text NOT NULL DEFAULT 'feed',
        feed_url text,
        actief boolean NOT NULL DEFAULT true,
        laatst timestamptz,
        status text,
        fout text)""",
    """CREATE TABLE IF NOT EXISTS claude_gebruik (
        id serial PRIMARY KEY,
        tijd timestamptz NOT NULL DEFAULT now(),
        onderdeel text NOT NULL,
        model text NOT NULL,
        invoer int NOT NULL DEFAULT 0,
        uitvoer int NOT NULL DEFAULT 0,
        kosten_eur numeric(10, 5) NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS concepten (
        id serial PRIMARY KEY,
        soort text NOT NULL,
        titel text NOT NULL DEFAULT '',
        bron jsonb,
        invoer jsonb,
        status text NOT NULL DEFAULT 'bezig',
        tekst text,
        velden jsonb,
        meta jsonb,
        model text,
        fout text,
        ouder_id int,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # Pas na CREATE TABLE concepten: op een lege database bestaat de tabel anders nog niet.
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS origineel bytea",
    "ALTER TABLE concepten ADD COLUMN IF NOT EXISTS origineel_naam text",
    """CREATE TABLE IF NOT EXISTS gesprekken (
        id serial PRIMARY KEY,
        titel text NOT NULL DEFAULT '',
        rol text NOT NULL DEFAULT 'strateeg',
        status text NOT NULL DEFAULT 'bezig',
        fout text,
        achtergrond text,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    "ALTER TABLE gesprekken ADD COLUMN IF NOT EXISTS vertrouwelijk boolean NOT NULL DEFAULT false",
    """CREATE TABLE IF NOT EXISTS gesprek_berichten (
        id serial PRIMARY KEY,
        gesprek_id int NOT NULL,
        rol text NOT NULL,
        tekst text NOT NULL,
        tijd timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS taken (
        naam text PRIMARY KEY,
        laatst timestamptz,
        resultaat text,
        fout text)""",
    # Geüploade versies van de griffie-formats; zonder rij geldt de standaard uit app/sjablonen/.
    """CREATE TABLE IF NOT EXISTS sjablonen (
        soort text PRIMARY KEY,
        bestandsnaam text NOT NULL,
        inhoud bytea NOT NULL,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
]

# Fractie en steunfractie, met Whaller-namen uit de berichten op Whaller (september en oktober 2026).
# Rollen en portefeuilles zijn aan te vullen op het scherm Instellingen.
STANDAARD_PERSONEN = [
    {"naam": "Murat Yalcin", "whaller": "murat_yalcin", "rol": "raadslid, fractievoorzitter", "portefeuilles": "asiel, migratie", "volgorde": 10},
    {"naam": "Martijn Buurman", "whaller": "martijn_buurman", "rol": "raadslid", "portefeuilles": "digitalisering, pleegzorg, mantelzorg", "volgorde": 11},
    {"naam": "Bert van Sas", "whaller": "bert_van_sas", "rol": "raadslid", "portefeuilles": "milieu, luchtkwaliteit, ruimtelijke ordening, openbare ruimte", "volgorde": 12},
    {"naam": "Jeanette Hendriks", "whaller": "JeanetteHendriks", "rol": "raadslid", "portefeuilles": "afval, circulaire economie", "volgorde": 13},
    {"naam": "Niek Engbers", "whaller": "n.engbers", "rol": "raadslid", "portefeuilles": "", "volgorde": 14},
    {"naam": "Sanae Yahia", "whaller": "SanaeYahia", "rol": "raadslid", "portefeuilles": "cultuur, integratie", "volgorde": 15},
    {"naam": "Petra Verdonk", "whaller": "PetraVerdonk", "rol": "burgercommissielid", "portefeuilles": "", "volgorde": 30},
    {"naam": "Stijn van Hoek", "whaller": "StijnvanHoek", "rol": "burgercommissielid", "portefeuilles": "", "volgorde": 31},
    {"naam": "Deliana", "whaller": "Deliana", "rol": "burgercommissielid", "portefeuilles": "", "volgorde": 32},
]

# Woordvoerders uit de Whaller-berichten van september en oktober 2026: (soort, datum, onderwerp, woordvoerder, zit erbij).
HISTORIE_TOEWIJZINGEN = [
    ("commissie", "2026-09-01", "Verordening behandeling bezwaarschriften Helmond 2023 - Invoeren ambtelijk horen in de organisatie", "martijn_buurman", "n.engbers"),
    ("commissie", "2026-09-01", "Thema brede Informatiesessie Veiligheid & Naleving", "martijn_buurman", "murat_yalcin"),
    ("commissie", "2026-09-08", "Zienswijze wijziging investeringsbegroting herhuisvesting Blink", "Deliana", "SanaeYahia"),
    ("commissie", "2026-09-08", "Omgevingsplan Dijksestraat", "n.engbers", "PetraVerdonk"),
    ("commissie", "2026-09-08", "Omgevingsplan 'Ensemble' behandeling", "n.engbers", "PetraVerdonk"),
    ("commissie", "2026-09-08", "Wijzigen GREX Mierloseweg 210-232", "martijn_buurman", "murat_yalcin"),
    ("commissie", "2026-09-08", "Automotive Campus - Aankoop CSM en Businessplan Doorontwikkeling AC", "murat_yalcin", "martijn_buurman"),
    ("commissie", "2026-09-08", "Voorkeursrecht Oostzijde Automotive Campus", "murat_yalcin", "martijn_buurman"),
    ("raad", "2026-09-23", "Verordening behandeling bezwaarschriften Helmond 2023 - Invoeren ambtelijk horen in de organisatie", "martijn_buurman", ""),
    ("raad", "2026-09-23", "Zienswijze wijziging investeringsbegroting herhuisvesting Blink", "JeanetteHendriks", ""),
    ("raad", "2026-09-23", "Omgevingsplan Dijksestraat", "n.engbers", ""),
    ("raad", "2026-09-23", "Wijzigen GREX Mierloseweg 210-232", "martijn_buurman", ""),
    ("raad", "2026-09-23", "Automotive Campus", "murat_yalcin", ""),
    ("commissie", "2026-10-06", "Afwegingskader sportaccommodaties", "martijn_buurman", "murat_yalcin"),
    ("commissie", "2026-10-06", "Fusie onderwijsgroep Oost Brabant (OGOB)", "n.engbers", "PetraVerdonk"),
    ("commissie", "2026-10-06", "Participatieverordening", "StijnvanHoek", "bert_van_sas"),
    ("commissie", "2026-10-06", "Vaststellen Assetsheets 2027-2031", "JeanetteHendriks", "Deliana"),
    ("commissie", "2026-10-06", "Bouwplan Baroniehof 75 appartementen", "martijn_buurman", "StijnvanHoek"),
    ("commissie", "2026-10-13", "Verlengingsvoorstel Regionaal Veiligheidsplan Oost-Brabant 2027-2028", "murat_yalcin", "martijn_buurman"),
    ("commissie", "2026-10-13", "Grondexploitatie Woonzorg zone Helmond Oost (Generaal Snijderstraat)", "StijnvanHoek", "martijn_buurman"),
    ("commissie", "2026-10-13", "Verzoek raadsadvies omgevingsvergunning Winteropvang Daklozen 2026", "JeanetteHendriks", "SanaeYahia"),
    ("raad", "2026-10-27", "Afwegingskader sportaccommodaties", "martijn_buurman", ""),
    ("raad", "2026-10-27", "Fusie onderwijsgroep Oost Brabant (OGOB)", "n.engbers", ""),
    ("raad", "2026-10-27", "Participatieverordening", "bert_van_sas", ""),
    ("raad", "2026-10-27", "Grondexploitatie Woonzorg zone Helmond Oost (Generaal Snijderstraat)", "martijn_buurman", ""),
]


def _historie():
    from .whaller import sleutel  # pylint: disable=import-outside-toplevel
    return [("""INSERT INTO toewijzingen (soort, datum, sleutel, onderwerp, woordvoerder, zit_erbij)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
             (soort, datum, sleutel(onderwerp), onderwerp, wv, erbij))
            for soort, datum, onderwerp, wv, erbij in HISTORIE_TOEWIJZINGEN]


def _bronnen():
    from .nieuws import STANDAARD_BRONNEN  # pylint: disable=import-outside-toplevel
    return [("INSERT INTO bronnen (naam, url, soort) VALUES (%s, %s, %s) ON CONFLICT (url) DO NOTHING", b)
            for b in STANDAARD_BRONNEN]


# Eenmalige correcties op bestaande gegevens; elke naam wordt maar één keer uitgevoerd,
# zodat latere wijzigingen op het scherm Instellingen blijven staan.
EENMALIG = [
    ("rollen-2026-10-26", [
        # Opgegeven door Martijn op 26 oktober 2026: zes raadsleden, drie burgercommissieleden.
        "UPDATE personen SET rol = 'raadslid, fractievoorzitter', volgorde = 10 WHERE naam = 'Murat Yalcin'",
        "UPDATE personen SET rol = 'raadslid', volgorde = 11 WHERE naam = 'Martijn Buurman'",
        "UPDATE personen SET rol = 'raadslid', volgorde = 12 WHERE naam = 'Bert van Sas'",
        "UPDATE personen SET rol = 'raadslid', volgorde = 13 WHERE naam = 'Jeanette Hendriks'",
        "UPDATE personen SET rol = 'raadslid', volgorde = 14 WHERE naam = 'Niek Engbers'",
        "UPDATE personen SET rol = 'raadslid', volgorde = 15 WHERE naam = 'Sanae Yahia'",
        "UPDATE personen SET rol = 'burgercommissielid', volgorde = 30 WHERE naam = 'Petra Verdonk'",
        "UPDATE personen SET rol = 'burgercommissielid', volgorde = 31 WHERE naam = 'Stijn van Hoek'",
        "UPDATE personen SET rol = 'burgercommissielid', volgorde = 32 WHERE naam = 'Deliana'",
        "UPDATE personen SET rol = '', volgorde = 50 WHERE naam = 'Thomas Tuerlings'",
    ]),
    ("thomas-inactief-2026-10-26", [
        # Thomas hoort niet meer bij de fractie (Martijn, 26 oktober 2026).
        "UPDATE personen SET actief = false WHERE naam = 'Thomas Tuerlings'",
    ]),
    ("woordvoerders-historie-2026-10-26", _historie()),
    ("nieuwsbronnen-2026-10-26", _bronnen()),
    ("oude-raadsstukken-gezien-2026-10-26", [
        # De raadsvragen kwamen de eerste keer niet mee (time-out) en daarna allemaal als nieuw binnen.
        """UPDATE stukken SET gezien = true WHERE bron = 'notubiz' AND NOT gezien
             AND soort IN ('vraag', 'motie', 'toezegging', 'ingekomen', 'rib')
             AND (datum IS NULL OR datum < current_date - 14)""",
    ]),
    ("bronnen-herstel-2026-10-26", [
        # Helmond Actueel bestaat niet onder dat adres; Binnenlands Bestuur heeft geen openbare feed.
        "UPDATE bronnen SET actief = false, fout = 'Uitgezet: deze website bestaat niet onder dit adres.' WHERE url = 'https://www.helmondactueel.nl/feed/'",
        """UPDATE bronnen SET naam = 'Google: Binnenlands Bestuur', soort = 'google', feed_url = NULL, fout = NULL,
             url = 'site:binnenlandsbestuur.nl gemeente OR gemeenten OR raadsleden'
           WHERE url = 'https://www.binnenlandsbestuur.nl/rss'""",
    ]),
    ("whaller-petra-2026-10-26", [
        # In Whaller is het @PetraVerdonk (zo staat het in de berichten van september en oktober).
        "UPDATE personen SET whaller = 'PetraVerdonk' WHERE whaller = 'Petra-Verdonk'",
    ]),
]

# sleutel: (standaardwaarde, uitleg). Bestaande waarden worden nooit overschreven.
# De volgorde hier is ook de volgorde op het scherm Instellingen.
STANDAARD_INSTELLINGEN = {
    "fractie_dag": ("maandag", "Vaste dag van de fractievergadering"),
    "fractie_tijd": ("19.00", "Begintijd van de fractievergadering"),
    "fractie_locatie": ("Huis voor de Stad, ruimte De Fabriek", "Locatie van het Whaller-event bij de fractievergadering"),
    "raad_dag": ("dinsdag", "Gewone dag van commissie en raad (bij een andere dag komt er een rode waarschuwing in het Whaller-bericht)"),
    "raad_tijd": ("19.30-22.30", "Tijd van het Whaller-event bij commissie en raad"),
    "raad_locatie": ("Huis voor de Stad", "Locatie van het Whaller-event bij commissie en raad"),
    "raad_notubiz": ("https://helmond.notubiz.nl", "Notubiz van de gemeente Helmond"),
    "agenda_ik": ("martijn_buurman", "Jouw Whaller-naam: daarmee herkent Promonitor jouw agendapunten op het scherm Agenda"),
    "agenda_technische_vragen_dagen": ("7", "Deadline technische vragen: aantal dagen vóór de commissievergadering (0 = geen deadline tonen)"),
    "briefing_tijd": ("08.00", "Tijd van de ochtendbriefing (Telegram en mail)"),
    "briefing_drempel": ("60", "Minimale score van een nieuwsbericht om in de briefing te komen"),
    "claude_model_weging": ("claude-haiku-4-5", "Claude-model voor het wegen van nieuws (klein en goedkoop)"),
    "claude_model_schrijven": ("claude-sonnet-5-5", "Claude-model voor bijdragen, concepten en de adviseur"),
    "claude_dagplafond_schrijven_eur": ("3.00", "Maximaal per dag aan schrijven en de adviseur, in euro"),
    "claude_dagplafond_eur": ("1.00", "Maximaal per dag aan het wegen van nieuws, in euro; daarboven wacht het wegen tot morgen"),
    "whaller_plaatsdag": ("woensdag", "Dag waarop de berichten voor de week erna klaarstaan"),
    # Rode sfeer: box "2. Raads- en commissievergaderingen (2026-30)"
    "rood_sfeer": ("rode sfeer", "Naam van de sfeer"),
    "rood_box": ("", "Box '2. Raads- en commissievergaderingen (2026-30)'"),
    "rood_map_commissie": ("", "Map 'Commissievergaderingen' (hierin per vergadering een map jjjj-mm-dd voor de bijdragen)"),
    "rood_map_raad": ("", "Map 'Raadsvergaderingen' (hierin per vergadering een map jjjj-mm-dd)"),
    "rood_map_begroting": ("", "Map voor de begrotingsbehandeling (nu 'Begroting 2026')"),
    "rood_map_sjablonen": ("", "Map 'Sjablonen en logo's'"),
    # Groene sfeer: box "1. Fractievergaderingen (2026-30)"
    "groen_sfeer": ("groene sfeer", "Naam van de sfeer"),
    "groen_box": ("", "Box '1. Fractievergaderingen (2026-30)'"),
    "groen_map_notulen": ("", "Map 'Notulen fractievergaderingen' (link bij agendapunt 2)"),
    "groen_map_agenda": ("", "Map 'Agenda en bijeenkomsten' (link bij agendapunt 7)"),
    "groen_doc_activiteiten": ("", "Document 'Activiteiten_Helmond_Fractie', als dat een los document is"),
    "groen_map_besluiten": ("", "Map 'Besluitenlijst en actielijst'"),
    "groen_map_raadsvragen": ("", "Map 'Raadsvragen'"),
    "groen_map_strategie": ("", "Map 'Strategie'"),
    "groen_map_columns": ("", "Map 'Columns'"),
    "groen_map_pers": ("", "Map 'Pers en social media berichten'"),
    "groen_formulier": ("", "Formulier om een agendapunt voor de fractievergadering in te brengen"),
}

# Deze links staan in de vaste Whaller-berichten; Vandaag meldt het als ze nog leeg zijn.
WHALLER_NODIG = ["rood_map_commissie", "rood_map_raad", "groen_map_notulen", "groen_map_agenda", "groen_formulier"]

# Oude sleutels uit de eerste versie (25 oktober 2026): waarde gaat mee naar de nieuwe sleutel.
HERNOEMD = {
    "sfeer_raad": "rood_sfeer",
    "sfeer_fractie": "groen_sfeer",
    "link_map_raad_commissie": "rood_box",
    "link_activiteiten": "groen_doc_activiteiten",
    "link_agenda_bijeenkomsten": "groen_map_agenda",
    "link_agendaformulier": "groen_formulier",
    "link_notubiz": "raad_notubiz",
}
for _oud, _nieuw in HERNOEMD.items():
    MIGRATIES.append(
        f"UPDATE instellingen SET sleutel = '{_nieuw}' WHERE sleutel = '{_oud}' "
        f"AND NOT EXISTS (SELECT 1 FROM instellingen WHERE sleutel = '{_nieuw}')")
    MIGRATIES.append(f"DELETE FROM instellingen WHERE sleutel = '{_oud}'")
