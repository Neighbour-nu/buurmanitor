"""De aanpassing van de n8n-werkstroom (pure functies; geen n8n of docker nodig)."""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy"))
import n8n_telegram as nt  # noqa: E402


def werkstroom(updates=("message",), extra_uitgang=False):
    return {
        "id": "abc", "name": "Doellijn bot", "active": True,
        "settings": {"executionOrder": "v1", "binaryMode": "x"},
        "nodes": [
            {"id": "1", "name": "Telegram Trigger", "type": nt.TRIGGER, "typeVersion": 1.1, "webhookId": "wh-123",
             "position": [100, 300], "parameters": {"updates": list(updates)}, "credentials": {"telegramApi": {"id": "9"}}},
            {"id": "2", "name": "Switch", "type": "n8n-nodes-base.switch", "position": [300, 300], "parameters": {}},
            {"id": "3", "name": "Ticket Tailor", "type": "n8n-nodes-base.httpRequest", "position": [500, 300],
             "parameters": {"url": "https://example.org", "headerParameters": {"parameters": [{"name": "X", "value": "geheim"}]}}},
        ],
        "connections": {
            "Telegram Trigger": {"main": [[{"node": "Switch", "type": "main", "index": 0}]] + ([[]] if extra_uitgang else [])},
            "Switch": {"main": [[{"node": "Ticket Tailor", "type": "main", "index": 0}]]},
        },
    }


def test_vind_op_webhook_id():
    wf = werkstroom()
    anders = copy.deepcopy(wf) | {"id": "x"}
    anders["nodes"][0]["webhookId"] = "ander"
    gevonden, trigger = nt.vind([anders, wf], "https://n8n.example.org/webhook/wh-123/webhook")
    assert gevonden["id"] == "abc" and trigger == "Telegram Trigger"
    assert nt.vind([anders], "https://n8n.example.org/webhook/wh-123/webhook") == (None, None)
    assert nt.vind([wf], "") == (None, None)


def test_koppel_zet_if_en_http_ertussen():
    wf = werkstroom()
    nieuw, veranderd = nt.koppel(wf, "Telegram Trigger", "http://cockpit-app:8000/intern/telegram", "s3")
    assert veranderd
    assert wf["nodes"][0]["parameters"]["updates"] == ["message"]  # origineel onaangeroerd
    assert nieuw["nodes"][0]["parameters"]["updates"] == ["message", "callback_query"]
    a, b, _ = nt.eigen(nieuw)
    assert a["type"] == "n8n-nodes-base.if" and a["typeVersion"] == 1
    assert b["parameters"]["url"].endswith("/intern/telegram")
    assert b["parameters"]["headerParameters"]["parameters"] == [{"name": "X-Doellijn-Secret", "value": "s3"}]
    v = nieuw["connections"]
    assert v["Telegram Trigger"]["main"] == [[{"node": a["name"], "type": "main", "index": 0}]]
    assert v[a["name"]]["main"][0] == [{"node": b["name"], "type": "main", "index": 0}]
    assert v[a["name"]]["main"][1] == [{"node": "Switch", "type": "main", "index": 0}]
    assert v["Switch"] == wf["connections"]["Switch"]
    assert "kn:" in a["parameters"]["conditions"]["boolean"][0]["value1"]


def test_koppel_nogmaals_verandert_niets_of_alleen_het_geheim():
    nieuw, _ = nt.koppel(werkstroom(), "Telegram Trigger", "http://u", "s3")
    nogmaals, veranderd = nt.koppel(nieuw, "Telegram Trigger", "http://u", "s3")
    assert not veranderd and len(nogmaals["nodes"]) == 5
    ander, veranderd = nt.koppel(nieuw, "Telegram Trigger", "http://u", "nieuw-geheim")
    assert veranderd and nt.eigen(ander)[1]["parameters"]["headerParameters"]["parameters"][0]["value"] == "nieuw-geheim"


def test_alle_updates_blijft_alle_updates():
    nieuw, _ = nt.koppel(werkstroom(updates=("*",)), "Telegram Trigger", "http://u", "s")
    assert nieuw["nodes"][0]["parameters"]["updates"] == ["*"]
    terug, _ = nt.ontkoppel(nieuw)
    assert terug["nodes"][0]["parameters"]["updates"] == ["*"]


def test_al_callback_query_wordt_bij_terug_niet_weggehaald():
    wf = werkstroom(updates=("message", "callback_query"))
    nieuw, _ = nt.koppel(wf, "Telegram Trigger", "http://u", "s")
    terug, _ = nt.ontkoppel(nieuw)
    assert terug["nodes"][0]["parameters"]["updates"] == ["message", "callback_query"]


