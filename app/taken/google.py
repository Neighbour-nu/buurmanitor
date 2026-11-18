"""Taken van het takenbord als Google Taken (zichtbaar in Gmail, Google Agenda en de app Google Tasks), in plaats van als
afspraken in de agenda.

- Instelling 'taken_vorm': google_taken (standaard), afspraak (zoals vroeger) of uit.
- Met 'taken_tijd_afspraak' aan blijft een taak met een begintijd een afspraak: dat is tijd die je vrijhoudt.
- De taken komen in een eigen lijst (standaard 'Buurmanitor') van het Google-account van de gekozen agenda, of anders van
  het eerste Google-account met schrijfrechten. Google Taken kent alleen een datum, geen tijd.
- Twee kanten op: vink je een taak in Google af, dan gaat hij in Buurmanitor naar Afgerond; zet je hem in Google terug,
  dan komt hij terug bij Gepland. Een andere titel of datum in Google komt ook op het bord. Verwijder je hem in Google,
  dan gaat hij in Buurmanitor naar Afgerond (en blijft hij uit Google).
"""
import datetime as dt
import json
import urllib.parse

from ..clara import bronnen, sync as agenda_sync
from ..kern import config

API = "https://tasks.googleapis.com/tasks/v1"
SOORT = "Google Taken"
STANDAARD = {"taken_vorm": "google_taken", "taken_tijd_afspraak": "1", "taken_google_lijst": "Buurmanitor", "taken_google_alle": "0",
             "taken_google_account": ""}
VORMEN = (("google_taken", "Als taak in Google Taken (Gmail en Google Agenda)"), ("afspraak", "Als afspraak in de agenda"),
          ("uit", "Niet buiten Buurmanitor"))


class Fout(bronnen.BronFout):
    pass


def instellingen(c):
    rijen = c.execute("SELECT sleutel, waarde FROM instellingen WHERE sleutel = ANY(%s)", (list(STANDAARD),)).fetchall()
    return STANDAARD | {r["sleutel"]: r["waarde"] for r in rijen}


def zet(c, sleutel, waarde):
    c.execute("""INSERT INTO instellingen (sleutel, waarde) VALUES (%s, %s)
                 ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde""", (sleutel, str(waarde)))


def vorm(t, inst, afspraak):
    """Waar deze taak hoort: 'afspraak', 'google' of None. Pure functie; 'afspraak' is de functie die de afspraak maakt."""
    v = inst["taken_vorm"]
    if v == "uit":
        return None
    if v == "afspraak":
        return "afspraak" if afspraak(t) else None
    if inst["taken_tijd_afspraak"] == "1" and t.get("starttijd") and afspraak(t):
        return "afspraak"
    if t.get("startdatum") or t.get("einddatum") or inst["taken_google_alle"] == "1":
        return "google"
    return None


def datum(t):
    """De datum voor Google: de einddatum (deadline), anders de startdatum."""
    return t.get("einddatum") or t.get("startdatum")


def inhoud(t, notities_extra=""):
    """Wat er in Google komt te staan. Pure functie."""
    d = datum(t)
    notities = (t.get("beschrijving") or "").strip()
    if t.get("starttijd") or t.get("eindtijd"):
        tijd = (f"{t['starttijd']:%H.%M}" if t.get("starttijd") else "") + (f"-{t['eindtijd']:%H.%M}" if t.get("eindtijd") else "")
        notities = f"Tijd: {tijd}\n\n{notities}".strip()
    link = f"https://{config.PORTAL_HOST}/taken?bewerk={t['id']}#formulier"
    notities = (notities + f"\n\n[taak:{t['id']}] Uit Buurmanitor" + (f": {link}" if link else "") + notities_extra).strip()
    uit = {"title": (t["titel"] or "(zonder titel)")[:1000], "notes": notities[:8000],
           "status": "completed" if t.get("status") == "afgerond" else "needsAction"}
    if d:
        uit["due"] = f"{d.isoformat()}T00:00:00.000Z"
    return uit


# ---------------------------------------------------------------- Google

