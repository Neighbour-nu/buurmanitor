"""Menu, schermbeschrijvingen en fasering van Promonitor (volgens de functionele beschrijving v0.5)."""

MENU = [
    ("vandaag", "/", "Vandaag"),
    ("inbox", "/inbox", "Inbox"),
    ("agenda", "/agenda", "Agenda"),
    ("bijdragen", "/bijdragen", "Bijdragen"),
    ("vragen", "/vragen", "Raadsvragen"),
    ("concepten", "/concepten", "Concepten"),
    ("fractie", "/fractie", "Fractie"),
    ("acties", "/acties", "Acties"),
    ("whaller", "/whaller", "Whaller"),
    ("dossiers", "/dossiers", "Dossiers"),
    ("bewaking", "/bewaking", "Bewaking"),
    ("dashboard", "/dashboard", "Dashboard"),
    ("adviseur", "/adviseur", "Adviseur"),
]

# Schermen die nog gebouwd worden. Vandaag, Dossiers en Instellingen hebben een eigen route in main.py.
SCHERMEN = {
    "bewaking": {
        "pad": "/bewaking", "titel": "Bewaking", "fase": 3, "onderdelen": "M10",
        "uitleg": "Toezeggingen, moties, coalitiebeloften en raadskaders, met een signaal als iets verloopt of afwijkt.",
        "straks": [
            "Toezeggingen met datum en bron, en het onderscheid tussen toezegging en intentie.",
            "Zelfbindende kaders zoals de Omgevingsvisie 2040 en de Nota Investeringsbeleid.",
        ],
    },
}

FASEN = [
    (0, "Fundament", "Inloggen, huisstijl, schermen, instellingen, kennisbank, sjablonen, mail en Telegram", "klaar"),
    (1, "Basis", "Vaste Whaller-berichten, raadsinformatie, nieuwsradar, Vandaag, ochtendbriefing, dossierkaarten, bijdragenatelier, adviseur en raadsvragen", "klaar, in gebruik"),
    (2, "Fractie", "Agenda, fractievergadering, acties en besluiten, feedback, dashboard, stemadvies en begrotingshulp", "klaar, in gebruik"),
    (3, "Bewaken en regio", "Bewakingsmonitor en regio", "gepland"),
    (4, "Whaller", "Overige Whaller-redactie en eventueel de Whaller-API", "gepland"),
]

GROEPEN = [
    ("Vergaderingen en agenda", ("fractie_", "raad_", "whaller_", "agenda_")),
    ("Briefing en Claude", ("briefing_", "claude_")),
    ("Whaller rode sfeer: raad en commissie", ("rood_",)),
    ("Whaller groene sfeer: fractie", ("groen_",)),
]


def groepeer_instellingen(rijen, volgorde=()):
    """Deelt de instellingen in groepen, in de volgorde van STANDAARD_INSTELLINGEN."""
    plek = {k: i for i, k in enumerate(volgorde)}
    rijen = sorted(rijen, key=lambda r: (plek.get(r["sleutel"], len(plek)), r["sleutel"]))
    uit, gebruikt = [], set()
    for naam, voorvoegsels in GROEPEN:
        leden = [r for r in rijen if r["sleutel"].startswith(voorvoegsels)]
        gebruikt.update(r["sleutel"] for r in leden)
        if leden:
            uit.append((naam, leden))
    # Lange teksten (profiel en filterregels) staan op het scherm Filter.
    rest = [r for r in rijen if r["sleutel"] not in gebruikt and r["sleutel"] not in ("nieuws_profiel", "filter_regels")]
    if rest:
        uit.append(("Overig", rest))
    return uit
