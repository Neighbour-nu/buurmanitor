"""Instellingen die de werkruimte ZEROHERO in Buurmanitor nodig heeft, naast die van de Doellijn zelf.

ACHTERGROND: de achtergronddraden (gegevens ophalen bij Google, MailerLite, YouTube, Meta, Threads, Ticket Tailor,
WooCommerce en de Claude-rekening, en het weekplan van de adviseur). In de oude Doellijn startten ze pas als iemand
het dashboard of de adviseur opende; hier starten ze bij het opstarten, maar alleen als ZEROHERO_ACHTERGROND=1.
Tijdens de proef staat dit uit, zodat de oude Doellijn en de portal niet allebei dezelfde gegevens ophalen.
"""
import os

ACHTERGROND = os.environ.get("ZEROHERO_ACHTERGROND", "0") != "0"
PORTAL_HOST = os.environ.get("PORTAL_HOST", "buurmanitor.neighbour.nu").lower()
BASIS = os.environ.get("ZEROHERO_BASIS_URL", f"https://{PORTAL_HOST}/zerohero").rstrip("/")
AUTH_URL = os.environ.get("AUTH_URL", "https://auth.neighbour.nu")
