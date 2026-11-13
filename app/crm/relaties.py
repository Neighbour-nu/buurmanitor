"""Relaties uit de mail: per afzender één contactpersoon, per (zakelijk) maildomein één organisatie.

Bram bewaart van elke mail de afzender. Elk half uur zet Buurmanitor die om in contacten: naam, adres, hoe vaak en
wanneer het laatst gemaild. Automatische afzenders (noreply, nieuwsbrieven, notificaties) en je eigen adressen tellen
niet mee. Een adres bij een zakelijk domein (niet gmail.com, hotmail.com en dergelijke) krijgt een organisatie; die
naam kun je zelf verbeteren. Wat je zelf invult (naam, functie, organisatie, telefoon) overschrijft Buurmanitor niet.
"""
import datetime as dt
import re

from ..kern import planner

AUTOMATISCH = re.compile(
    r"(^|[.\-_+])(no-?reply|do-?not-?reply|noreply|mailer-daemon|postmaster|bounce[s]?|notifications?|notificaties?|nieuwsbrief|"
    r"newsletter|news|info-?mail|mailing|marketing|updates?|alerts?|support-?noreply|calendar-notification|automated|digest)"
    r"([.\-_+@]|$)", re.I)
VRIJE_DOMEINEN = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.nl", "outlook.com", "outlook.nl", "live.com", "live.nl", "msn.com",
    "icloud.com", "me.com", "mac.com", "yahoo.com", "yahoo.nl", "ziggo.nl", "kpnmail.nl", "kpnplanet.nl", "planet.nl",
    "home.nl", "hetnet.nl", "chello.nl", "xs4all.nl", "zonnet.nl", "online.nl", "telfort.nl", "tele2.nl", "upcmail.nl",
    "proton.me", "protonmail.com", "gmx.com", "gmx.net", "aol.com", "casema.nl", "quicknet.nl", "caiway.nl", "solcon.nl",
}


def is_automatisch(adres, naam="", nieuwsbrief=False):
    """Een afzender die geen mens is. Pure functie."""
    adres = (adres or "").lower()
    if nieuwsbrief or "@" not in adres:
        return True
    lokaal = adres.split("@", 1)[0]
    return bool(AUTOMATISCH.search(lokaal + "@") or re.search(r"\b(no ?reply|notification|nieuwsbrief|newsletter)\b", naam or "", re.I))


def organisatie_domein(adres):
    """Het domein voor een organisatie, of '' bij een vrij mailadres. 'jan@mail.upperspire.com' -> 'upperspire.com'.
    Pure functie."""
    domein = (adres or "").lower().rsplit("@", 1)[-1].strip(">. ")
    if not domein or domein in VRIJE_DOMEINEN or "." not in domein:
        return ""
    delen = domein.split(".")
    # tweede niveau zoals co.uk of com.au: drie delen houden
    return ".".join(delen[-3:]) if len(delen) >= 3 and delen[-2] in ("co", "com", "org", "ac", "gov", "net") else ".".join(delen[-2:])


def organisatie_naam(domein):
    """'upperspire.com' -> 'Upperspire', 'helmond.nl' -> 'Helmond', 'iv-professional.nl' -> 'Iv-professional'. Pure functie."""
    kern = domein.split(".")[0] if domein else ""
    return kern[:1].upper() + kern[1:]