def _vraag(toegang, pad, methode=None, body=None):
    try:
        return bronnen._http(API + pad, headers={"Authorization": f"Bearer {toegang}"}, methode=methode, body=body,  # pylint: disable=protected-access
                             soort=SOORT)
    except bronnen.NietGevonden:
        raise
    except bronnen.BronFout as e:
        tekst = str(e)
        if "insufficient" in tekst.lower() or "scope" in tekst.lower():
            raise Fout("Buurmanitor mag nog niet bij je Google Taken. Koppel je Google-account opnieuw: Clara, Agenda-instellingen, "
                       "Google-account koppelen (hetzelfde account; je instellingen blijven staan) en sta daarbij Taken toe.") from e
        if "has not been used" in tekst or "is disabled" in tekst or "SERVICE_DISABLED" in tekst:
            raise Fout("De Google Tasks API staat nog uit in je Google Cloud-project. Zet hem aan: console.cloud.google.com, "
                       "API's en services, Bibliotheek, Google Tasks API, Inschakelen.") from e
        raise


def account(c, t, inst):
    """Het Google-account (agenda_koppelingen-rij) voor deze taak, of None."""
    ks = [k for k in agenda_sync.koppelingen(c, alleen_actief=True) if k["soort"] == "google" and k["schrijven"]]
    voorkeur = []
    if (t.get("doel") or "").split("|")[0].isdigit():
        voorkeur.append(int(t["doel"].split("|")[0]))
    if inst.get("taken_google_account", "").isdigit():
        voorkeur.append(int(inst["taken_google_account"]))
    for kid in voorkeur:
        for k in ks:
            if k["id"] == kid:
                return k
    return ks[0] if ks else None


def lijst(c, k, naam):
    """De id van de takenlijst met deze naam (gemaakt als hij er nog niet is); onthouden per account."""
    sleutel = f"taken_google_lijst_id:{k['id']}:{naam}"
    r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel,)).fetchone()
    if r and r["waarde"]:
        return r["waarde"]
    toegang = agenda_sync.toegang(c, k)
    pagina = ""
    while True:
        j = _vraag(toegang, "/users/@me/lists?maxResults=100" + (f"&pageToken={urllib.parse.quote(pagina)}" if pagina else ""))
        for x in j.get("items") or []:
            if x.get("title") == naam:
                zet(c, sleutel, x["id"])
                return x["id"]
        pagina = j.get("nextPageToken") or ""
        if not pagina:
            break
    lid = _vraag(toegang, "/users/@me/lists", methode="POST", body={"title": naam})["id"]
    zet(c, sleutel, lid)
    return lid


def verwijder(c, t):
    """De Google-taak bij deze taakrij weghalen (stil als hij er al niet meer is)."""
    k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s", (t["agenda_koppeling"],)).fetchone()
    if k and t["agenda_extern"]:
        try:
            _vraag(agenda_sync.toegang(c, k), f"/lists/{urllib.parse.quote(t['agenda_id'])}/tasks/{urllib.parse.quote(t['agenda_extern'])}",
                   methode="DELETE")
        except bronnen.NietGevonden:
            pass


def bijwerken(c, t, inst):
    """De Google-taak maken of bijwerken. Geeft een korte uitslag; fouten gaan als BronFout naar boven."""
    k = account(c, t, inst)
    if not k:
        raise Fout("Er is geen Google-account met schrijfrechten gekoppeld (Clara, Agenda's) om de taak in Google Taken te zetten.")
    lid = lijst(c, k, inst["taken_google_lijst"] or STANDAARD["taken_google_lijst"])
    body = inhoud(t)
    spec = json.dumps({"vorm": "google", "k": k["id"], "lijst": lid, **body}, sort_keys=True)
    zelfde_plek = t["agenda_soort"] == "google_taak" and t["agenda_extern"] and t["agenda_koppeling"] == k["id"] and t["agenda_id"] == lid
    if zelfde_plek and spec == t["agenda_spec"]:
        return ""
    toegang = agenda_sync.toegang(c, k)
    extern = ""
    if zelfde_plek:
        try:
            extern = _vraag(toegang, f"/lists/{urllib.parse.quote(lid)}/tasks/{urllib.parse.quote(t['agenda_extern'])}", methode="PATCH",
                            body=body | ({} if "due" in body else {"due": None}))["id"]
        except bronnen.NietGevonden:
            extern = ""
    elif t["agenda_extern"] and t["agenda_soort"] == "google_taak":
        verwijder(c, t)  # op een andere plek: daar weg
    if not extern:
        extern = _vraag(toegang, f"/lists/{urllib.parse.quote(lid)}/tasks", methode="POST", body=body)["id"]
    c.execute("""UPDATE takenbord SET agenda_soort = 'google_taak', agenda_koppeling = %s, agenda_id = %s, agenda_extern = %s,
                 agenda_spec = %s, agenda_fout = '' WHERE id = %s""", (k["id"], lid, extern, spec, t["id"]))
    return "in Google Taken gezet"


