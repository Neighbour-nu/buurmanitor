"""Databaseschema van Buurmanitor (de kern en de assistent).

Promonitor en de Doellijn houden in fase 1 en 2 hun eigen database; die tabellen staan hier niet.
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
    # Logboek: wat een persona deed of voorstelde, en wat Martijn besloot.
    """CREATE TABLE IF NOT EXISTS logboek (
        id serial PRIMARY KEY,
        tijd timestamptz NOT NULL DEFAULT now(),
        persona text NOT NULL,
        actie text NOT NULL,
        details jsonb)""",
]
