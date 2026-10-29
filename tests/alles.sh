#!/usr/bin/env bash
# Alle tests, elk deel in een eigen proces: de portal en de werkruimte Politiek lezen bij het laden hun instellingen
# uit de omgeving, dus in één pytest-proces zitten ze elkaar in de weg.
#
#   bash tests/alles.sh
#
# Met TEST_DATABASE_URL en TEST_POLITIEK_DATABASE_URL (twee lege, wegwerpbare databases) draaien ook de
# database-, scherm- en koppelingstests; zonder worden die overgeslagen.
set -euo pipefail
cd "$(dirname "$0")/.."
fout=0
for deel in tests/test_kern.py tests/politiek tests/test_portal_politiek.py; do
  echo "== $deel"
  python -m pytest -q "$deel" || fout=1
done
[ "$fout" = 0 ] && echo "ALLE TESTS GOED" || { echo "ER ZIJN FOUTEN"; exit 1; }
