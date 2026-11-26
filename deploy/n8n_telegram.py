"""Laat n8n de tikken op keepersnieuws-knoppen doorsturen naar Buurmanitor (optie 2).

De ZEROHERO Doellijn-bot heeft een webhook van een Telegram-trigger in n8n. Dan krijgt alleen n8n de tikken. Dit script
past die ene werkstroom aan, op de server zelf (er wordt niets geëxporteerd of getoond):

  - de Telegram-trigger luistert ook naar "callback_query" (tikken op knoppen);
  - direct na de trigger komt een IF "Keepersnieuws?": tikken op keepersnieuws-knoppen (kn:...) en antwoorden op een
    keepersnieuwsbericht gaan naar Buurmanitor (http://cockpit-app:8000/intern/telegram, met X-Doellijn-Secret);
  - al het andere gaat precies zoals eerst naar de rest van de werkstroom;
  - de werkstroom wordt uit- en weer aangezet, zodat n8n de webhook bij Telegram opnieuw aanmeldt;
  - in de Doellijn gaat "n8n stuurt de tikken door" aan.

  python3 n8n_telegram.py aan       aanpassen (of bijwerken als het al gedaan is)
  python3 n8n_telegram.py terug     de aanpassing weer weghalen
  python3 n8n_telegram.py controle  laat zien hoe het nu staat (zonder n8n-sleutel)

Nodig: een n8n API-sleutel (n8n > Settings > n8n API > Create an API key). Die typ je in; hij wordt niet getoond en
niet bewaard. Vóór elke wijziging komt een reservekopie van de werkstroom in ~/buurmanitor/n8n-backup (alleen voor jou).
"""
import copy
import datetime as dt
import getpass
import json
import os
import subprocess
import sys
import uuid

TRIGGER = "n8n-nodes-base.telegramTrigger"
MERK = "buurmanitor-keepersnieuws"
IF_NAAM = "Keepersnieuws?"
HTTP_NAAM = "Keepersnieuws naar Buurmanitor"
URL = os.environ.get("BM_INTERN_URL", "http://cockpit-app:8000/intern/telegram")
# Waar: een tik op een keepersnieuws-knop, of een antwoord op een keepersnieuwsbericht (die hebben kn:-knoppen of de link
# "Aanpassen in de Doellijn").
VOORWAARDE = ("={{ ($json.callback_query?.data || '').startsWith('kn:')"
              " || JSON.stringify($json.message?.reply_to_message?.reply_markup || {}).includes('\"kn:')"
              " || ($json.message?.reply_to_message?.text || '').includes('Aanpassen in de Doellijn') }}")
INSTELLINGEN = {"saveExecutionProgress", "saveManualExecutions", "saveDataErrorExecution", "saveDataSuccessExecution",
                "executionTimeout", "errorWorkflow", "timezone", "executionOrder", "callerPolicy", "callerIds",
                "timeSavedPerExecution", "availableInMCP"}
NODE_VELDEN = {"id", "name", "webhookId", "disabled", "notesInFlow", "notes", "type", "typeVersion", "executeOnce",
               "alwaysOutputData", "retryOnFail", "maxTries", "waitBetweenTries", "continueOnFail", "onError", "position",
               "parameters", "credentials"}


class Fout(Exception):
    pass


# ---------------------------------------------------------------- de werkstroom aanpassen (pure functies)

def triggers(wf):
    return [n for n in wf.get("nodes") or [] if n.get("type") == TRIGGER and not n.get("disabled")]


def vind(werkstromen, webhook_url):
    """De werkstroom en trigger waarvan de webhook nu op de bot staat (de webhook-URL bevat het webhookId)."""
    for wf in werkstromen:
        for t in triggers(wf):
            if t.get("webhookId") and t["webhookId"] in (webhook_url or ""):
                return wf, t["name"]
    return None, None


def eigen(wf):
    """Onze twee nodes, als ze er al in staan: (if_node, http_node)."""
    gevonden = [n for n in wf.get("nodes") or [] if str(n.get("notes") or "").startswith(MERK)]
    a = next((n for n in gevonden if n.get("type") == "n8n-nodes-base.if"), None)
    b = next((n for n in gevonden if n.get("type") == "n8n-nodes-base.httpRequest"), None)
    return a, b


