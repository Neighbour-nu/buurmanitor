"""Schermen van het CRM: kansen (een bord per activiteit), projecten, relaties en instellingen; en het maken van een
kans of project vanuit een mail, nieuwsbericht, socialpost of een voorstel van Iris."""
import asyncio
import datetime as dt
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..kern import claude, config, db
from ..kern.web import controleer_herkomst, ctx, templates
from . import model, voorstel

router = APIRouter()
TABS = (("kansen", "Kansen", "/crm"), ("projecten", "Projecten", "/crm/projecten"), ("relaties", "Relaties", "/crm/relaties"),
        ("instellingen", "Instellingen", "/crm/instellingen"))


def _terug(pad, **params):
    params = {k: v for k, v in params.items() if v}
    if not params:
        return RedirectResponse(pad, status_code=303)
    anker = ""
    if "#" in pad:
        pad, anker = pad.split("#", 1)
        anker = "#" + anker
    return RedirectResponse(pad + ("&" if "?" in pad else "?") + urlencode(params) + anker, status_code=303)


def _ctx(request, tab, **kw):
    return ctx(request, "crm", tabs=TABS, tab=tab, model=model, euro_kort=euro_kort, vandaag=dt.datetime.now(config.TZ).date(),
               projectstatus=dict(model.PROJECTSTATUS), **kw)


def euro_kort(x):
    """€ 12.500 (zonder centen als het een rond bedrag is). Pure functie."""
    if x is None:
        return ""
    x = Decimal(x)
    tekst = f"{x:,.2f}" if x != x.to_integral_value() else f"{x:,.0f}"
    return "€ " + tekst.replace(",", "X").replace(".", ",").replace("X", ".")


def _activiteitmap(c):
    return {a["code"]: a for a in model.activiteiten(c, alleen_actief=False)}


def _organisatie(c, naam):
    """Een organisatie op naam: bestaande (hoofdletters maken niet uit) of nieuw. None als leeg."""
    naam = (naam or "").strip()[:200]
    if not naam:
        return None
    r = c.execute("SELECT id FROM crm_organisaties WHERE lower(naam) = lower(%s) ORDER BY id LIMIT 1", (naam,)).fetchone()
    return r["id"] if r else c.execute("INSERT INTO crm_organisaties (naam) VALUES (%s) RETURNING id", (naam,)).fetchone()["id"]


def _contact(c, naam, email, org_id):
    """Een contact op mailadres (of anders op naam): bestaand of nieuw. None als beide leeg."""
    from .relaties import contact_voor_adres  # pylint: disable=import-outside-toplevel
    naam, email = (naam or "").strip()[:200], (email or "").strip()[:200]
    if email and "@" in email:
        r = contact_voor_adres(c, email, naam)
        if org_id and not r["organisatie_id"]:
            c.execute("UPDATE crm_contacten SET organisatie_id = %s WHERE id = %s", (org_id, r["id"]))
        if naam and r["naam"] in ("", email.split("@")[0]):
            c.execute("UPDATE crm_contacten SET naam = %s WHERE id = %s", (naam, r["id"]))
        return r["id"]
    if not naam:
        return None
    r = c.execute("SELECT id FROM crm_contacten WHERE lower(naam) = lower(%s) ORDER BY id LIMIT 1", (naam,)).fetchone()
    return r["id"] if r else c.execute("INSERT INTO crm_contacten (naam, organisatie_id) VALUES (%s, %s) RETURNING id",
                                       (naam, org_id)).fetchone()["id"]


# ---------------------------------------------------------------- kansen

def _kansen(c, waar="true", args=()):
    return c.execute(f"""SELECT k.*, o.naam AS organisatie, p.naam AS contact, p.email AS contact_email, a.naam AS activiteitnaam,
                                a.soort, t.status AS taakstatus, pr.titel AS project
                         FROM crm_kansen k JOIN crm_activiteiten a ON a.code = k.activiteit
                         LEFT JOIN crm_organisaties o ON o.id = k.organisatie_id LEFT JOIN crm_contacten p ON p.id = k.contact_id
                         LEFT JOIN takenbord t ON t.id = k.taak_id LEFT JOIN crm_projecten pr ON pr.id = k.project_id
                         WHERE {waar} ORDER BY k.volgende_datum NULLS LAST, k.bijgewerkt DESC""", args).fetchall()


@router.get("/crm", response_class=HTMLResponse)
def kansen_scherm(request: Request, activiteit: str = "", gesloten: int = 0, melding: str = "", fout: str = ""):
    """Zonder activiteit: het overzicht (per activiteit, en alle volgende stappen). Met activiteit: het bord per fase."""
    with db.pool.connection() as c:
        acts = model.activiteiten(c)
        act = next((a for a in acts if a["code"] == activiteit), None)
        if act:
            lijst = _kansen(c, "k.activiteit = %s AND (k.gesloten IS NULL OR %s OR k.gesloten > now() - interval '30 days')",
                            (act["code"], bool(gesloten)))
        else:
            lijst = _kansen(c, "k.gesloten IS NULL")
        recent = c.execute("""SELECT k.id, k.titel, k.fase, a.soort, k.gesloten FROM crm_kansen k JOIN crm_activiteiten a ON a.code = k.activiteit
                              WHERE k.gesloten > now() - interval '60 days' ORDER BY k.gesloten DESC LIMIT 8""").fetchall()
    per = []
    for a in acts:
        mijn = [k for k in lijst if k["activiteit"] == a["code"]] if not act else []
        per.append({"a": a, "aantal": len(mijn), "waarde": sum((k["waarde"] or 0) for k in mijn),
                    "gewogen": sum(model.gewogen(k) for k in mijn), "achter": sum(1 for k in mijn if model.achter(k, dt.date.today()))})
    kolommen = []
    if act:
        for code, naam, uit in model.fases(act["soort"]):
            kolommen.append({"code": code, "naam": naam, "uitkomst": uit, "kansen": [k for k in lijst if k["fase"] == code]})
    return templates.TemplateResponse("crm_kansen.html", _ctx(
        request, "kansen", acts=acts, act=act, lijst=lijst, per=per, kolommen=kolommen, recent=recent, gesloten=gesloten,
        melding=melding, fout=fout))


