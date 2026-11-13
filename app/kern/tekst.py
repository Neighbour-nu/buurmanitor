"""Tekst schoonmaken die door Claude (met webzoeken) of een website is gemaakt: geen <cite index="...">-bronvermeldingen
en geen andere HTML-tags in koppen, samenvattingen en toelichtingen. Op twee plekken:

- schoon(): in Python, voor alles wat Buurmanitor zelf toont of mailt;
- sql_migraties(): een trigger in de database die het bij elke INSERT en UPDATE doet, ook als n8n de regel schrijft,
  plus het opschonen van wat er al staat.
"""
import html
import re

CITE = re.compile(r"(?:<|&lt;)\s*/?\s*cite\b(?:[^<>&]|&quot;|&#34;|&#39;|&apos;)*(?:>|&gt;)", re.I)
TAG = re.compile(r"(?:<|&lt;)/?[a-zA-Z][a-zA-Z0-9]*\b(?:\s[^<>]*?)?\s*/?\s*(?:>|&gt;)")


def schoon(tekst):
    """Tekst zonder bronvermeldingen en HTML-tags; de tekst tussen de tags blijft staan. Pure functie."""
    if not tekst or not isinstance(tekst, str) or ("<" not in tekst and "&" not in tekst):
        return tekst
    t = TAG.sub("", CITE.sub("", tekst))
    t = html.unescape(t) if "&" in t else t
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r" ([.,;:!?])", r"\1", t).strip()


# Dezelfde schoonmaak in PostgreSQL. bm_schoon is idempotent: twee keer schoonmaken geeft hetzelfde.
FUNCTIES = [
    r"""CREATE OR REPLACE FUNCTION bm_schoon(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
        SELECT CASE WHEN t IS NULL OR t !~ '<|&' THEN t ELSE btrim(
          regexp_replace(regexp_replace(
            replace(replace(replace(replace(replace(replace(
              regexp_replace(regexp_replace(t,
                '(<|&lt;)\s*/?\s*cite\M([^<>&]|&quot;|&#34;|&#39;|&apos;)*(>|&gt;)', '', 'gi'),
                '(<|&lt;)/?[a-zA-Z][a-zA-Z0-9]*\M(\s[^<>]*?)?\s*/?\s*(>|&gt;)', '', 'g'),
              '&quot;', '"'), '&#34;', '"'), '&#39;', ''''), '&apos;', ''''), '&nbsp;', ' '), '&amp;', '&'),
            '[ \t]{2,}', ' ', 'g'),
          ' ([.,;:!?])', '\1', 'g')) END
    $f$""",
    r"""CREATE OR REPLACE FUNCTION bm_schoon_kolommen() RETURNS trigger LANGUAGE plpgsql AS $f$
        DECLARE k text; j jsonb := to_jsonb(NEW); veranderd boolean := false; oud text; nieuw text;
        BEGIN
          FOREACH k IN ARRAY TG_ARGV LOOP
            IF jsonb_typeof(j -> k) = 'string' THEN
              oud := j ->> k;
              IF oud ~ '<|&' THEN
                nieuw := bm_schoon(oud);
                IF nieuw IS DISTINCT FROM oud THEN
                  j := jsonb_set(j, ARRAY[k], to_jsonb(nieuw));
                  veranderd := true;
                END IF;
              END IF;
            END IF;
          END LOOP;
          IF veranderd THEN
            NEW := jsonb_populate_record(NEW, j);
          END IF;
          RETURN NEW;
        END
    $f$""",
]


def sql_migraties(tabel, kolommen):
    """Trigger op tabel(kolommen) en eenmalig schoonmaken van wat er al staat. Bestaat de tabel (nog) niet, dan niets."""
    for x in [tabel, *kolommen]:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", x), x
    args = ", ".join(f"'{k}'" for k in kolommen)
    zet = ", ".join(f"{k} = bm_schoon({k})" for k in kolommen)
    waar = " OR ".join(f"{k} ~ '<|&(lt|gt|quot|amp|#39|apos|nbsp);'" for k in kolommen)
    return FUNCTIES + [f"""DO $d$ BEGIN
        IF to_regclass('{tabel}') IS NOT NULL THEN
          DROP TRIGGER IF EXISTS bm_schoon ON {tabel};
          CREATE TRIGGER bm_schoon BEFORE INSERT OR UPDATE ON {tabel} FOR EACH ROW EXECUTE FUNCTION bm_schoon_kolommen({args});
          UPDATE {tabel} SET {zet} WHERE {waar};
        END IF;
    END $d$"""]