def _uniek(naam, namen):
    kandidaat, i = naam, 2
    while kandidaat in namen:
        kandidaat, i = f"{naam} {i}", i + 1
    return kandidaat


def _http_parameters(url, geheim):
    return {"method": "POST", "url": url, "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "X-Doellijn-Secret", "value": geheim}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json) }}",
            "options": {"timeout": 15000}}


def koppel(wf, trigger_naam, url, geheim):
    """Geeft (nieuwe werkstroom, veranderd). Staat de koppeling er al, dan worden alleen adres en geheim bijgewerkt."""
    wf = copy.deepcopy(wf)
    a, b = eigen(wf)
    if a and b:
        nieuw = _http_parameters(url, geheim)
        if b.get("parameters") == nieuw:
            return wf, False
        b["parameters"] = nieuw
        return wf, True
    if a or b:
        raise Fout("De werkstroom heeft maar één van de twee keepersnieuws-nodes. Draai eerst 'terug'.")
    t = next((n for n in wf["nodes"] if n.get("name") == trigger_naam), None)
    if not t:
        raise Fout("De Telegram-trigger staat niet meer in de werkstroom.")
    p = t.setdefault("parameters", {})
    updates = p.get("updates")
    updates = list(updates) if isinstance(updates, list) else ([updates] if updates else [])
    erbij = "*" not in updates and "callback_query" not in updates
    if erbij:
        updates.append("callback_query")
    p["updates"] = updates
    namen = {n.get("name") for n in wf["nodes"]}
    if_naam = _uniek(IF_NAAM, namen)
    http_naam = _uniek(HTTP_NAAM, namen | {if_naam})
    x, y = (t.get("position") or [0, 0])[:2]
    wf["nodes"] += [
        {"id": str(uuid.uuid4()), "name": if_naam, "type": "n8n-nodes-base.if", "typeVersion": 1,
         "position": [x + 220, y + 220], "parameters": {"conditions": {"boolean": [{"value1": VOORWAARDE, "value2": True}]}},
         "notes": MERK + (";callback_query" if erbij else "") + ". Waar: naar Buurmanitor. Onwaar: de rest zoals eerst."},
        {"id": str(uuid.uuid4()), "name": http_naam, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.1,
         "position": [x + 460, y + 120], "parameters": _http_parameters(url, geheim), "onError": "continueRegularOutput",
         "notes": MERK + ". Stuurt keepersnieuws-tikken door naar Buurmanitor."},
    ]
    verb = wf.setdefault("connections", {})
    oud = (verb.get(trigger_naam) or {}).get("main") or [[]]
    verb[trigger_naam] = dict(verb.get(trigger_naam) or {}, main=[[{"node": if_naam, "type": "main", "index": 0}]] + oud[1:])
    verb[if_naam] = {"main": [[{"node": http_naam, "type": "main", "index": 0}], list(oud[0] or [])]}
    return wf, True


def ontkoppel(wf):
    """Haalt de koppeling weer weg: de trigger gaat weer rechtstreeks naar wat eerst achter hem zat."""
    wf = copy.deepcopy(wf)
    a, b = eigen(wf)
    if not (a or b):
        return wf, False
    weg = {n["name"] for n in (a, b) if n}
    verb = wf.setdefault("connections", {})
    doelen = ((verb.get(a["name"]) or {}).get("main") or [[], []]) if a else [[], []]
    doelen = list(doelen[1]) if len(doelen) > 1 else []
    voor_if = {bron for bron, soorten in verb.items() if bron not in weg and a
               for uitgangen in (soorten or {}).values() for uit in uitgangen or [] for d in uit or [] if d.get("node") == a["name"]}
    for bron, soorten in list(verb.items()):
        if bron in weg:
            continue
        for soort, uitgangen in (soorten or {}).items():
            nieuw = []
            for uit in uitgangen or []:
                rij = []
                for d in uit or []:
                    if d.get("node") == (a or {}).get("name"):
                        rij += [x for x in doelen if x not in rij]
                    elif d.get("node") not in weg:
                        rij.append(d)
                nieuw.append(rij)
            soorten[soort] = nieuw
    for naam in weg:
        verb.pop(naam, None)
    if a and ";callback_query" in str(a.get("notes") or ""):
        for t in wf["nodes"]:
            if t.get("name") in voor_if and t.get("type") == TRIGGER and isinstance((t.get("parameters") or {}).get("updates"), list):
                t["parameters"]["updates"] = [u for u in t["parameters"]["updates"] if u != "callback_query"]
    wf["nodes"] = [n for n in wf["nodes"] if n.get("name") not in weg]
    return wf, True