def _kans_velden(f, soort):
    fasecodes = [x[0] for x in model.fases(soort)]
    fase = str(f.get("fase") or "")
    return {"titel": str(f.get("titel") or "").strip()[:200], "fase": fase if fase in fasecodes else fasecodes[0],
            "omschrijving": str(f.get("omschrijving") or "").strip()[:4000],
            "waarde": model.bedrag(f.get("waarde")) if soort == "commercieel" else None,
            "kans": model.geheel(f.get("kans"), 0, 100) if soort == "commercieel" else None,
            "verwacht": model.datum(f.get("verwacht")), "volgende_stap": str(f.get("volgende_stap") or "").strip()[:300],
            "volgende_datum": model.datum(f.get("volgende_datum"))}


@router.get("/crm/kans/{kid:int}", response_class=HTMLResponse)
def kans_scherm(request: Request, kid: int, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        rij = _kansen(c, "k.id = %s", (kid,))
        if not rij:
            raise HTTPException(404)
        k = rij[0]
        tijd = c.execute("SELECT * FROM crm_tijdlijn WHERE kans_id = %s ORDER BY tijd DESC, id DESC", (kid,)).fetchall()
        acts = model.activiteiten(c)
    return templates.TemplateResponse("crm_kans.html", _ctx(request, "kansen", k=k, tijd=tijd, acts=acts, melding=melding, fout=fout))


@router.post("/crm/kans/{kid:int}")
async def kans_opslaan(request: Request, kid: int):
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            oud = c.execute("SELECT * FROM crm_kansen WHERE id = %s", (kid,)).fetchone()
            if not oud:
                raise HTTPException(404)
            acts = _activiteitmap(c)
            activiteit = str(f.get("activiteit") or oud["activiteit"])
            activiteit = activiteit if activiteit in acts else oud["activiteit"]
            soort = acts[activiteit]["soort"]
            v = _kans_velden(f, soort)
            if not v["titel"]:
                return "Geef de kans een korte titel."
            org = _organisatie(c, f.get("organisatie"))
            contact = _contact(c, f.get("contact_naam"), f.get("contact_email"), org)
            uitkomst = model.uitkomst(soort, v["fase"])
            k = c.execute("""UPDATE crm_kansen SET titel = %(titel)s, activiteit = %(activiteit)s, fase = %(fase)s, omschrijving = %(omschrijving)s,
                                waarde = %(waarde)s, kans = %(kans)s, verwacht = %(verwacht)s, volgende_stap = %(volgende_stap)s,
                                volgende_datum = %(volgende_datum)s, organisatie_id = %(org)s, contact_id = %(contact)s,
                                gesloten = CASE WHEN %(uitkomst)s <> '' THEN coalesce(gesloten, now()) END, bijgewerkt = now()
                             WHERE id = %(id)s RETURNING *""",
                          v | {"activiteit": activiteit, "org": org, "contact": contact, "uitkomst": uitkomst, "id": kid}).fetchone()
            if oud["fase"] != k["fase"]:
                model.tijdlijn(c, "fase", f"Naar {model.fase_naam(soort, k['fase'])}", kans_id=kid)
            model.volgende_stap_taak(c, k)
            return ""
    fout = await asyncio.to_thread(doe)
    return _terug(f"/crm/kans/{kid}", **({"fout": fout} if fout else {"melding": "Opgeslagen."}))


@router.post("/crm/kans/{kid:int}/fase")
async def kans_fase(request: Request, kid: int):
    """Een kans naar een andere fase (slepen op het bord of de knoppen op de kans)."""
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            k = c.execute("SELECT k.*, a.soort FROM crm_kansen k JOIN crm_activiteiten a ON a.code = k.activiteit WHERE k.id = %s",
                          (kid,)).fetchone()
            if not k:
                raise HTTPException(404)
            fase = str(f.get("fase") or "")
            if fase not in [x[0] for x in model.fases(k["soort"])]:
                raise HTTPException(400)
            uitkomst = model.uitkomst(k["soort"], fase)
            k = c.execute("""UPDATE crm_kansen SET fase = %s, bijgewerkt = now(),
                                gesloten = CASE WHEN %s <> '' THEN coalesce(gesloten, now()) END
                             WHERE id = %s RETURNING *""", (fase, uitkomst, kid)).fetchone() | {"soort": k["soort"]}
            model.tijdlijn(c, "fase", f"Naar {model.fase_naam(k['soort'], fase)}", kans_id=kid)
            model.volgende_stap_taak(c, k)
            return k, uitkomst
    k, uitkomst = await asyncio.to_thread(doe)
    if request.headers.get("X-CRM"):
        return {"ok": True}
    if uitkomst == "gewonnen" and not k["project_id"]:
        return _terug(f"/crm/kans/{kid}#project", melding=f"'{k['titel']}' is {model.fase_naam(k['soort'], k['fase']).lower()}. "
                      "Wil je er een project van maken?")
    return _terug(str(f.get("terug") or f"/crm/kans/{kid}") if str(f.get("terug") or "").startswith("/crm") else f"/crm/kans/{kid}",
                  melding=f"'{k['titel']}' staat nu bij {model.fase_naam(k['soort'], k['fase'])}.")


@router.post("/crm/kans/{kid:int}/notitie")
async def kans_notitie(request: Request, kid: int):
    return await _notitie(request, kans_id=kid)


@router.post("/crm/project/{pid:int}/notitie")
async def project_notitie(request: Request, pid: int):
    return await _notitie(request, project_id=pid)


async def _notitie(request, kans_id=None, project_id=None):
    controleer_herkomst(request)
    f = await request.form()
    tekst = str(f.get("tekst") or "").strip()[:4000]
    url = str(f.get("url") or "").strip()[:1000]
    if url and not url.startswith(("http://", "https://")):
        url = ""
    pad = f"/crm/kans/{kans_id}#tijdlijn" if kans_id else f"/crm/project/{project_id}#tijdlijn"
    if not tekst and not url:
        return _terug(pad)

    def doe():
        with db.pool.connection() as c:
            tabel = "crm_kansen" if kans_id else "crm_projecten"
            if not c.execute(f"SELECT 1 FROM {tabel} WHERE id = %s", (kans_id or project_id,)).fetchone():
                raise HTTPException(404)
            model.tijdlijn(c, "link" if url else "notitie", tekst[:120] if url else "", tekst, url, kans_id=kans_id, project_id=project_id)
            c.execute(f"UPDATE {tabel} SET bijgewerkt = now() WHERE id = %s", (kans_id or project_id,))
    await asyncio.to_thread(doe)
    return _terug(pad)


@router.post("/crm/tijdlijn/{tid:int}/weg")
def tijdlijn_weg(request: Request, tid: int):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        r = c.execute("DELETE FROM crm_tijdlijn WHERE id = %s RETURNING kans_id, project_id", (tid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return _terug(f"/crm/kans/{r['kans_id']}#tijdlijn" if r["kans_id"] else f"/crm/project/{r['project_id']}#tijdlijn")


@router.post("/crm/kans/{kid:int}/weg")
def kans_weg(request: Request, kid: int):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        k = c.execute("DELETE FROM crm_kansen WHERE id = %s RETURNING *", (kid,)).fetchone()
        if not k:
            raise HTTPException(404)
        if k["taak_id"]:
            c.execute("DELETE FROM takenbord WHERE id = %s AND status <> 'afgerond'", (k["taak_id"],))
    return _terug(f"/crm?activiteit={k['activiteit']}", melding=f"Kans '{k['titel']}' verwijderd.")


@router.post("/crm/kans/{kid:int}/project")
def kans_naar_project(request: Request, kid: int):
    """Van een (gewonnen) kans een project maken: titel, activiteit, organisatie en contact gaan mee; de waarde wordt
    de begrote opbrengst."""
    controleer_herkomst(request)
    with db.pool.connection() as c:
        k = c.execute("SELECT * FROM crm_kansen WHERE id = %s", (kid,)).fetchone()
        if not k:
            raise HTTPException(404)
        if k["project_id"]:
            return _terug(f"/crm/project/{k['project_id']}")
        pid = c.execute("""INSERT INTO crm_projecten (titel, activiteit, status, omschrijving, organisatie_id, contact_id, budget_opbrengst, kans_id)
                           VALUES (%s, %s, 'gepland', %s, %s, %s, %s, %s) RETURNING id""",
                        (k["titel"], k["activiteit"], k["omschrijving"], k["organisatie_id"], k["contact_id"], k["waarde"], kid)).fetchone()["id"]
        c.execute("UPDATE crm_kansen SET project_id = %s WHERE id = %s", (pid, kid))
        c.execute("UPDATE takenbord SET project_id = %s WHERE kans_id = %s", (pid, kid))
        model.tijdlijn(c, "notitie", "Project gemaakt", f"Uit de kans '{k['titel']}'.", project_id=pid)
        model.tijdlijn(c, "notitie", "Project gemaakt", f"Project '{k['titel']}' gemaakt.", kans_id=kid)
    return _terug(f"/crm/project/{pid}", melding="Project gemaakt. Vul de datum en het budget aan, of kies een sjabloon voor de vaste taken.")


# ---------------------------------------------------------------- nieuw (ook vanuit een bron)

def _bron_uit_mail(c, bid):
    b = c.execute("""SELECT b.*, k.domein, k.naam AS koppeling FROM post_berichten b JOIN post_koppelingen k ON k.id = b.koppeling_id
                     WHERE b.id = %s""", (bid,)).fetchone()
    if not b:
        raise HTTPException(404)
    return {"soort": "mail", "bericht_id": str(b["id"]), "titel": b["onderwerp"], "van": f"{b['van_naam']} <{b['van_adres']}>",
            "van_naam": b["van_naam"], "van_adres": b["van_adres"], "tekst": b["fragment"] or "", "activiteit": b["domein"],
            "url": f"/bram/bericht/{b['id']}", "koppeling": b["koppeling"]}


def _vooraf(c, qp):
    """De velden van het formulier vooraf ingevuld vanuit de bron: een mail (id), een nieuwsbericht of socialpost
    (titel, url, tekst en van in de link), of een voorstel van Iris."""
    codes = {a["code"] for a in model.activiteiten(c)}
    bron = str(qp.get("bron") or "handmatig")
    v = {"wat": qp.get("wat") if qp.get("wat") in ("kans", "project") else "kans", "bron": bron if bron in model.BRONNEN else "handmatig"}
    b = None
    if v["bron"] == "mail" and str(qp.get("id") or "").isdigit():
        b = _bron_uit_mail(c, int(qp["id"]))
        v |= {"titel": b["titel"], "contact_naam": b["van_naam"], "contact_email": b["van_adres"], "bericht_id": b["bericht_id"],
              "bron_titel": b["titel"], "bron_url": b["url"], "omschrijving": b["tekst"][:600]}
        if b["activiteit"] in codes:
            v["activiteit"] = b["activiteit"]
        from .relaties import organisatie_domein  # pylint: disable=import-outside-toplevel
        if organisatie_domein(b["van_adres"]):
            r = c.execute("""SELECT o.naam FROM crm_contacten p JOIN crm_organisaties o ON o.id = p.organisatie_id
                             WHERE lower(p.email) = lower(%s)""", (b["van_adres"],)).fetchone()
            if r:
                v["organisatie"] = r["naam"]
    elif v["bron"] in ("nieuws", "social"):
        url = str(qp.get("url") or "")
        b = {"soort": "nieuwsbericht" if v["bron"] == "nieuws" else "socialpost", "titel": str(qp.get("titel") or "")[:300],
             "van": str(qp.get("van") or "")[:200], "tekst": str(qp.get("tekst") or "")[:2000],
             "url": url if url.startswith(("http://", "https://")) else "", "activiteit": str(qp.get("stroom") or "")}
        v |= {"titel": b["titel"][:120], "bron_titel": b["titel"], "bron_url": b["url"], "omschrijving": b["tekst"][:600]}
        stroom = b["activiteit"]
        if stroom == "keepers":
            v["activiteit"] = "zerohero"
        elif stroom in ("politiek", "landelijk"):
            v["activiteit"] = "politiek"
    if str(qp.get("voorstel") or "").isdigit():
        r = c.execute("SELECT * FROM crm_voorstellen WHERE id = %s", (int(qp["voorstel"]),)).fetchone()
        if r:
            v |= {k: w for k, w in r["gegevens"].items() if w} | {"voorstel": str(r["id"])}
            if v.get("bericht_id") and not b:
                try:
                    b = _bron_uit_mail(c, int(v["bericht_id"]))
                    v |= {"bron": "mail", "bron_titel": b["titel"], "bron_url": b["url"]}
                    v.setdefault("contact_email", b["van_adres"])
                except HTTPException:
                    v.pop("bericht_id", None)
    # Wat in de link staat (na een fout, of ingevuld door Iris) gaat voor.
    for k in (*voorstel.VELDEN, "fase", "kans", "verwacht"):
        if qp.get(k) and k not in ("bron", "wat", "bericht_id"):
            v[k] = str(qp.get(k))[:4000]
    if v.get("activiteit") not in codes:
        v.pop("activiteit", None)
    return v, b


@router.get("/crm/nieuw", response_class=HTMLResponse)
def nieuw_scherm(request: Request, fout: str = ""):
    with db.pool.connection() as c:
        v, b = _vooraf(c, request.query_params)
        acts = model.activiteiten(c)
        open_kansen = c.execute("SELECT id, titel FROM crm_kansen WHERE gesloten IS NULL ORDER BY bijgewerkt DESC LIMIT 60").fetchall()
        open_projecten = c.execute("""SELECT id, titel FROM crm_projecten WHERE status NOT IN ('afgerond', 'gestopt')
                                      ORDER BY bijgewerkt DESC LIMIT 60""").fetchall()
    return templates.TemplateResponse("crm_nieuw.html", _ctx(
        request, "kansen", v=v, b=b, acts=acts, open_kansen=open_kansen, open_projecten=open_projecten, fout=fout,
        sleutel_ok=bool(config.ANTHROPIC_API_KEY), qs=str(request.url.query)))


@router.post("/crm/nieuw/iris", response_class=HTMLResponse)
async def nieuw_iris(request: Request):
    """Laat Iris het formulier invullen op basis van de bron."""
    controleer_herkomst(request)
    f = await request.form()
    qs = str(f.get("qs") or "")
    from urllib.parse import parse_qsl  # pylint: disable=import-outside-toplevel
    qp = dict(parse_qsl(qs))

    def doe():
        with db.pool.connection() as c:
            v, b = _vooraf(c, qp)
            if not b:
                return "Er is geen bron om in te vullen."
            try:
                ingevuld = voorstel.invullen(c, b)
            except (claude.ClaudeFout, ValueError) as e:
                return f"Iris kon het niet invullen: {e}"
            return v | {k: w for k, w in ingevuld.items() if w and k not in ("bron", "bron_titel", "bron_url", "bericht_id")}
    uit = await asyncio.to_thread(doe)
    if isinstance(uit, str):
        return _terug("/crm/nieuw?" + qs, fout=uit)
    return _terug("/crm/nieuw?" + urlencode({k: w for k, w in (qp | uit | {"iris": "1"}).items() if w}))


@router.post("/crm/nieuw")
async def nieuw_opslaan(request: Request):
    """Een kans of project aanmaken (met de bron op de tijdlijn), of de bron koppelen aan een bestaande kans of project."""
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            acts = _activiteitmap(c)
            bericht_id = int(f.get("bericht_id")) if str(f.get("bericht_id") or "").isdigit() else None
            if bericht_id and not c.execute("SELECT 1 FROM post_berichten WHERE id = %s", (bericht_id,)).fetchone():
                bericht_id = None
            bron = str(f.get("bron") or "handmatig")
            bron_titel, bron_url = str(f.get("bron_titel") or "")[:300], str(f.get("bron_url") or "")[:1000]
            if bron_url and not bron_url.startswith(("http://", "https://", "/bram/bericht/")):
                bron_url = ""
            koppel = str(f.get("koppel") or "")
            if koppel:  # aan een bestaande kans of project
                soort, _, ref = koppel.partition(":")
                if soort not in ("kans", "project") or not ref.isdigit():
                    raise HTTPException(400)
                ids = {"kans_id": int(ref)} if soort == "kans" else {"project_id": int(ref)}
                tabel = "crm_kansen" if soort == "kans" else "crm_projecten"
                if not c.execute(f"SELECT 1 FROM {tabel} WHERE id = %s", (int(ref),)).fetchone():
                    raise HTTPException(404)
                model.tijdlijn(c, bron if bron in ("mail", "nieuws", "social") else "link", bron_titel, str(f.get("omschrijving") or "")[:600],
                               bron_url, bericht_id=bericht_id, **ids)
                c.execute(f"UPDATE {tabel} SET bijgewerkt = now() WHERE id = %s", (int(ref),))
                return f"/crm/{soort}/{ref}#tijdlijn", "Gekoppeld.", ""
            wat = "project" if f.get("wat") == "project" else "kans"
            activiteit = str(f.get("activiteit") or "")
            if activiteit not in acts:
                return "", "", "Kies bij welke activiteit het hoort."
            titel = str(f.get("titel") or "").strip()[:200]
            if not titel:
                return "", "", "Geef een korte titel."
            org = _organisatie(c, f.get("organisatie"))
            contact = _contact(c, f.get("contact_naam"), f.get("contact_email"), org)
            soort = acts[activiteit]["soort"]
            ids = {}
            if wat == "kans":
                v = _kans_velden(f, soort)
                k = c.execute("""INSERT INTO crm_kansen (titel, activiteit, fase, omschrijving, organisatie_id, contact_id, waarde, kans,
                                     verwacht, volgende_stap, volgende_datum)
                                 VALUES (%(titel)s, %(activiteit)s, %(fase)s, %(omschrijving)s, %(org)s, %(contact)s, %(waarde)s, %(kans)s,
                                     %(verwacht)s, %(volgende_stap)s, %(volgende_datum)s) RETURNING *""",
                              v | {"titel": titel, "activiteit": activiteit, "org": org, "contact": contact}).fetchone()
                model.volgende_stap_taak(c, k)
                ids, pad = {"kans_id": k["id"]}, f"/crm/kans/{k['id']}"
            else:
                p = c.execute("""INSERT INTO crm_projecten (titel, activiteit, status, omschrijving, datum, organisatie_id, contact_id)
                                 VALUES (%s, %s, 'gepland', %s, %s, %s, %s) RETURNING *""",
                              (titel, activiteit, str(f.get("omschrijving") or "")[:4000], model.datum(f.get("datum")), org, contact)).fetchone()
                stap = str(f.get("volgende_stap") or "").strip()
                if stap:
                    c.execute("""INSERT INTO takenbord (titel, status, prio, einddatum, project_id) VALUES (%s, 'gepland', 'middel', %s, %s)""",
                              (stap[:200], model.datum(f.get("volgende_datum")), p["id"]))
                ids, pad = {"project_id": p["id"]}, f"/crm/project/{p['id']}"
            if bron in ("mail", "nieuws", "social") and (bron_titel or bron_url or bericht_id):
                model.tijdlijn(c, bron, bron_titel, "", bron_url, bericht_id=bericht_id, **ids)
            if str(f.get("voorstel") or "").isdigit():
                c.execute("UPDATE crm_voorstellen SET status = 'gemaakt', kans_id = %s, project_id = %s WHERE id = %s",
                          (ids.get("kans_id"), ids.get("project_id"), int(f["voorstel"])))
            return pad, f"{'Kans' if wat == 'kans' else 'Project'} '{titel}' aangemaakt.", ""
    pad, melding, fout = await asyncio.to_thread(doe)
    if fout:
        terug = {k: str(f.get(k) or "") for k in (*voorstel.VELDEN, "fase", "kans", "verwacht", "voorstel")}
        return _terug("/crm/nieuw?" + urlencode({k: w for k, w in terug.items() if w}), fout=fout)
    return _terug(pad, melding=melding)


@router.post("/crm/voorstel/{vid:int}/weg")
def voorstel_weg(request: Request, vid: int):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        r = c.execute("UPDATE crm_voorstellen SET status = 'weg' WHERE id = %s RETURNING gesprek_id", (vid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return _terug(f"/iris?g={r['gesprek_id']}#laatste")


# ---------------------------------------------------------------- projecten

def _projecten(c, waar="true", args=()):
    return c.execute(f"""SELECT p.*, o.naam AS organisatie, ct.naam AS contact, ct.email AS contact_email, a.naam AS activiteitnaam,
                                (SELECT count(*) FROM takenbord t WHERE t.project_id = p.id AND t.status <> 'afgerond') AS open_taken,
                                (SELECT count(*) FROM takenbord t WHERE t.project_id = p.id) AS alle_taken,
                                (SELECT min(t.einddatum) FROM takenbord t WHERE t.project_id = p.id AND t.status <> 'afgerond') AS eerstvolgende
                         FROM crm_projecten p JOIN crm_activiteiten a ON a.code = p.activiteit
                         LEFT JOIN crm_organisaties o ON o.id = p.organisatie_id LEFT JOIN crm_contacten ct ON ct.id = p.contact_id
                         WHERE {waar} ORDER BY CASE p.status WHEN 'actief' THEN 0 WHEN 'gepland' THEN 1 WHEN 'idee' THEN 2 ELSE 3 END,
                                  p.datum NULLS LAST, p.bijgewerkt DESC""", args).fetchall()


@router.get("/crm/projecten", response_class=HTMLResponse)
def projecten_scherm(request: Request, activiteit: str = "", alles: int = 0, melding: str = ""):
    with db.pool.connection() as c:
        acts = model.activiteiten(c)
        waar, args = ["(%s OR p.status NOT IN ('afgerond', 'gestopt'))"], [bool(alles)]
        if activiteit:
            waar.append("p.activiteit = %s")
            args.append(activiteit)
        lijst = _projecten(c, " AND ".join(waar), tuple(args))
        sjablonen = c.execute("SELECT id, naam FROM crm_sjablonen ORDER BY naam").fetchall()
    return templates.TemplateResponse("crm_projecten.html", _ctx(
        request, "projecten", acts=acts, lijst=lijst, activiteit=activiteit, alles=alles, melding=melding, sjablonen=sjablonen,
        statussen=model.PROJECTSTATUS))


@router.get("/crm/project/{pid:int}", response_class=HTMLResponse)
def project_scherm(request: Request, pid: int, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        rij = _projecten(c, "p.id = %s", (pid,))
        if not rij:
            raise HTTPException(404)
        p = rij[0]
        taken = c.execute("""SELECT * FROM takenbord WHERE project_id = %s
                             ORDER BY status = 'afgerond', einddatum NULLS LAST, id""", (pid,)).fetchall()
        tijd = c.execute("SELECT * FROM crm_tijdlijn WHERE project_id = %s ORDER BY tijd DESC, id DESC", (pid,)).fetchall()
        kansen = c.execute("SELECT id, titel, fase FROM crm_kansen WHERE project_id = %s", (pid,)).fetchall()
        sjablonen = c.execute("SELECT * FROM crm_sjablonen ORDER BY naam").fetchall()
        acts = model.activiteiten(c)
    return templates.TemplateResponse("crm_project.html", _ctx(
        request, "projecten", p=p, taken=taken, tijd=tijd, kansen=kansen, sjablonen=sjablonen, acts=acts,
        statussen=model.PROJECTSTATUS, melding=melding, fout=fout))


@router.post("/crm/project/{pid:int}")
async def project_opslaan(request: Request, pid: int):
    """Nieuw (pid 0) of bijwerken."""
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            acts = _activiteitmap(c)
            titel = str(f.get("titel") or "").strip()[:200]
            activiteit = str(f.get("activiteit") or "")
            if not titel or activiteit not in acts:
                return pid, "Geef een titel en kies een activiteit."
            status = str(f.get("status") or "gepland")
            org = _organisatie(c, f.get("organisatie"))
            v = {"titel": titel, "activiteit": activiteit, "status": status if status in dict(model.PROJECTSTATUS) else "gepland",
                 "omschrijving": str(f.get("omschrijving") or "").strip()[:4000], "datum": model.datum(f.get("datum")),
                 "start": model.datum(f.get("start")), "eind": model.datum(f.get("eind")), "org": org,
                 "contact": _contact(c, f.get("contact_naam"), f.get("contact_email"), org)}
            v |= {k: model.bedrag(f.get(k)) for k in ("budget_kosten", "budget_opbrengst", "kosten", "opbrengst")}
            if pid:
                r = c.execute("""UPDATE crm_projecten SET titel = %(titel)s, activiteit = %(activiteit)s, status = %(status)s,
                                    omschrijving = %(omschrijving)s, datum = %(datum)s, start = %(start)s, eind = %(eind)s,
                                    organisatie_id = %(org)s, contact_id = %(contact)s, budget_kosten = %(budget_kosten)s,
                                    budget_opbrengst = %(budget_opbrengst)s, kosten = %(kosten)s, opbrengst = %(opbrengst)s, bijgewerkt = now()
                                 WHERE id = %(id)s RETURNING id""", v | {"id": pid}).fetchone()
                if not r:
                    raise HTTPException(404)
                return pid, ""
            nieuw = c.execute("""INSERT INTO crm_projecten (titel, activiteit, status, omschrijving, datum, start, eind, organisatie_id,
                                    contact_id, budget_kosten, budget_opbrengst, kosten, opbrengst)
                                 VALUES (%(titel)s, %(activiteit)s, %(status)s, %(omschrijving)s, %(datum)s, %(start)s, %(eind)s, %(org)s,
                                    %(contact)s, %(budget_kosten)s, %(budget_opbrengst)s, %(kosten)s, %(opbrengst)s) RETURNING *""", v).fetchone()
            sid = str(f.get("sjabloon") or "")
            if sid.isdigit():
                s = c.execute("SELECT * FROM crm_sjablonen WHERE id = %s", (int(sid),)).fetchone()
                if s:
                    model.sjabloon_toepassen(c, nieuw, s)
            return nieuw["id"], ""
    nieuw_id, fout = await asyncio.to_thread(doe)
    if fout:
        return _terug(f"/crm/project/{pid}" if pid else "/crm/projecten#nieuw", fout=fout)
    return _terug(f"/crm/project/{nieuw_id}", melding="Opgeslagen." if pid else "Project gemaakt.")


@router.post("/crm/project/{pid:int}/taak")
async def project_taak(request: Request, pid: int):
    """Snel een taak toevoegen aan het project (die staat dan ook op het takenbord)."""
    controleer_herkomst(request)
    f = await request.form()
    titel = str(f.get("titel") or "").strip()[:200]
    if not titel:
        return _terug(f"/crm/project/{pid}#taken")

    def doe():
        with db.pool.connection() as c:
            if not c.execute("SELECT 1 FROM crm_projecten WHERE id = %s", (pid,)).fetchone():
                raise HTTPException(404)
            c.execute("""INSERT INTO takenbord (titel, status, prio, einddatum, project_id) VALUES (%s, 'gepland', %s, %s, %s)""",
                      (titel, f.get("prio") if f.get("prio") in ("laag", "middel", "hoog") else "middel", model.datum(f.get("einddatum")), pid))
            c.execute("UPDATE crm_projecten SET bijgewerkt = now() WHERE id = %s", (pid,))
    await asyncio.to_thread(doe)
    return _terug(f"/crm/project/{pid}#taken")


@router.post("/crm/project/{pid:int}/sjabloon")
async def project_sjabloon(request: Request, pid: int):
    """Een sjabloon toepassen (de vaste taken erbij), of van de taken van dit project een sjabloon maken."""
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            p = c.execute("SELECT * FROM crm_projecten WHERE id = %s", (pid,)).fetchone()
            if not p:
                raise HTTPException(404)
            if f.get("maak"):
                naam = str(f.get("naam") or "").strip()[:120] or p["titel"]
                taken = c.execute("SELECT titel, einddatum FROM takenbord WHERE project_id = %s", (pid,)).fetchall()
                if not taken:
                    return "", "Dit project heeft nog geen taken om een sjabloon van te maken."
                c.execute("INSERT INTO crm_sjablonen (naam, activiteit, taken) VALUES (%s, %s, %s)",
                          (naam, p["activiteit"], model.json_dump(model.taken_als_sjabloon(taken, p["datum"]))))
                return f"Sjabloon '{naam}' gemaakt met {len(taken)} taken" + ("" if p["datum"] else " (zonder projectdatum: zonder dagen)") + ".", ""
            s = c.execute("SELECT * FROM crm_sjablonen WHERE id = %s", (int(f.get("sjabloon") or 0),)).fetchone()
            if not s:
                return "", "Kies een sjabloon."
            n = model.sjabloon_toepassen(c, p, s)
            return f"{n} taken uit '{s['naam']}' toegevoegd" + ("" if p["datum"] else "; vul de projectdatum in voor de deadlines") + ".", ""
    melding, fout = await asyncio.to_thread(doe)
    return _terug(f"/crm/project/{pid}#taken", melding=melding, fout=fout)


@router.post("/crm/project/{pid:int}/weg")
def project_weg(request: Request, pid: int):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        p = c.execute("DELETE FROM crm_projecten WHERE id = %s RETURNING *", (pid,)).fetchone()
        if not p:
            raise HTTPException(404)
    return _terug("/crm/projecten", melding=f"Project '{p['titel']}' verwijderd. De taken staan nog op het takenbord.")


# ---------------------------------------------------------------- relaties

@router.get("/crm/relaties", response_class=HTMLResponse)
def relaties_scherm(request: Request, q: str = "", activiteit: str = "", verborgen: int = 0, melding: str = ""):
    q = q.strip()[:100]
    zoek = f"%{q}%"
    with db.pool.connection() as c:
        contacten = c.execute("""SELECT p.*, o.naam AS organisatie,
                                    (SELECT count(*) FROM crm_kansen k WHERE k.contact_id = p.id AND k.gesloten IS NULL) AS kansen
                                 FROM crm_contacten p LEFT JOIN crm_organisaties o ON o.id = p.organisatie_id
                                 WHERE p.verborgen = %s AND (%s = '' OR p.naam ILIKE %s OR p.email ILIKE %s OR o.naam ILIKE %s)
                                   AND (%s = '' OR p.activiteit = %s)
                                 ORDER BY (SELECT count(*) FROM crm_kansen k WHERE k.contact_id = p.id AND k.gesloten IS NULL) DESC,
                                          p.laatste_mail DESC NULLS LAST, p.naam LIMIT 200""",
                              (bool(verborgen), q, zoek, zoek, zoek, activiteit, activiteit)).fetchall()
        organisaties = c.execute("""SELECT o.*, count(p.id) AS contacten, max(p.laatste_mail) AS laatst FROM crm_organisaties o
                                    LEFT JOIN crm_contacten p ON p.organisatie_id = o.id AND NOT p.verborgen
                                    WHERE (%s = '' OR o.naam ILIKE %s OR o.domein ILIKE %s)
                                    GROUP BY o.id ORDER BY max(p.laatste_mail) DESC NULLS LAST, o.naam LIMIT 60""", (q, zoek, zoek)).fetchall()
        acts = model.activiteiten(c)
        totaal = c.execute("SELECT count(*) AS n FROM crm_contacten WHERE NOT verborgen").fetchone()["n"]
    return templates.TemplateResponse("crm_relaties.html", _ctx(
        request, "relaties", contacten=contacten, organisaties=organisaties, q=q, activiteit=activiteit, verborgen=verborgen,
        acts=acts, totaal=totaal, melding=melding))


@router.get("/crm/contact/{cid:int}", response_class=HTMLResponse)
def contact_scherm(request: Request, cid: int, melding: str = ""):
    with db.pool.connection() as c:
        p = c.execute("""SELECT p.*, o.naam AS organisatie FROM crm_contacten p LEFT JOIN crm_organisaties o ON o.id = p.organisatie_id
                         WHERE p.id = %s""", (cid,)).fetchone()
        if not p:
            raise HTTPException(404)
        mails = c.execute("""SELECT b.id, b.onderwerp, b.ontvangen, b.status, k.naam AS koppeling FROM post_berichten b
                             JOIN post_koppelingen k ON k.id = b.koppeling_id WHERE %s <> '' AND lower(b.van_adres) = lower(%s)
                             ORDER BY b.ontvangen DESC LIMIT 15""", (p["email"], p["email"])).fetchall()
        kansen = _kansen(c, "k.contact_id = %s", (cid,))
        projecten = _projecten(c, "p.contact_id = %s", (cid,))
        acts = model.activiteiten(c)
    return templates.TemplateResponse("crm_contact.html", _ctx(
        request, "relaties", p=p, mails=mails, kansen=kansen, projecten=projecten, acts=acts, melding=melding))


@router.post("/crm/contact/{cid:int}")
async def contact_opslaan(request: Request, cid: int):
    """Nieuw (cid 0) of bijwerken."""
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            acts = _activiteitmap(c)
            email = str(f.get("email") or "").strip().lower()[:200]
            v = {"naam": str(f.get("naam") or "").strip()[:200], "email": email if "@" in email else "",
                 "functie": str(f.get("functie") or "").strip()[:200], "telefoon": str(f.get("telefoon") or "").strip()[:60],
                 "notitie": str(f.get("notitie") or "").strip()[:4000], "org": _organisatie(c, f.get("organisatie")),
                 "activiteit": str(f.get("activiteit") or "") if str(f.get("activiteit") or "") in acts else "",
                 "verborgen": f.get("verborgen") == "1"}
            if not v["naam"] and not v["email"]:
                return cid, "Geef een naam of mailadres."
            if v["email"]:
                dubbel = c.execute("SELECT id FROM crm_contacten WHERE lower(email) = %s AND id <> %s", (v["email"], cid)).fetchone()
                if dubbel:
                    return cid, "Er is al een contact met dit mailadres."
            if cid:
                r = c.execute("""UPDATE crm_contacten SET naam = %(naam)s, email = %(email)s, functie = %(functie)s, telefoon = %(telefoon)s,
                                    notitie = %(notitie)s, organisatie_id = %(org)s, activiteit = %(activiteit)s, verborgen = %(verborgen)s
                                 WHERE id = %(id)s RETURNING id""", v | {"id": cid}).fetchone()
                if not r:
                    raise HTTPException(404)
                return cid, ""
            return c.execute("""INSERT INTO crm_contacten (naam, email, functie, telefoon, notitie, organisatie_id, activiteit)
                                VALUES (%(naam)s, %(email)s, %(functie)s, %(telefoon)s, %(notitie)s, %(org)s, %(activiteit)s) RETURNING id""",
                             v).fetchone()["id"], ""
    nieuw_id, fout = await asyncio.to_thread(doe)
    if fout:
        return _terug(f"/crm/contact/{cid}" if cid else "/crm/relaties", melding=fout)
    return _terug(f"/crm/contact/{nieuw_id}", melding="Opgeslagen.")


@router.get("/crm/organisatie/{oid:int}", response_class=HTMLResponse)
def organisatie_scherm(request: Request, oid: int, melding: str = ""):
    with db.pool.connection() as c:
        o = c.execute("SELECT * FROM crm_organisaties WHERE id = %s", (oid,)).fetchone()
        if not o:
            raise HTTPException(404)
        contacten = c.execute("SELECT * FROM crm_contacten WHERE organisatie_id = %s ORDER BY laatste_mail DESC NULLS LAST", (oid,)).fetchall()
        kansen = _kansen(c, "k.organisatie_id = %s", (oid,))
        projecten = _projecten(c, "p.organisatie_id = %s", (oid,))
    return templates.TemplateResponse("crm_organisatie.html", _ctx(
        request, "relaties", o=o, contacten=contacten, kansen=kansen, projecten=projecten, melding=melding))


@router.post("/crm/organisatie/{oid:int}")
async def organisatie_opslaan(request: Request, oid: int):
    controleer_herkomst(request)
    f = await request.form()
    naam = str(f.get("naam") or "").strip()[:200]
    website = str(f.get("website") or "").strip()[:300]
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website

    def doe():
        with db.pool.connection() as c:
            if not naam:
                return "Geef een naam."
            if not c.execute("UPDATE crm_organisaties SET naam = %s, website = %s, notitie = %s WHERE id = %s RETURNING id",
                             (naam, website, str(f.get("notitie") or "")[:4000], oid)).fetchone():
                raise HTTPException(404)
            return "Opgeslagen."
    return _terug(f"/crm/organisatie/{oid}", melding=await asyncio.to_thread(doe))


# ---------------------------------------------------------------- instellingen

@router.get("/crm/instellingen", response_class=HTMLResponse)
def instellingen_scherm(request: Request, bewerk: int = 0, melding: str = "", fout: str = ""):
    with db.pool.connection() as c:
        acts = model.activiteiten(c, alleen_actief=False)
        sjablonen = c.execute("SELECT * FROM crm_sjablonen ORDER BY naam").fetchall()
        stand = c.execute("SELECT laatst, resultaat FROM taken WHERE naam = 'relaties'").fetchone()
    bewerkt = next((s for s in sjablonen if s["id"] == bewerk), None)
    return templates.TemplateResponse("crm_instellingen.html", _ctx(
        request, "instellingen", acts=acts, sjablonen=sjablonen, bewerkt=bewerkt, melding=melding, fout=fout, stand=stand))


@router.post("/crm/activiteiten")
async def activiteiten_opslaan(request: Request):
    """Namen, soort en aan/uit van de activiteiten; en een nieuwe activiteit."""
    controleer_herkomst(request)
    f = await request.form()

    def doe():
        with db.pool.connection() as c:
            for a in model.activiteiten(c, alleen_actief=False):
                naam = str(f.get(f"naam_{a['code']}") or "").strip()[:80]
                soort = str(f.get(f"soort_{a['code']}") or a["soort"])
                c.execute("UPDATE crm_activiteiten SET naam = %s, soort = %s, actief = %s WHERE code = %s",
                          (naam or a["naam"], soort if soort in model.FASES else a["soort"], f.get(f"actief_{a['code']}") == "1", a["code"]))
            nieuw = str(f.get("nieuw_naam") or "").strip()[:80]
            if nieuw:
                import re  # pylint: disable=import-outside-toplevel
                code = re.sub(r"[^a-z0-9]+", "", nieuw.lower())[:20] or "activiteit"
                while c.execute("SELECT 1 FROM crm_activiteiten WHERE code = %s", (code,)).fetchone():
                    code += "x"
                soort = str(f.get("nieuw_soort") or "commercieel")
                c.execute("""INSERT INTO crm_activiteiten (code, naam, soort, volgorde)
                             VALUES (%s, %s, %s, (SELECT coalesce(max(volgorde), 0) + 1 FROM crm_activiteiten))""",
                          (code, nieuw, soort if soort in model.FASES else "commercieel"))
    await asyncio.to_thread(doe)
    return _terug("/crm/instellingen#activiteiten", melding="Opgeslagen.")


@router.post("/crm/sjablonen")
async def sjabloon_opslaan(request: Request):
    controleer_herkomst(request)
    f = await request.form()
    sid = int(f.get("id") or 0) if str(f.get("id") or "0").isdigit() else 0
    naam = str(f.get("naam") or "").strip()[:120]
    taken = model.sjabloon_regels(f.get("taken"))
    if not naam or not taken:
        return _terug("/crm/instellingen" + (f"?bewerk={sid}" if sid else "") + "#sjablonen", fout="Geef het sjabloon een naam en minstens één taak.")

    def doe():
        with db.pool.connection() as c:
            acts = _activiteitmap(c)
            act = str(f.get("activiteit") or "")
            act = act if act in acts else None
            if sid:
                if not c.execute("UPDATE crm_sjablonen SET naam = %s, activiteit = %s, taken = %s WHERE id = %s RETURNING id",
                                 (naam, act, model.json_dump(taken), sid)).fetchone():
                    raise HTTPException(404)
            else:
                c.execute("INSERT INTO crm_sjablonen (naam, activiteit, taken) VALUES (%s, %s, %s)", (naam, act, model.json_dump(taken)))
    await asyncio.to_thread(doe)
    return _terug("/crm/instellingen#sjablonen", melding=f"Sjabloon '{naam}' opgeslagen met {len(taken)} taken.")


@router.post("/crm/sjablonen/{sid:int}/weg")
def sjabloon_weg(request: Request, sid: int):
    controleer_herkomst(request)
    with db.pool.connection() as c:
        c.execute("DELETE FROM crm_sjablonen WHERE id = %s", (sid,))
    return _terug("/crm/instellingen#sjablonen", melding="Sjabloon verwijderd.")


@router.post("/crm/relaties/bijwerken")
def relaties_bijwerken(request: Request):
    controleer_herkomst(request)
    from ..kern import planner  # pylint: disable=import-outside-toplevel
    planner.nu_draaien(db.achtergrond, "relaties")
    return _terug("/crm/relaties", melding="Buurmanitor haalt de afzenders uit je mail. Ververs over een minuut.")