def test_terug_geeft_de_oude_werkstroom():
    wf = werkstroom(extra_uitgang=True)
    nieuw, _ = nt.koppel(wf, "Telegram Trigger", "http://u", "s")
    assert nieuw["connections"]["Telegram Trigger"]["main"][1] == []
    terug, veranderd = nt.ontkoppel(nieuw)
    assert veranderd
    assert terug["nodes"] == wf["nodes"]
    assert terug["connections"] == wf["connections"]
    assert nt.ontkoppel(terug) == (terug, False)


def test_unieke_namen():
    wf = werkstroom()
    wf["nodes"].append({"id": "4", "name": nt.IF_NAAM, "type": "n8n-nodes-base.noOp", "parameters": {}})
    nieuw, _ = nt.koppel(wf, "Telegram Trigger", "http://u", "s")
    a, _, _ = nt.eigen(nieuw)
    assert a["name"] == nt.IF_NAAM + " 2"
    assert len({n["name"] for n in nieuw["nodes"]}) == len(nieuw["nodes"])


def test_trigger_zonder_vervolg():
    wf = werkstroom()
    wf["connections"] = {}
    nieuw, _ = nt.koppel(wf, "Telegram Trigger", "http://u", "s")
    a, _, _ = nt.eigen(nieuw)
    assert nieuw["connections"][a["name"]]["main"][1] == []
    terug, _ = nt.ontkoppel(nieuw)
    assert terug["connections"] == {"Telegram Trigger": {"main": [[]]}}


def test_put_body_laat_onbekende_instellingen_weg():
    wf = werkstroom()
    wf["nodes"][0]["extra"] = 1
    b = nt.put_body(wf)
    assert b["settings"] == {"executionOrder": "v1"} and set(b) == {"name", "nodes", "connections", "settings"}
    assert "extra" in b["nodes"][0]
    assert "extra" not in nt.put_body(wf, streng=True)["nodes"][0]


def test_half_gekoppeld_stopt():
    nieuw, _ = nt.koppel(werkstroom(), "Telegram Trigger", "http://u", "s")
    a, _, _ = nt.eigen(nieuw)
    nieuw["nodes"] = [n for n in nieuw["nodes"] if n is not a]
    try:
        nt.koppel(nieuw, "Telegram Trigger", "http://u", "s")
        raise AssertionError("had moeten stoppen")
    except nt.Fout:
        pass
    terug, veranderd = nt.ontkoppel(nieuw)
    assert veranderd and len(terug["nodes"]) == 3


def met_beperking():
    wf = werkstroom(updates=("callback_query", "message"))
    wf["nodes"][0]["parameters"]["additionalFields"] = {"userIds": "111, 222", "download": True}
    return wf


def test_beperking_gaat_van_trigger_naar_toegestaan():
    """n8n laat met 'Restrict to User IDs' geen tikken op knoppen door; die beperking komt nu achter Keepersnieuws?."""
    wf = met_beperking()
    nieuw, _ = nt.koppel(wf, "Telegram Trigger", "http://u", "s")
    assert nieuw["nodes"][0]["parameters"]["additionalFields"] == {"download": True}
    a, b, c = nt.eigen(nieuw)
    assert c and c["name"] == nt.TOEGESTAAN_NAAM
    v = nieuw["connections"]
    assert v[a["name"]]["main"] == [[{"node": b["name"], "type": "main", "index": 0}], [{"node": c["name"], "type": "main", "index": 0}]]
    assert v[c["name"]]["main"] == [[{"node": "Switch", "type": "main", "index": 0}], []]
    voorwaarde = c["parameters"]["conditions"]["boolean"][0]["value1"]
    assert '["111", "222"].includes(' in voorwaarde and "callback_query?.from?.id" in voorwaarde
    terug, _ = nt.ontkoppel(nieuw)
    assert terug["nodes"] == wf["nodes"] and terug["connections"] == wf["connections"]


def test_beperking_bij_bestaande_koppeling():
    wf = met_beperking()
    af = wf["nodes"][0]["parameters"].pop("additionalFields")
    eerst, _ = nt.koppel(wf, "Telegram Trigger", "http://u", "s")  # zoals de eerste versie van het script
    eerst["nodes"][0]["parameters"]["additionalFields"] = af
    nieuw, veranderd = nt.koppel(eerst, "Telegram Trigger", "http://u", "s")
    assert veranderd and nt.eigen(nieuw)[2] and "userIds" not in nieuw["nodes"][0]["parameters"]["additionalFields"]
    nogmaals, veranderd = nt.koppel(nieuw, "Telegram Trigger", "http://u", "s")
    assert not veranderd and nogmaals == nieuw
    terug, _ = nt.ontkoppel(nieuw)
    assert terug["nodes"][0]["parameters"]["additionalFields"]["userIds"] == "111, 222"
    assert terug["connections"]["Telegram Trigger"]["main"] == [[{"node": "Switch", "type": "main", "index": 0}]]
    assert len(terug["nodes"]) == 3