def put_body(wf, streng=False):
    """Wat de n8n API bij PUT accepteert. streng: alleen de vaste velden (als n8n over extra velden klaagt)."""
    s = {k: v for k, v in (wf.get("settings") or {}).items() if k in INSTELLINGEN}
    nodes = wf["nodes"]
    if streng:
        s = {k: v for k, v in s.items() if k == "executionOrder"}
        nodes = [{k: v for k, v in n.items() if k in NODE_VELDEN} for n in nodes]
    return {"name": wf["name"], "nodes": nodes, "connections": wf.get("connections") or {}, "settings": s}


# ---------------------------------------------------------------- de server (docker compose)

H = os.path.expanduser("~")
BM = os.path.join(H, "buurmanitor")
COCKPIT = os.path.join(H, "cockpit")
BACKUP = os.path.join(BM, "n8n-backup")

APP_INFO = r"""
import json, os, urllib.request
t = os.environ.get("ZEROHERO_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
uit = {"token": bool(t), "geheim": os.environ.get("ZEROHERO_CONCEPT_SECRET", "")}
if t:
    try:
        r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getWebhookInfo", timeout=20))["result"]
        uit.update(webhook=r.get("url", ""), toegestaan=r.get("allowed_updates"), wachtend=r.get("pending_update_count", 0),
                   laatste_fout=r.get("last_error_message", ""))
    except Exception as e:
        uit["fout"] = str(e).replace(t, "...")
try:
    import psycopg
    with psycopg.connect(os.environ["ZEROHERO_DATABASE_URL"]) as c:
        rij = dict(c.execute("SELECT sleutel, waarde FROM instellingen WHERE sleutel IN ('kn_via_n8n', 'kn_via_n8n_laatst')").fetchall())
    uit.update(via_n8n=rij.get("kn_via_n8n", ""), laatst=rij.get("kn_via_n8n_laatst", ""))
except Exception as e:
    uit["dbfout"] = str(e)[:200]
print(json.dumps(uit))
"""

APP_ZET = r"""
import os, sys, psycopg
with psycopg.connect(os.environ["ZEROHERO_DATABASE_URL"], autocommit=True) as c:
    c.execute("INSERT INTO instellingen (sleutel, waarde) VALUES ('kn_via_n8n', %s) "
              "ON CONFLICT (sleutel) DO UPDATE SET waarde = EXCLUDED.waarde, bijgewerkt = now()", (sys.argv[1],))
print("ok")
"""

N8N_JS = r"""
let d = ''; process.stdin.on('data', c => d += c).on('end', async () => {
  const a = JSON.parse(d);
  const pad = (process.env.N8N_PATH || '/').replace(/\/+$/, '');
  const url = a.url || ('http://127.0.0.1:' + (process.env.N8N_PORT || 5678) + pad + '/api/v1' + a.path);
  const headers = Object.assign({'Content-Type': 'application/json', 'Accept': 'application/json'},
                                a.key ? {'X-N8N-API-KEY': a.key} : {}, a.headers || {});
  try {
    const r = await fetch(url, {method: a.method, headers, body: a.body === undefined ? undefined : JSON.stringify(a.body)});
    const t = await r.text(); let b; try { b = JSON.parse(t); } catch { b = t.slice(0, 500); }
    process.stdout.write(JSON.stringify({status: r.status, body: b}));
  } catch (e) { process.stdout.write(JSON.stringify({status: 0, body: String((e.cause && e.cause.message) || e.message || e)})); }
});
"""