def bijwerken(c, sinds=None):
    """Contacten en organisaties bijwerken uit de mail die Bram heeft gezien. Geeft (nieuwe contacten, bijgewerkt)."""
    eigen = {r["a"] for r in c.execute("SELECT lower(account) AS a FROM post_koppelingen WHERE account <> ''").fetchall()}
    rijen = c.execute("""SELECT lower(b.van_adres) AS adres, max(b.van_naam) FILTER (WHERE b.van_naam <> '') AS naam,
                                count(*) AS aantal, max(b.ontvangen) AS laatst, bool_and(b.nieuwsbrief) AS nieuwsbrief,
                                mode() WITHIN GROUP (ORDER BY k.domein) AS domein
                         FROM post_berichten b JOIN post_koppelingen k ON k.id = b.koppeling_id
                         WHERE b.van_adres LIKE '%%@%%' AND (%s::timestamptz IS NULL OR b.ontvangen >= %s)
                         GROUP BY lower(b.van_adres)""", (sinds, sinds)).fetchall()
    nieuw = bij = 0
    activiteiten = {r["code"] for r in c.execute("SELECT code FROM crm_activiteiten").fetchall()}
    for r in rijen:
        if r["adres"] in eigen or is_automatisch(r["adres"], r["naam"], r["nieuwsbrief"]):
            continue
        org_id = None
        domein = organisatie_domein(r["adres"])
        if domein:
            org = c.execute("SELECT id FROM crm_organisaties WHERE lower(domein) = %s", (domein,)).fetchone()
            org_id = org["id"] if org else c.execute(
                "INSERT INTO crm_organisaties (naam, domein, website) VALUES (%s, %s, %s) RETURNING id",
                (organisatie_naam(domein), domein, f"https://{domein}")).fetchone()["id"]
        totaal = c.execute("SELECT count(*) AS n, max(ontvangen) AS m FROM post_berichten WHERE lower(van_adres) = %s",
                           (r["adres"],)).fetchone()
        bestaand = c.execute("SELECT id FROM crm_contacten WHERE lower(email) = %s", (r["adres"],)).fetchone()
        if bestaand:
            c.execute("""UPDATE crm_contacten SET mails = %s, laatste_mail = %s, naam = CASE WHEN naam = '' THEN %s ELSE naam END,
                            organisatie_id = coalesce(organisatie_id, %s) WHERE id = %s""",
                      (totaal["n"], totaal["m"], r["naam"] or "", org_id, bestaand["id"]))
            bij += 1
        else:
            c.execute("""INSERT INTO crm_contacten (naam, email, organisatie_id, activiteit, bron, mails, laatste_mail)
                         VALUES (%s, %s, %s, %s, 'mail', %s, %s)""",
                      (r["naam"] or r["adres"].split("@")[0], r["adres"], org_id, r["domein"] if r["domein"] in activiteiten else "",
                       totaal["n"], totaal["m"]))
            nieuw += 1
    return nieuw, bij


def contact_voor_adres(c, adres, naam=""):
    """Het contact bij een mailadres; maakt het aan als het er nog niet is (ook voor automatische afzenders, als je er
    zelf een kans van maakt). Geeft de rij."""
    adres = (adres or "").strip().lower()
    if not adres or "@" not in adres:
        return None
    r = c.execute("SELECT * FROM crm_contacten WHERE lower(email) = %s", (adres,)).fetchone()
    if r:
        return r
    domein = organisatie_domein(adres)
    org_id = None
    if domein:
        org = c.execute("SELECT id FROM crm_organisaties WHERE lower(domein) = %s", (domein,)).fetchone()
        org_id = org["id"] if org else c.execute("INSERT INTO crm_organisaties (naam, domein, website) VALUES (%s, %s, %s) RETURNING id",
                                                 (organisatie_naam(domein), domein, f"https://{domein}")).fetchone()["id"]
    return c.execute("""INSERT INTO crm_contacten (naam, email, organisatie_id, bron) VALUES (%s, %s, %s, 'mail') RETURNING *""",
                     (naam or adres.split("@")[0], adres, org_id)).fetchone()


@planner.taak("relaties", elke=dt.timedelta(minutes=30))
def taak_relaties(c):
    laatst = c.execute("SELECT laatst FROM taken WHERE naam = 'relaties'").fetchone()
    sinds = (laatst["laatst"] - dt.timedelta(days=1)) if laatst and laatst["laatst"] else None
    nieuw, bij = bijwerken(c, sinds)
    return f"{nieuw} nieuwe contacten, {bij} bijgewerkt" if nieuw or bij else "geen nieuwe afzenders"
