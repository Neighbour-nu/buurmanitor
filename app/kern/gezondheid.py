"""Gezondheid van de server en de portal: geheugen, schijf, belasting, back-ups en taken.

Geheugen en schijf komen van de server zelf (/proc/meminfo en het bestandssysteem). De back-upmappen van
de Doellijn, Promonitor en Buurmanitor zijn alleen-lezen in de container gekoppeld onder /backups.
"""
import datetime as dt
import glob
import os
import shutil

from . import config

GRENS_GEHEUGEN = 75   # procent: daarboven verruimen we de server (ontwerp 6.2)
GRENS_SCHIJF = 85     # procent
GRENS_BACKUP_UUR = 36  # een nachtelijke back-up ouder dan dit is te oud


def geheugen():
    waarden = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for regel in f:
                naam, rest = regel.split(":", 1)
                waarden[naam] = int(rest.split()[0]) * 1024
    except OSError:
        return None
    totaal, beschikbaar = waarden.get("MemTotal", 0), waarden.get("MemAvailable", 0)
    if not totaal:
        return None
    gebruikt = totaal - beschikbaar
    return {"totaal": totaal, "gebruikt": gebruikt, "procent": round(gebruikt / totaal * 100)}


def schijf(pad="/"):
    try:
        s = shutil.disk_usage(pad)
    except OSError:
        return None
    return {"totaal": s.total, "gebruikt": s.used, "vrij": s.free, "procent": round(s.used / s.total * 100)}


def belasting():
    try:
        return os.getloadavg()
    except OSError:
        return None


def backups(basis=None):
    """Per back-upmap: aantal, nieuwste bestand en hoe oud dat is."""
    basis = basis or config.BACKUP_MAP
    uit = []
    for map_ in sorted(glob.glob(os.path.join(basis, "*"))):
        bestanden = sorted(glob.glob(os.path.join(map_, "*.sql.gz")), key=os.path.getmtime)
        laatst = dt.datetime.fromtimestamp(os.path.getmtime(bestanden[-1]), config.TZ) if bestanden else None
        uur = (dt.datetime.now(config.TZ) - laatst).total_seconds() / 3600 if laatst else None
        uit.append({"naam": os.path.basename(map_), "aantal": len(bestanden), "laatst": laatst,
                    "grootte": os.path.getsize(bestanden[-1]) if bestanden else 0,
                    "uur": uur, "ok": uur is not None and uur <= GRENS_BACKUP_UUR})
    return uit


def gb(n):
    return f"{n / 1024 ** 3:.1f} GB".replace(".", ",")


def controle(c):
    """Taak voor de planner: maak meldingen bij te veel geheugen, een volle schijf of te oude back-ups."""
    from . import meldingen  # pylint: disable=import-outside-toplevel
    vandaag = dt.date.today().isoformat()
    punten = []
    g = geheugen()
    if g and g["procent"] >= GRENS_GEHEUGEN:
        meldingen.melden(c, "belangrijk", "gezondheid", f"Geheugen {g['procent']} procent in gebruik",
                         "Komt dit vaker voor, dan is het tijd om de server te verruimen.", "/gezondheid",
                         sleutel=f"geheugen-{vandaag}")
        punten.append("geheugen")
    s = schijf()
    if s and s["procent"] >= GRENS_SCHIJF:
        meldingen.melden(c, "belangrijk", "gezondheid", f"Schijf {s['procent']} procent vol", "", "/gezondheid",
                         sleutel=f"schijf-{vandaag}")
        punten.append("schijf")
    for b in backups():
        if not b["ok"]:
            meldingen.melden(c, "urgent", "gezondheid", f"Back-up {b['naam']} is te oud of ontbreekt",
                             "De nachtelijke back-up is niet gelukt. Kijk in backup.log in de back-upmap.", "/gezondheid",
                             sleutel=f"backup-{b['naam']}-{vandaag}")
            punten.append(f"back-up {b['naam']}")
    return "alles goed" if not punten else "aandacht: " + ", ".join(punten)
