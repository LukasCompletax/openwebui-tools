#!/usr/bin/env bash
# Ersetzt den laufenden Container "open-webui" durch einen neuen aus open-webui-ocr:local.
#
# Übernommen werden Datenvolume, Port, host.docker.internal, Neustartregel und alle
# Umgebungsvariablen des alten Containers (inklusive WEBUI_SECRET_KEY, damit Logins
# gültig bleiben). Die Umgebung wird über eine Prozess-Substitution durchgereicht und
# nie in eine Datei geschrieben.
#
# Der alte Container wird nicht gelöscht, sondern gestoppt und in "open-webui-old"
# umbenannt. Rückweg: rollback.sh
set -euo pipefail

OLD=open-webui
NEW_IMAGE=open-webui-ocr:local

if ! docker image inspect "$NEW_IMAGE" >/dev/null 2>&1; then
  echo "Image $NEW_IMAGE fehlt. Zuerst build.sh ausführen." >&2
  exit 1
fi
if docker container inspect open-webui-old >/dev/null 2>&1; then
  echo "Es gibt schon einen Container open-webui-old. Zuerst aufräumen (docker rm open-webui-old) oder rollback.sh." >&2
  exit 1
fi

VOLUME=$(docker inspect "$OLD" --format '{{range .Mounts}}{{if eq .Destination "/app/backend/data"}}{{.Name}}{{end}}{{end}}')
PORT=$(docker inspect "$OLD" --format '{{range $p,$b := .HostConfig.PortBindings}}{{range $b}}{{.HostPort}}{{end}}{{end}}')
RESTART=$(docker inspect "$OLD" --format '{{.HostConfig.RestartPolicy.Name}}')
[[ -n "$VOLUME" && -n "$PORT" ]] || { echo "Volume oder Port des alten Containers nicht ermittelbar." >&2; exit 1; }

echo "alt: $OLD  volume=$VOLUME  port=$PORT  restart=$RESTART"
echo "neu: $NEW_IMAGE"
echo "Stoppe $OLD ..."
docker stop "$OLD" >/dev/null
docker rename "$OLD" open-webui-old

echo "Starte neuen Container ..."
docker run -d \
  --name "$OLD" \
  --restart "${RESTART:-always}" \
  -p "${PORT}:8080" \
  --add-host host.docker.internal:host-gateway \
  -v "${VOLUME}:/app/backend/data" \
  --env-file <(docker inspect open-webui-old --format '{{range .Config.Env}}{{println .}}{{end}}') \
  "$NEW_IMAGE" >/dev/null

echo "Warte auf Start ..."
for _ in $(seq 1 60); do
  if docker exec "$OLD" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)" >/dev/null 2>&1; then
    echo "Open WebUI antwortet. OCR-Werkzeuge:"
    docker exec "$OLD" sh -c 'ocrmypdf --version && tesseract --list-langs | tr "\n" " "; echo'
    echo "Fertig. Alter Container liegt gestoppt als open-webui-old bereit (rollback.sh)."
    exit 0
  fi
  sleep 2
done
echo "Neuer Container antwortet nicht. Logs: docker logs open-webui. Rückweg: rollback.sh" >&2
exit 1