def _draai(map_, args, invoer=""):
    r = subprocess.run(["docker", "compose", *args], cwd=map_, input=invoer, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise Fout(f"docker compose {' '.join(args[:3])} lukte niet: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def app_info():
    uit = _draai(BM, ["exec", "-T", "app", "python", "-"], APP_INFO)
    return json.loads(uit.strip().splitlines()[-1])


def zet_via_n8n(waarde):
    _draai(BM, ["exec", "-T", "app", "python", "-", waarde], APP_ZET)


class N8n:
    def __init__(self, sleutel):
        self.sleutel = sleutel

    def vraag(self, methode, pad=None, body=None, url=None, headers=None):
        a = {"method": methode, "path": pad, "key": None if url else self.sleutel, "url": url, "headers": headers}
        if body is not None:
            a["body"] = body
        uit = _draai(COCKPIT, ["exec", "-T", "n8n", "node", "-e", N8N_JS], json.dumps(a))
        r = json.loads(uit.strip().splitlines()[-1])
        if r["status"] == 0:
            raise Fout(f"n8n niet bereikbaar: {r['body']}")
        return r["status"], r["body"]

    def api(self, methode, pad, body=None):
        status, b = self.vraag(methode, pad, body)
        if status == 401:
            raise Fout("n8n wijst de API-sleutel af. Maak in n8n (Settings > n8n API) een nieuwe en probeer opnieuw.")
        if status == 404 and pad.startswith("/workflows?"):
            raise Fout("De n8n API staat uit of is niet bereikbaar (404). Zet N8N_PUBLIC_API_DISABLED niet op true.")
        if status >= 400:
            raise Fout(f"n8n antwoordt {status} op {methode} {pad.split('?')[0]}: {str(b.get('message') if isinstance(b, dict) else b)[:300]}")
        return b

    def werkstromen(self):
        alles, cursor = [], ""
        for _ in range(50):
            b = self.api("GET", "/workflows?limit=100" + (f"&cursor={cursor}" if cursor else ""))
            alles += b.get("data") or []
            cursor = b.get("nextCursor")
            if not cursor:
                return alles
        return alles

    def opslaan(self, wf):
        try:
            self.api("PUT", f"/workflows/{wf['id']}", put_body(wf))
        except Fout as e:
            if "400" not in str(e):
                raise
            self.api("PUT", f"/workflows/{wf['id']}", put_body(wf, streng=True))

    def opnieuw_aan(self, wid):
        """Uit en aan: n8n meldt de webhook dan opnieuw aan bij Telegram, met de nieuwe soorten updates."""
        self.api("POST", f"/workflows/{wid}/deactivate")
        try:
            self.api("POST", f"/workflows/{wid}/activate")
        except Fout:
            self.api("POST", f"/workflows/{wid}/activate")  # nog één keer


def reservekopie(wf):
    os.makedirs(BACKUP, mode=0o700, exist_ok=True)
    os.chmod(BACKUP, 0o700)
    pad, i = os.path.join(BACKUP, f"werkstroom-{wf['id']}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"), 2
    while os.path.exists(pad):
        pad, i = pad.rsplit(".json", 1)[0].rsplit("~", 1)[0] + f"~{i}.json", i + 1
    oud = os.umask(0o077)
    try:
        with open(pad, "w", encoding="utf-8") as f:
            json.dump(wf, f)
    finally:
        os.umask(oud)
    return pad


# ---------------------------------------------------------------- de stappen

def toon_stand(i):
    print(f"   Webhook op de bot: {'ja (n8n)' if i.get('webhook') else 'nee: Buurmanitor haalt de tikken zelf op'}")
    if i.get("webhook"):
        toeg = i.get("toegestaan")
        print(f"   Telegram stuurt tikken op knoppen door: {'ja' if not toeg or 'callback_query' in toeg else 'nee'}")
        if i.get("laatste_fout"):
            print(f"   Laatste fout van Telegram bij de webhook: {i['laatste_fout']}")
    print(f"   'n8n stuurt de tikken door' in de Doellijn: {'aan' if i.get('via_n8n') == '1' else 'uit'}")
    if i.get("laatst"):
        print(f"   Laatste tik via n8n ontvangen: {i['laatst'][:19].replace('T', ' ')} (UTC)")


def sleutel_vragen():
    s = getpass.getpass("n8n API-sleutel (n8n > Settings > n8n API; wordt niet getoond): ").strip()
    if not s:
        raise Fout("Niets ingevuld; er is niets veranderd.")
    return s


def zoek_werkstroom(n8n, i):
    if not i.get("webhook"):
        raise Fout("Er staat geen webhook op de bot. Dan haalt Buurmanitor de tikken zelf op en is dit niet nodig.")
    print("== De werkstroom met de Telegram-trigger zoeken")
    wf, trigger = vind(n8n.werkstromen(), i["webhook"])
    if not wf:
        raise Fout("Geen n8n-werkstroom gevonden waarvan de Telegram-trigger de webhook van deze bot is.")
    print(f"   Gevonden: \"{wf['name']}\" (trigger \"{trigger}\", {'actief' if wf.get('active') else 'staat uit'})")
    return wf, trigger


def aan():
    print("== Hoe het nu staat")
    i = app_info()
    if i.get("fout"):
        raise Fout(f"Telegram niet bereikbaar vanuit Buurmanitor: {i['fout']}")
    if not i.get("token"):
        raise Fout("Er is geen bot ingesteld. Doe eerst: bash ~/buurmanitor/app/deploy/zerohero-sleutel.sh telegram")
    if not i.get("geheim"):
        raise Fout("ZEROHERO_CONCEPT_SECRET ontbreekt in zerohero.env; zonder dat geheim kan n8n niet bij /intern.")
    toon_stand(i)
    n8n = N8n(sleutel_vragen())
    wf, trigger = zoek_werkstroom(n8n, i)
    nieuw, veranderd = koppel(wf, trigger, URL, i["geheim"])
    if veranderd:
        print(f"   Reservekopie: {reservekopie(wf)}")
        print("== De werkstroom aanpassen")
        n8n.opslaan(nieuw)
        if wf.get("active"):
            n8n.opnieuw_aan(wf["id"])
            print("   Aangepast, en uit en weer aan gezet (de webhook is opnieuw aangemeld)")
        else:
            print("   Aangepast. LET OP: de werkstroom staat uit in n8n; zet hem zelf weer aan.")
    else:
        print("   De koppeling staat er al; niets aan de werkstroom veranderd.")
    print("== Controleren")
    status, b = n8n.vraag("POST", url=URL, headers={"X-Doellijn-Secret": i["geheim"]}, body={"update_id": 0})
    if status != 200:
        raise Fout(f"n8n bereikt {URL} niet goed ({status}). De werkstroom is wel aangepast; draai 'terug' om dat ongedaan te maken.")
    print("   n8n bereikt Buurmanitor met het juiste geheim")
    zet_via_n8n("1")
    na = app_info()
    toon_stand(na)
    toeg = na.get("toegestaan")
    if na.get("webhook") and toeg and "callback_query" not in toeg:
        print("   LET OP: Telegram geeft tikken op knoppen nog niet door aan n8n. Zet de werkstroom in n8n even uit en")
        print("   weer aan, en draai daarna: bash ~/buurmanitor/app/deploy/n8n-telegram.sh controle")
    print()
    print("KLAAR. Nieuwe keepersnieuwsberichten krijgen weer gewone knoppen; een tik gaat via n8n naar Buurmanitor.")
    print("Oudere berichten hebben nog de link-knoppen; die werken ook gewoon.")
    print("Ongedaan maken: bash ~/buurmanitor/app/deploy/n8n-telegram.sh terug")


def terug():
    print("== Hoe het nu staat")
    i = app_info()
    toon_stand(i)
    n8n = N8n(sleutel_vragen())
    wf, _ = zoek_werkstroom(n8n, i)
    nieuw, veranderd = ontkoppel(wf)
    if veranderd:
        print(f"   Reservekopie: {reservekopie(wf)}")
        n8n.opslaan(nieuw)
        if wf.get("active"):
            n8n.opnieuw_aan(wf["id"])
        print("   De keepersnieuws-nodes zijn weg; de trigger gaat weer rechtstreeks naar de rest van de werkstroom")
    else:
        print("   De werkstroom had de koppeling niet; niets veranderd.")
    zet_via_n8n("0")
    print("KLAAR. Keepersnieuwsberichten krijgen weer link-knoppen naar de Doellijn.")


def main(argv):
    stap = (argv[1] if len(argv) > 1 else "aan").lower()
    try:
        if stap == "aan":
            aan()
        elif stap == "terug":
            terug()
        elif stap == "controle":
            toon_stand(app_info())
        else:
            print(__doc__)
            return 1
    except Fout as e:
        print(f"\nGESTOPT: {e}")
        return 1
    except subprocess.TimeoutExpired:
        print("\nGESTOPT: docker reageerde niet binnen twee minuten.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
