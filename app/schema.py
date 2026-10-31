"""Databaseschema van Buurmanitor (de kern en de assistent).

Politiek (Promonitor) en ZEROHERO (de Doellijn) hebben hun eigen database; die tabellen staan hier niet.
Migraties draaien bij elke start en zijn veilig om opnieuw uit te voeren.
"""

MIGRATIES = [
    """CREATE TABLE IF NOT EXISTS instellingen (
        sleutel text PRIMARY KEY,
        waarde text,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # Planner: één regel per taak. bezig_sinds wordt bij het opstarten leeggemaakt.
    """CREATE TABLE IF NOT EXISTS taken (
        naam text PRIMARY KEY,
        laatst timestamptz,
        resultaat text,
        fout text,
        bezig_sinds timestamptz)""",
    # Meldingen: urgent, belangrijk of normaal. sleutel voorkomt dat dezelfde melding twee keer komt.
    """CREATE TABLE IF NOT EXISTS meldingen (
        id serial PRIMARY KEY,
        tijd timestamptz NOT NULL DEFAULT now(),
        niveau text NOT NULL CHECK (niveau IN ('urgent', 'belangrijk', 'normaal')),
        bron text NOT NULL,
        titel text NOT NULL,
        tekst text NOT NULL DEFAULT '',
        link text NOT NULL DEFAULT '',
        sleutel text UNIQUE,
        gelezen boolean NOT NULL DEFAULT false)""",
    "CREATE INDEX IF NOT EXISTS meldingen_open ON meldingen (gelezen, id DESC)",
    # Verbruik van Claude, per aanroep.
    """CREATE TABLE IF NOT EXISTS claude_gebruik (
        id serial PRIMARY KEY,
        tijd timestamptz NOT NULL DEFAULT now(),
        persona text NOT NULL,
        model text NOT NULL,
        invoer int NOT NULL DEFAULT 0,
        uitvoer int NOT NULL DEFAULT 0,
        kosten_eur numeric(10, 5) NOT NULL DEFAULT 0)""",
    # Gesprekken met de persona's.
    """CREATE TABLE IF NOT EXISTS gesprekken (
        id serial PRIMARY KEY,
        persona text NOT NULL,
        titel text NOT NULL DEFAULT '',
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS gesprek_berichten (
        id serial PRIMARY KEY,
        gesprek_id int NOT NULL REFERENCES gesprekken(id) ON DELETE CASCADE,
        rol text NOT NULL CHECK (rol IN ('user', 'assistant')),
        tekst text NOT NULL,
        tijd timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS gesprek_berichten_gesprek ON gesprek_berichten (gesprek_id, id)",
    # Gesprekken kun je archiveren (uit de lijst, wel terug te halen) of verwijderen.
    "ALTER TABLE gesprekken ADD COLUMN IF NOT EXISTS gearchiveerd boolean NOT NULL DEFAULT false",
    # Logboek: wat een persona deed of voorstelde, en wat Martijn besloot.
    """CREATE TABLE IF NOT EXISTS logboek (
        id serial PRIMARY KEY,
        tijd timestamptz NOT NULL DEFAULT now(),
        persona text NOT NULL,
        actie text NOT NULL,
        details jsonb)""",
    # ---------- Clara (fase 3) ----------
    # Een koppeling is één account of agendalink. geheim: versleuteld refresh-token (Google, Microsoft) of de geheime
    # ICS-link. agendas: per agenda id, naam, aan/uit en domein. Afspraken zelf blijven in Google en Outlook leidend;
    # agenda_items is een kopie van het venster dat Clara bekijkt.
    """CREATE TABLE IF NOT EXISTS agenda_koppelingen (
        id serial PRIMARY KEY,
        soort text NOT NULL CHECK (soort IN ('google', 'microsoft', 'ics', 'raad')),
        naam text NOT NULL,
        account text NOT NULL DEFAULT '',
        tenant text NOT NULL DEFAULT '',
        domein text NOT NULL DEFAULT 'prive',
        geheim text,
        schrijven boolean NOT NULL DEFAULT false,
        agendas jsonb NOT NULL DEFAULT '[]',
        schrijfagenda text NOT NULL DEFAULT '',
        actief boolean NOT NULL DEFAULT true,
        laatst timestamptz,
        fout text,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS agenda_items (
        id bigserial PRIMARY KEY,
        koppeling_id int NOT NULL REFERENCES agenda_koppelingen(id) ON DELETE CASCADE,
        agenda_id text NOT NULL DEFAULT '',
        extern_id text NOT NULL,
        domein text NOT NULL DEFAULT '',
        ical_uid text NOT NULL DEFAULT '',
        titel text NOT NULL DEFAULT '',
        locatie text NOT NULL DEFAULT '',
        start timestamptz NOT NULL,
        eind timestamptz NOT NULL,
        heledag boolean NOT NULL DEFAULT false,
        status text NOT NULL DEFAULT 'bevestigd',
        vrij boolean NOT NULL DEFAULT false,
        antwoord text NOT NULL DEFAULT 'eigen',
        organisator text NOT NULL DEFAULT '',
        online boolean NOT NULL DEFAULT false,
        link text NOT NULL DEFAULT '',
        geschat boolean NOT NULL DEFAULT false,
        bijgewerkt timestamptz NOT NULL DEFAULT now(),
        UNIQUE (koppeling_id, agenda_id, extern_id))""",
    "CREATE INDEX IF NOT EXISTS agenda_items_start ON agenda_items (start)",
    # Begin van de omschrijving, voor verbergregels (bijvoorbeeld de naam van een Whaller-sphere).
    "ALTER TABLE agenda_items ADD COLUMN IF NOT EXISTS omschrijving text NOT NULL DEFAULT ''",
    # Eigen kleur van een afspraak (Google: colorId), zoals in Google Agenda.
    "ALTER TABLE agenda_items ADD COLUMN IF NOT EXISTS kleur text NOT NULL DEFAULT ''",
    # Reistijd: gevonden adressen (ook 'niet gevonden', zodat de dienst niet steeds dezelfde vraag krijgt) en routes.
    """CREATE TABLE IF NOT EXISTS plekken (
        sleutel text PRIMARY KEY,
        tekst text NOT NULL,
        lon double precision,
        lat double precision,
        label text NOT NULL DEFAULT '',
        gevonden boolean NOT NULL DEFAULT false,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # Gekozen vervoer per afspraak (bij een terugkerende afspraak voor de hele reeks): fiets, auto, ov, lopen of geen.
    """CREATE TABLE IF NOT EXISTS agenda_vervoer (
        sleutel text PRIMARY KEY,
        vervoer text NOT NULL CHECK (vervoer IN ('fiets', 'auto', 'ov', 'lopen', 'geen')),
        titel text NOT NULL DEFAULT '',
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS routes (
        sleutel text PRIMARY KEY,
        duur int NOT NULL,
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # Kortlevende 'state' voor het koppelen bij Google en Microsoft (tegen nagemaakte terugkeerverzoeken).
    """CREATE TABLE IF NOT EXISTS oauth_staat (
        staat text PRIMARY KEY,
        soort text NOT NULL,
        opties jsonb NOT NULL DEFAULT '{}',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # Voorstellen van Clara (een blok in de agenda zetten): pas na een klik van Martijn komt het in de agenda.
    """CREATE TABLE IF NOT EXISTS agenda_voorstellen (
        id serial PRIMARY KEY,
        gesprek_id int REFERENCES gesprekken(id) ON DELETE SET NULL,
        titel text NOT NULL,
        start timestamptz NOT NULL,
        eind timestamptz NOT NULL,
        domein text NOT NULL DEFAULT '',
        status text NOT NULL DEFAULT 'open',
        koppeling_id int,
        extern_id text,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # Voorstel van Clara voor een bepaalde agenda (bijvoorbeeld die van Sanne).
    "ALTER TABLE agenda_voorstellen ADD COLUMN IF NOT EXISTS agenda text NOT NULL DEFAULT ''",
    # Reistijd per afspraak (of per reeks, via de iCalUID): een eigen adres als de afspraak er geen (goed) heeft, en of
    # Clara de reistijd elke keer zelf inplant (Martijn zette dat aan met een klik).
    """CREATE TABLE IF NOT EXISTS agenda_reisinfo (
        sleutel text PRIMARY KEY,
        adres text NOT NULL DEFAULT '',
        auto boolean NOT NULL DEFAULT false,
        titel text NOT NULL DEFAULT '',
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # Herhalende afspraken: bij welke reeks hoort een afspraak (Google recurringEventId, Outlook seriesMasterId).
    "ALTER TABLE agenda_items ADD COLUMN IF NOT EXISTS reeks text NOT NULL DEFAULT ''",
    # Reeksen die Clara (na een klik) maakte, zodat ze die later kan stoppen, een keer overslaan of verwijderen.
    """CREATE TABLE IF NOT EXISTS agenda_reeksen (
        id serial PRIMARY KEY,
        koppeling_id int NOT NULL REFERENCES agenda_koppelingen(id) ON DELETE CASCADE,
        agenda_id text NOT NULL,
        extern_id text NOT NULL,
        titel text NOT NULL,
        start timestamptz NOT NULL,
        eind timestamptz NOT NULL,
        herhaal jsonb NOT NULL,
        voor text NOT NULL DEFAULT '',
        status text NOT NULL DEFAULT 'actief',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # Een reeks van Clara kan ook de reistijd bij een reeks van iemand anders zijn (soort 'reistijd', met de bron erbij).
    "ALTER TABLE agenda_reeksen ADD COLUMN IF NOT EXISTS soort text NOT NULL DEFAULT 'eigen'",
    "ALTER TABLE agenda_reeksen ADD COLUMN IF NOT EXISTS bron_koppeling int",
    "ALTER TABLE agenda_reeksen ADD COLUMN IF NOT EXISTS bron_reeks text NOT NULL DEFAULT ''",
    # Voorstellen: een nieuwe afspraak (eventueel herhalend, eventueel ter vervanging van een reeks), of een reeks
    # stoppen, een keer overslaan of verwijderen.
    "ALTER TABLE agenda_voorstellen ADD COLUMN IF NOT EXISTS soort text NOT NULL DEFAULT 'nieuw'",
    "ALTER TABLE agenda_voorstellen ADD COLUMN IF NOT EXISTS herhaal jsonb",
    "ALTER TABLE agenda_voorstellen ADD COLUMN IF NOT EXISTS reeks_id int",
    "ALTER TABLE agenda_voorstellen ADD COLUMN IF NOT EXISTS datum date",
    # Beschikbaarheid delen: mag Clara in dit account blokken 'Bezet' zetten, en welke blokken staan er al.
    "ALTER TABLE agenda_koppelingen ADD COLUMN IF NOT EXISTS spiegel boolean NOT NULL DEFAULT true",
    """CREATE TABLE IF NOT EXISTS agenda_spiegel (
        doel_koppeling int NOT NULL REFERENCES agenda_koppelingen(id) ON DELETE CASCADE,
        bron text NOT NULL,
        doel_agenda text NOT NULL,
        extern_id text NOT NULL,
        titel text NOT NULL DEFAULT '',
        start timestamptz NOT NULL,
        eind timestamptz NOT NULL,
        voorlopig boolean NOT NULL DEFAULT false,
        bijgewerkt timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (doel_koppeling, bron))""",
]
