-- Basistabellen van de database doellijn die de Doellijn-app niet zelf aanmaakt (ze stammen uit de eerste opzet met
-- n8n). Alleen voor tests op een lege database; de echte database op de server heeft ze al.
-- Kolommen afgeleid uit de queries in app/zerohero en de n8n-werkstromen.
CREATE TABLE IF NOT EXISTS items (
    id bigserial PRIMARY KEY,
    miniflux_id bigint UNIQUE,
    bron text, categorie text, url text, titel text,
    gepubliceerd timestamptz,
    binnen timestamptz NOT NULL DEFAULT now(),
    sport text, competitie text,
    keepers text[] NOT NULL DEFAULT '{}',
    soort text, nl_haak boolean, thema text, congreshaak boolean, gevoelig boolean,
    score int, score_detail jsonb, samenvatting text, invalshoek text, advies jsonb, verhaal text,
    status text NOT NULL DEFAULT 'nieuw',
    gemeld boolean NOT NULL DEFAULT false,
    model text, tokens_in int, tokens_out int
);
CREATE TABLE IF NOT EXISTS tt_orders (
    order_id text PRIMARY KEY,
    event_id text, status text, aantal int, totaal_cent int, valuta text,
    tickettypes jsonb, besteld timestamptz
);
