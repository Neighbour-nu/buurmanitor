"""Databaseschema van Buurmanitor (de kern en de assistent).

Politiek (Promonitor) en ZEROHERO (de Doellijn) hebben hun eigen database; die tabellen staan hier niet.
Migraties draaien bij elke start en zijn veilig om opnieuw uit te voeren.
"""
from .kern import tekst

def _vera_bronnen(stroom="overig"):
    """De standaardbronnen van Vera één keer klaarzetten (een vlag in instellingen onthoudt dat het gebeurd is)."""
    from .vera.nieuws import STANDAARD_BRONNEN, STANDAARD_LANDELIJK  # pylint: disable=import-outside-toplevel
    lijst, vlag = (STANDAARD_BRONNEN, "vera_bronnen_klaargezet") if stroom == "overig" else (STANDAARD_LANDELIJK, "vera_landelijk_klaargezet")
    waarden = ", ".join("(" + ", ".join("'" + x.replace("'", "''") + "'" for x in b) + ")" for b in lijst)
    kolommen, extra = ("naam, url, soort, rubriek", "") if stroom == "overig" else ("naam, url, soort, rubriek, stroom", ", 'landelijk'")
    return f"""WITH vlag AS (INSERT INTO instellingen (sleutel, waarde) VALUES ('{vlag}', '1')
                             ON CONFLICT (sleutel) DO NOTHING RETURNING 1)
               INSERT INTO vera_bronnen ({kolommen}) SELECT *{extra} FROM (VALUES {waarden}) AS v(naam, url, soort, rubriek)
               WHERE EXISTS (SELECT 1 FROM vlag)"""