# ---------------------------------------------------------------- terug uit Google

def _uit_google(x):
    due = None
    if x.get("due"):
        try:
            due = dt.date.fromisoformat(x["due"][:10])
        except ValueError:
            due = None
    return {"titel": (x.get("title") or "").strip(), "klaar": x.get("status") == "completed", "datum": due,
            "weg": bool(x.get("deleted"))}


def wijzigingen(t, x):
    """Wat er op het bord moet veranderen door wat er in Google is gebeurd: dict met kolommen (leeg: niets). Pure functie."""
    g = _uit_google(x)
    uit = {}
    if g["weg"] or g["klaar"]:
        if t["status"] != "afgerond":
            uit["status"] = "afgerond"
        if g["weg"]:
            return uit
    elif t["status"] == "afgerond":
        uit["status"] = "gepland"
    if g["titel"] and g["titel"] != t["titel"]:
        uit["titel"] = g["titel"][:200]
    if g["datum"] and g["datum"] != datum(t):
        if t.get("einddatum") or not t.get("startdatum"):
            uit["einddatum"] = g["datum"]
            if t.get("startdatum") and t["startdatum"] > g["datum"]:
                uit["startdatum"] = None
        else:
            uit["startdatum"] = g["datum"]
    return uit


def ophalen(c, nu=None):
    """Wat er in Google Taken is veranderd terughalen naar het bord. Geeft (bekeken, aangepast)."""
    nu = nu or dt.datetime.now(dt.timezone.utc)
    plekken = c.execute("""SELECT DISTINCT agenda_koppeling AS kid, agenda_id AS lijst FROM takenbord
                           WHERE agenda_soort = 'google_taak' AND agenda_extern <> '' AND agenda_koppeling IS NOT NULL""").fetchall()
    bekeken = aangepast = 0
    for p in plekken:
        k = c.execute("SELECT * FROM agenda_koppelingen WHERE id = %s AND actief", (p["kid"],)).fetchone()
        if not k:
            continue
        sleutel = f"taken_google_sinds:{p['kid']}:{p['lijst']}"
        r = c.execute("SELECT waarde FROM instellingen WHERE sleutel = %s", (sleutel,)).fetchone()
        sinds = r["waarde"] if r else (nu - dt.timedelta(days=30)).isoformat()
        toegang = agenda_sync.toegang(c, k)
        pagina, items = "", []
        while True:
            j = _vraag(toegang, f"/lists/{urllib.parse.quote(p['lijst'])}/tasks?" + urllib.parse.urlencode(
                {"showCompleted": "true", "showHidden": "true", "showDeleted": "true", "maxResults": 100, "updatedMin": sinds}
                | ({"pageToken": pagina} if pagina else {})))
            items += j.get("items") or []
            pagina = j.get("nextPageToken") or ""
            if not pagina:
                break
        for x in items:
            t = c.execute("""SELECT * FROM takenbord WHERE agenda_soort = 'google_taak' AND agenda_koppeling = %s AND agenda_id = %s
                             AND agenda_extern = %s""", (p["kid"], p["lijst"], x.get("id"))).fetchone()
            if not t:
                continue
            bekeken += 1
            w = wijzigingen(t, x)
            if w:
                aangepast += 1
                zetten = ", ".join(f"{kolom} = %s" for kolom in w)
                status = w.get("status")
                c.execute(f"""UPDATE takenbord SET {zetten}, bijgewerkt = now(),
                              afgerond = CASE WHEN %s::text = 'afgerond' THEN coalesce(afgerond, now()) WHEN %s::text IS NOT NULL THEN NULL ELSE afgerond END,
                              volgorde = CASE WHEN %s::text IS NOT NULL THEN NULL ELSE volgorde END
                              WHERE id = %s""", (*w.values(), status, status, status, t["id"]))
            if _uit_google(x)["weg"]:  # in Google verwijderd: niet opnieuw aanmaken
                c.execute("""UPDATE takenbord SET agenda_soort = 'google_weg', agenda_extern = '', agenda_spec = '' WHERE id = %s""", (t["id"],))
        zet(c, sleutel, (nu - dt.timedelta(minutes=2)).isoformat())
    return bekeken, aangepast
