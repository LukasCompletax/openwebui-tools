#!/usr/bin/env bash
# Macht recreate.sh rückgängig: neuen Container entfernen, alten wieder aktivieren.
# Das Datenvolume ist von beiden Containern dasselbe, es geht nichts verloren.
set -euo pipefail

if ! docker container inspect open-webui-old >/dev/null 2>&1; then
  echo "Kein Container open-webui-old vorhanden, nichts zurückzurollen." >&2
  exit 1
fi

echo "Entferne neuen Container open-webui ..."
docker rm -f open-webui >/dev/null 2>&1 || true
docker rename open-webui-old open-webui
docker start open-webui >/dev/null
echo "Alter Container läuft wieder als open-webui."