def _jesse_bronnen():
    """De standaardbronnen van Jesse één keer klaarzetten."""
    from .jesse.luisteren import STANDAARD_BRONNEN  # pylint: disable=import-outside-toplevel
    waarden = ", ".join("(" + ", ".join("'" + x.replace("'", "''") + "'" for x in b) + ")" for b in STANDAARD_BRONNEN)
    return f"""WITH vlag AS (INSERT INTO instellingen (sleutel, waarde) VALUES ('jesse_bronnen_klaargezet', '1')
                             ON CONFLICT (sleutel) DO NOTHING RETURNING 1)
               INSERT INTO jesse_bronnen (naam, soort, adres, stroom) SELECT * FROM (VALUES {waarden}) AS v(naam, soort, adres, stroom)
               WHERE EXISTS (SELECT 1 FROM vlag)"""


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
    "ALTER TABLE taken ADD COLUMN IF NOT EXISTS duur_ms int",  # hoe lang de taak de laatste keer duurde
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
    # Wat Martijn met een melding deed: open (nog te doen), opgelost (afgevinkt), archief of verwijderd. Verwijderen
    # bewaart de regel (in de prullenbak), zodat dezelfde melding met dezelfde sleutel niet opnieuw verschijnt.
    "ALTER TABLE meldingen ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'open'",
    "ALTER TABLE meldingen ADD COLUMN IF NOT EXISTS afgehandeld timestamptz",
    "CREATE INDEX IF NOT EXISTS meldingen_status ON meldingen (status, id DESC)",
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
    # Vergaderingen uit de raadsagenda (Notubiz, of een agendalink zoals Bundeling) die Martijn in zijn agenda overnam.
    """CREATE TABLE IF NOT EXISTS agenda_overgenomen (
        id serial PRIMARY KEY,
        bron_koppeling int NOT NULL,
        bron_extern text NOT NULL,
        doel_koppeling int NOT NULL REFERENCES agenda_koppelingen(id) ON DELETE CASCADE,
        doel_agenda text NOT NULL,
        extern_id text NOT NULL,
        titel text NOT NULL,
        start timestamptz NOT NULL,
        eind timestamptz NOT NULL,
        locatie text NOT NULL DEFAULT '',
        status text NOT NULL DEFAULT 'actief',
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        UNIQUE (bron_koppeling, bron_extern, doel_koppeling))""",
    # Beschikbaarheid delen: mag Clara in dit account blokken 'Bezet' zetten, en welke blokken staan er al.
    "ALTER TABLE agenda_koppelingen ADD COLUMN IF NOT EXISTS spiegel boolean NOT NULL DEFAULT true",
    # Wat er in de blokken in dit account komt: '' (de algemene keuze), 'bezet', 'titel' (privé) of 'details' (titel en plek,
    # zichtbaar voor wie de agenda met details mag zien, zoals een partner in de privé-agenda).
    "ALTER TABLE agenda_koppelingen ADD COLUMN IF NOT EXISTS spiegel_detail text NOT NULL DEFAULT ''",
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
    "ALTER TABLE agenda_spiegel ADD COLUMN IF NOT EXISTS locatie text NOT NULL DEFAULT ''",
    "ALTER TABLE agenda_spiegel ADD COLUMN IF NOT EXISTS zichtbaar boolean NOT NULL DEFAULT false",
    # Bram (postkamer): eigen koppelingen met toestemming voor mail, los van die van Clara.
    # claude: mag Bram de tekst van mails uit dit account naar Claude sturen om ze te sorteren.
    """CREATE TABLE IF NOT EXISTS post_koppelingen (
        id serial PRIMARY KEY,
        soort text NOT NULL CHECK (soort IN ('google', 'microsoft')),
        naam text NOT NULL,
        account text NOT NULL DEFAULT '',
        tenant text NOT NULL DEFAULT '',
        domein text NOT NULL DEFAULT 'prive',
        geheim text,
        claude boolean NOT NULL DEFAULT false,
        actief boolean NOT NULL DEFAULT true,
        laatst timestamptz,
        fout text,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # Berichten in de inbox, zonder de tekst zelf (die gaat hooguit één keer naar Claude). status: open (in de inbox)
    # of weg (gearchiveerd, hier of in Gmail/Outlook zelf). Het oordeel van Bram: categorie, prioriteit, één regel
    # (wat het is en wat het vraagt), een voorstel in woorden en de bijbehorende knop (actie).
    """CREATE TABLE IF NOT EXISTS post_berichten (
        id serial PRIMARY KEY,
        koppeling_id int NOT NULL REFERENCES post_koppelingen(id) ON DELETE CASCADE,
        extern_id text NOT NULL,
        draad text NOT NULL DEFAULT '',
        van_naam text NOT NULL DEFAULT '',
        van_adres text NOT NULL DEFAULT '',
        aan text NOT NULL DEFAULT '',
        onderwerp text NOT NULL DEFAULT '',
        fragment text NOT NULL DEFAULT '',
        ontvangen timestamptz NOT NULL,
        gelezen boolean NOT NULL DEFAULT false,
        labels jsonb NOT NULL DEFAULT '[]',
        link text NOT NULL DEFAULT '',
        nieuwsbrief boolean NOT NULL DEFAULT false,
        bron_categorie text NOT NULL DEFAULT '',
        status text NOT NULL DEFAULT 'open',
        categorie text NOT NULL DEFAULT '',
        prioriteit text NOT NULL DEFAULT '',
        regel text NOT NULL DEFAULT '',
        voorstel text NOT NULL DEFAULT '',
        actie text NOT NULL DEFAULT '',
        beoordeeld_door text NOT NULL DEFAULT '',
        beoordeeld timestamptz,
        gezien timestamptz NOT NULL DEFAULT now(),
        UNIQUE (koppeling_id, extern_id))""",
    "CREATE INDEX IF NOT EXISTS post_berichten_open ON post_berichten (status, ontvangen DESC)",
    # Een gesprek met Bram over één bericht, en wat Bram daarbij voorstelt: een antwoord versturen, een afspraak of
    # een herinnering in de agenda. Uitgevoerd wordt het pas na een klik van Martijn.
    "ALTER TABLE post_berichten ADD COLUMN IF NOT EXISTS gesprek_id int REFERENCES gesprekken(id) ON DELETE SET NULL",
    # Uitschrijven: wanneer Bram het deed, of de link als het alleen via de website van de afzender kan.
    # Gesprek met Bram over de hele postkamer (kader 'postkamer'), los van de gesprekken over één bericht. Daarin kan Bram
    # ook voorstellen om meer berichten tegelijk op te ruimen (soort 'bulk').
    "ALTER TABLE gesprekken ADD COLUMN IF NOT EXISTS kader text NOT NULL DEFAULT ''",
    "ALTER TABLE post_berichten ADD COLUMN IF NOT EXISTS uitgeschreven timestamptz",
    "ALTER TABLE post_berichten ADD COLUMN IF NOT EXISTS uitschrijf_link text NOT NULL DEFAULT ''",
    # Zelf verplaatst (actie, lezen of weg) gaat voor het oordeel van Bram. Een afzenderregel doet dat voortaan
    # vanzelf voor elke nieuwe mail van dat adres.
    "ALTER TABLE post_berichten ADD COLUMN IF NOT EXISTS groep_handmatig text NOT NULL DEFAULT ''",
    # Mappen in Gmail en Outlook waarin Martijn archiveert: de laatst gebruikte komen als snelknop boven de lijst.
    """CREATE TABLE IF NOT EXISTS post_mapgebruik (
        koppeling_id int NOT NULL REFERENCES post_koppelingen(id) ON DELETE CASCADE,
        map_id text NOT NULL,
        naam text NOT NULL,
        aantal int NOT NULL DEFAULT 0,
        laatst timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (koppeling_id, map_id))""",
    """CREATE TABLE IF NOT EXISTS post_afzenderregels (
        van_adres text PRIMARY KEY,
        groep text NOT NULL CHECK (groep IN ('actie', 'lezen', 'weg')),
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS post_voorstellen (
        id serial PRIMARY KEY,
        gesprek_id int REFERENCES gesprekken(id) ON DELETE CASCADE,
        bericht_id int REFERENCES post_berichten(id) ON DELETE CASCADE,
        soort text NOT NULL CHECK (soort IN ('antwoord', 'afspraak', 'herinnering')),
        gegevens jsonb NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'open',
        uitslag text NOT NULL DEFAULT '',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # (na het aanmaken, zodat het ook op een lege database werkt) soort 'bulk' erbij
    "ALTER TABLE post_voorstellen DROP CONSTRAINT IF EXISTS post_voorstellen_soort_check",
    "ALTER TABLE post_voorstellen ADD CONSTRAINT post_voorstellen_soort_check CHECK (soort IN ('antwoord', 'afspraak', 'herinnering', 'bulk'))",
    # Regels voor binnenkomende mail (zie bram/regels.py): als een nieuwe mail aan de voorwaarden voldoet, doet Bram
    # meteen wat de regel zegt (gelezen, label, naar een map of archiveren). Volgorde telt: de eerste regel die past.
    """CREATE TABLE IF NOT EXISTS post_regels (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        actief boolean NOT NULL DEFAULT true,
        volgorde int NOT NULL DEFAULT 0,
        koppeling_id int REFERENCES post_koppelingen(id) ON DELETE CASCADE,
        voorwaarden jsonb NOT NULL DEFAULT '{}',
        acties jsonb NOT NULL DEFAULT '{}',
        aantal int NOT NULL DEFAULT 0,
        laatst timestamptz,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS post_regel_log (
        id serial PRIMARY KEY,
        regel_id int REFERENCES post_regels(id) ON DELETE CASCADE,
        koppeling_id int REFERENCES post_koppelingen(id) ON DELETE CASCADE,
        van text NOT NULL DEFAULT '',
        onderwerp text NOT NULL DEFAULT '',
        wat text NOT NULL DEFAULT '',
        fout text NOT NULL DEFAULT '',
        tijd timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS post_regel_log_tijd ON post_regel_log (tijd DESC)",
    # De namen van de bijlagen (NULL: niet bekend), voor regels als 'met een factuur in de bijlage'.
    "ALTER TABLE post_berichten ADD COLUMN IF NOT EXISTS bijlagen jsonb",
    # ---------- Taken: kanbanbord ----------
    # Een taak met eventueel een start- en einddatum; dan staat hij ook als afspraak in de agenda (agenda_*: welke).
    """CREATE TABLE IF NOT EXISTS takenbord (
        id serial PRIMARY KEY,
        titel text NOT NULL,
        beschrijving text NOT NULL DEFAULT '',
        prio text NOT NULL DEFAULT 'middel' CHECK (prio IN ('laag', 'middel', 'hoog')),
        status text NOT NULL DEFAULT 'nieuw' CHECK (status IN ('nieuw', 'triage', 'gepland', 'actief', 'wachtend', 'afgerond')),
        wacht_op text NOT NULL DEFAULT '',
        startdatum date,
        starttijd time,
        einddatum date,
        eindtijd time,
        doel text NOT NULL DEFAULT '',
        agenda_koppeling int,
        agenda_id text NOT NULL DEFAULT '',
        agenda_extern text NOT NULL DEFAULT '',
        agenda_spec text NOT NULL DEFAULT '',
        agenda_fout text NOT NULL DEFAULT '',
        bericht_id int REFERENCES post_berichten(id) ON DELETE SET NULL,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now(),
        afgerond timestamptz)""",
    # Noor: voorstellen uit het gesprek over het takenbord (een nieuwe taak, of een taak wijzigen). Pas na Martijns klik.
    """CREATE TABLE IF NOT EXISTS taak_voorstellen (
        id serial PRIMARY KEY,
        gesprek_id int REFERENCES gesprekken(id) ON DELETE CASCADE,
        soort text NOT NULL CHECK (soort IN ('nieuw', 'wijzig')),
        taak_id int REFERENCES takenbord(id) ON DELETE CASCADE,
        gegevens jsonb NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'open',
        uitslag text NOT NULL DEFAULT '',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # ---------- Vera: nieuwsredactie ----------
    # Eigen bronnen voor 'overig nieuws' (keepers- en politiek nieuws leest Vera uit de werkruimtes).
    """CREATE TABLE IF NOT EXISTS vera_bronnen (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        url text NOT NULL,
        soort text NOT NULL DEFAULT 'feed' CHECK (soort IN ('feed', 'google')),
        rubriek text NOT NULL DEFAULT 'Algemeen',
        actief boolean NOT NULL DEFAULT true,
        laatst timestamptz,
        status text NOT NULL DEFAULT '',
        fout text NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS vera_berichten (
        id serial PRIMARY KEY,
        bron_id int REFERENCES vera_bronnen(id) ON DELETE SET NULL,
        guid text NOT NULL UNIQUE,
        titel text NOT NULL,
        url text NOT NULL,
        samenvatting text NOT NULL DEFAULT '',
        bron text NOT NULL DEFAULT '',
        rubriek text NOT NULL DEFAULT '',
        gepubliceerd timestamptz,
        binnen timestamptz NOT NULL DEFAULT now(),
        score int,
        waarom text NOT NULL DEFAULT '',
        ook_bij text NOT NULL DEFAULT '',
        verborgen boolean NOT NULL DEFAULT false)""",
    "CREATE INDEX IF NOT EXISTS vera_berichten_binnen ON vera_berichten (binnen DESC)",
    """CREATE TABLE IF NOT EXISTS vera_gelezen (
        soort text NOT NULL,
        bericht int NOT NULL,
        tijd timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (soort, bericht))""",
    # Voorstellen uit het gesprek met Vera over haar bronnen (pas na Martijns klik).
    """CREATE TABLE IF NOT EXISTS vera_voorstellen (
        id serial PRIMARY KEY,
        gesprek_id int REFERENCES gesprekken(id) ON DELETE CASCADE,
        soort text NOT NULL CHECK (soort IN ('nieuw', 'wijzig', 'aan', 'uit', 'weg')),
        bron_id int REFERENCES vera_bronnen(id) ON DELETE CASCADE,
        gegevens jsonb NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'open',
        uitslag text NOT NULL DEFAULT '',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # De standaardbronnen één keer klaarzetten (daarna beheer je ze zelf op Nieuws › Bronnen).
    _vera_bronnen(),
    # Landelijk politiek nieuws: een tweede stroom in Vera's eigen bronnen (naast 'overig').
    "ALTER TABLE vera_bronnen ADD COLUMN IF NOT EXISTS stroom text NOT NULL DEFAULT 'overig'",
    "ALTER TABLE vera_berichten ADD COLUMN IF NOT EXISTS stroom text NOT NULL DEFAULT 'overig'",
    "ALTER TABLE vera_bronnen DROP CONSTRAINT IF EXISTS vera_bronnen_stroom_check",
    "ALTER TABLE vera_bronnen ADD CONSTRAINT vera_bronnen_stroom_check CHECK (stroom IN ('overig', 'landelijk'))",
    _vera_bronnen("landelijk"),
    # ---------- Jesse: socials ----------
    # Postvoorstellen uit het gesprek met Jesse: tekst om te kopiëren. Jesse post nooit zelf.
    """CREATE TABLE IF NOT EXISTS jesse_voorstellen (
        id serial PRIMARY KEY,
        gesprek_id int REFERENCES gesprekken(id) ON DELETE SET NULL,
        kanaal text NOT NULL,
        voor text NOT NULL DEFAULT '',
        tekst text NOT NULL,
        status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'bewaard', 'weg')),
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # Jesse luistert mee op de socials: eigen bronnen (Reddit, Mastodon, Bluesky, YouTube, RSS), de berichten en wat gelezen is.
    """CREATE TABLE IF NOT EXISTS jesse_bronnen (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        soort text NOT NULL CHECK (soort IN ('reddit', 'mastodon', 'bluesky', 'youtube', 'rss')),
        adres text NOT NULL,
        stroom text NOT NULL DEFAULT 'keepers' CHECK (stroom IN ('keepers', 'politiek', 'overig')),
        actief boolean NOT NULL DEFAULT true,
        laatst timestamptz,
        status text NOT NULL DEFAULT '',
        fout text NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS jesse_berichten (
        id serial PRIMARY KEY,
        bron_id int REFERENCES jesse_bronnen(id) ON DELETE SET NULL,
        platform text NOT NULL,
        stroom text NOT NULL,
        guid text NOT NULL UNIQUE,
        auteur text NOT NULL DEFAULT '',
        tekst text NOT NULL,
        url text NOT NULL,
        tijd timestamptz,
        binnen timestamptz NOT NULL DEFAULT now(),
        interacties int,
        score int,
        toon text NOT NULL DEFAULT '',
        waarom text NOT NULL DEFAULT '',
        verborgen boolean NOT NULL DEFAULT false)""",
    "CREATE INDEX IF NOT EXISTS jesse_berichten_binnen ON jesse_berichten (binnen DESC)",
    """CREATE TABLE IF NOT EXISTS jesse_gelezen (
        bericht int PRIMARY KEY,
        tijd timestamptz NOT NULL DEFAULT now())""",
    _jesse_bronnen(),
    # Herhalende taken: een reeks waar losse taken (met elk een afspraak) van gemaakt worden. gemaakt_tot onthoudt tot welke
    # dag de taken al gemaakt zijn, zodat een taak die Martijn weghaalt niet terugkomt.
    """CREATE TABLE IF NOT EXISTS takenreeksen (
        id serial PRIMARY KEY,
        titel text NOT NULL,
        beschrijving text NOT NULL DEFAULT '',
        prio text NOT NULL DEFAULT 'middel',
        status text NOT NULL DEFAULT 'gepland',
        soort text NOT NULL DEFAULT 'week' CHECK (soort IN ('week', 'maand')),
        dagen jsonb NOT NULL DEFAULT '[]',
        interval int NOT NULL DEFAULT 1,
        begindatum date NOT NULL,
        einddatum date,
        starttijd time,
        eindtijd time,
        doel text NOT NULL DEFAULT '',
        gemaakt_tot date,
        actief boolean NOT NULL DEFAULT true,
        klaar boolean NOT NULL DEFAULT false,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    "ALTER TABLE takenbord ADD COLUMN IF NOT EXISTS reeks_id int REFERENCES takenreeksen(id) ON DELETE SET NULL",
    # Takenarchief: afgeronde taken die van het bord af zijn (zelf gearchiveerd, of automatisch na een maand).
    "ALTER TABLE takenbord ADD COLUMN IF NOT EXISTS gearchiveerd timestamptz",
    "CREATE INDEX IF NOT EXISTS takenbord_archief ON takenbord (gearchiveerd DESC) WHERE gearchiveerd IS NOT NULL",
    # ---------- CRM: relaties, kansen en projecten (zie crm/) ----------
    # Activiteiten: waar een kans of project bij hoort. soort 'commercieel' (fases met waarde in euro's) of 'initiatief'
    # (politiek: geen geld, eigen fases). code sluit aan bij de domeinen van de mailboxen (zerohero, upperspire, fbim).
    """CREATE TABLE IF NOT EXISTS crm_activiteiten (
        code text PRIMARY KEY,
        naam text NOT NULL,
        soort text NOT NULL DEFAULT 'commercieel' CHECK (soort IN ('commercieel', 'initiatief')),
        volgorde int NOT NULL DEFAULT 0,
        actief boolean NOT NULL DEFAULT true)""",
    """INSERT INTO crm_activiteiten (code, naam, soort, volgorde) VALUES ('zerohero', 'ZEROHERO', 'commercieel', 1),
        ('upperspire', 'Upper Spire', 'commercieel', 2), ('fbim', 'IV-professional', 'commercieel', 3), ('politiek', 'Politiek', 'initiatief', 4)
        ON CONFLICT (code) DO NOTHING""",
    """CREATE TABLE IF NOT EXISTS crm_organisaties (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        domein text NOT NULL DEFAULT '',
        website text NOT NULL DEFAULT '',
        notitie text NOT NULL DEFAULT '',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS crm_organisaties_domein ON crm_organisaties (lower(domein)) WHERE domein <> ''",
    # Contactpersonen. Uit de mail opgebouwd (bron 'mail'): per afzender één contact; verborgen = niet tonen
    # (bijvoorbeeld een automatische afzender die toch doorkwam).
    """CREATE TABLE IF NOT EXISTS crm_contacten (
        id serial PRIMARY KEY,
        naam text NOT NULL DEFAULT '',
        email text NOT NULL DEFAULT '',
        organisatie_id int REFERENCES crm_organisaties(id) ON DELETE SET NULL,
        functie text NOT NULL DEFAULT '',
        telefoon text NOT NULL DEFAULT '',
        notitie text NOT NULL DEFAULT '',
        activiteit text NOT NULL DEFAULT '',
        bron text NOT NULL DEFAULT 'handmatig',
        mails int NOT NULL DEFAULT 0,
        laatste_mail timestamptz,
        verborgen boolean NOT NULL DEFAULT false,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS crm_contacten_email ON crm_contacten (lower(email)) WHERE email <> ''",
    """CREATE TABLE IF NOT EXISTS crm_projecten (
        id serial PRIMARY KEY,
        titel text NOT NULL,
        activiteit text NOT NULL REFERENCES crm_activiteiten(code),
        status text NOT NULL DEFAULT 'gepland' CHECK (status IN ('idee', 'gepland', 'actief', 'afgerond', 'gestopt')),
        omschrijving text NOT NULL DEFAULT '',
        datum date,
        start date,
        eind date,
        organisatie_id int REFERENCES crm_organisaties(id) ON DELETE SET NULL,
        contact_id int REFERENCES crm_contacten(id) ON DELETE SET NULL,
        budget_kosten numeric(12,2),
        budget_opbrengst numeric(12,2),
        kosten numeric(12,2),
        opbrengst numeric(12,2),
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS crm_kansen (
        id serial PRIMARY KEY,
        titel text NOT NULL,
        activiteit text NOT NULL REFERENCES crm_activiteiten(code),
        fase text NOT NULL,
        omschrijving text NOT NULL DEFAULT '',
        organisatie_id int REFERENCES crm_organisaties(id) ON DELETE SET NULL,
        contact_id int REFERENCES crm_contacten(id) ON DELETE SET NULL,
        waarde numeric(12,2),
        kans int CHECK (kans BETWEEN 0 AND 100),
        verwacht date,
        volgende_stap text NOT NULL DEFAULT '',
        volgende_datum date,
        taak_id int REFERENCES takenbord(id) ON DELETE SET NULL,
        project_id int REFERENCES crm_projecten(id) ON DELETE SET NULL,
        gesloten timestamptz,
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    "ALTER TABLE crm_projecten ADD COLUMN IF NOT EXISTS kans_id int REFERENCES crm_kansen(id) ON DELETE SET NULL",
    # De tijdlijn van een kans of project: notities, gekoppelde mails, nieuws- en socialberichten, links en fasewissels.
    """CREATE TABLE IF NOT EXISTS crm_tijdlijn (
        id serial PRIMARY KEY,
        kans_id int REFERENCES crm_kansen(id) ON DELETE CASCADE,
        project_id int REFERENCES crm_projecten(id) ON DELETE CASCADE,
        soort text NOT NULL CHECK (soort IN ('notitie', 'mail', 'nieuws', 'social', 'link', 'fase')),
        titel text NOT NULL DEFAULT '',
        tekst text NOT NULL DEFAULT '',
        url text NOT NULL DEFAULT '',
        bericht_id int REFERENCES post_berichten(id) ON DELETE SET NULL,
        tijd timestamptz NOT NULL DEFAULT now(),
        CHECK (kans_id IS NOT NULL OR project_id IS NOT NULL))""",
    "CREATE INDEX IF NOT EXISTS crm_tijdlijn_kans ON crm_tijdlijn (kans_id, tijd DESC)",
    "CREATE INDEX IF NOT EXISTS crm_tijdlijn_project ON crm_tijdlijn (project_id, tijd DESC)",
    # Sjablonen voor terugkerende projecten (zoals een uitgave van het keeperscongres): vaste taken met het aantal dagen
    # voor (negatief) of na de projectdatum.
    """CREATE TABLE IF NOT EXISTS crm_sjablonen (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        activiteit text REFERENCES crm_activiteiten(code) ON DELETE SET NULL,
        taken jsonb NOT NULL DEFAULT '[]',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    "ALTER TABLE takenbord ADD COLUMN IF NOT EXISTS project_id int REFERENCES crm_projecten(id) ON DELETE SET NULL",
    "ALTER TABLE takenbord ADD COLUMN IF NOT EXISTS kans_id int REFERENCES crm_kansen(id) ON DELETE SET NULL",
    # Voorstellen van Iris in het gesprek ("maak een kans van die mail van Jan"): pas na Martijns klik aangemaakt.
    """CREATE TABLE IF NOT EXISTS crm_voorstellen (
        id serial PRIMARY KEY,
        gesprek_id int REFERENCES gesprekken(id) ON DELETE CASCADE,
        bericht_id int REFERENCES gesprek_berichten(id) ON DELETE CASCADE,
        gegevens jsonb NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'gemaakt', 'weg')),
        kans_id int REFERENCES crm_kansen(id) ON DELETE SET NULL,
        project_id int REFERENCES crm_projecten(id) ON DELETE SET NULL,
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    # Dagelijkse overzichten per mail (Promonitor, ZEROHERO): zelf in te stellen onderdelen, opmaak, tijd en ontvangers.
    # onderdelen: [{"code", "aan", "titel", "max", "opties": {...}}] in de volgorde van de mail.
    """CREATE TABLE IF NOT EXISTS overzichten (
        id serial PRIMARY KEY,
        naam text NOT NULL,
        werkruimte text NOT NULL CHECK (werkruimte IN ('politiek', 'zerohero')),
        actief boolean NOT NULL DEFAULT false,
        tijd time NOT NULL DEFAULT '08:00',
        dagen int[] NOT NULL DEFAULT '{0,1,2,3,4,5,6}',
        onderwerp text NOT NULL DEFAULT '',
        kop text NOT NULL DEFAULT '',
        intro text NOT NULL DEFAULT '',
        afsluiting text NOT NULL DEFAULT '',
        kleur text NOT NULL DEFAULT '#FF0000',
        afzender text NOT NULL DEFAULT '',
        antwoord_naar text NOT NULL DEFAULT '',
        telegram boolean NOT NULL DEFAULT false,
        leeg_versturen boolean NOT NULL DEFAULT true,
        onderdelen jsonb NOT NULL DEFAULT '[]',
        laatst timestamptz,
        uitslag text NOT NULL DEFAULT '',
        aangemaakt timestamptz NOT NULL DEFAULT now(),
        bijgewerkt timestamptz NOT NULL DEFAULT now())""",
    # rol eigen: Martijn zelf (alles, met links naar Buurmanitor). rol lid: een fractielid of ander (geen links naar
    # Buurmanitor, geen persoonlijke onderdelen zoals agenda en taken). persoon: de Whaller-naam in Promonitor, voor de
    # eigen deadlines en agendapunten van die persoon.
    """CREATE TABLE IF NOT EXISTS overzicht_ontvangers (
        id serial PRIMARY KEY,
        overzicht_id int NOT NULL REFERENCES overzichten(id) ON DELETE CASCADE,
        naam text NOT NULL DEFAULT '',
        email text NOT NULL,
        rol text NOT NULL DEFAULT 'lid' CHECK (rol IN ('eigen', 'lid')),
        persoon text NOT NULL DEFAULT '',
        actief boolean NOT NULL DEFAULT true,
        laatst timestamptz,
        fout text NOT NULL DEFAULT '',
        aangemaakt timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS overzicht_ontvangers_uniek ON overzicht_ontvangers (overzicht_id, lower(email))",
    """CREATE TABLE IF NOT EXISTS overzicht_verzonden (
        id bigserial PRIMARY KEY,
        overzicht_id int NOT NULL REFERENCES overzichten(id) ON DELETE CASCADE,
        tijd timestamptz NOT NULL DEFAULT now(),
        aan text NOT NULL,
        gelukt boolean NOT NULL,
        tekst text NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS overzicht_verzonden_tijd ON overzicht_verzonden (overzicht_id, tijd DESC)",
] + tekst.sql_migraties("vera_berichten", ["titel", "samenvatting", "waarom"])
