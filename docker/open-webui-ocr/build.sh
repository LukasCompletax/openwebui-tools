#!/usr/bin/env bash
# Baut das Image open-webui-ocr:local auf Basis des offiziellen Open-WebUI-Images.
# Mit --pull wird vorher die neueste Basis geholt (Update von Open WebUI selbst).
set -euo pipefail
cd "$(dirname "$0")"

PULL=""
if [[ "${1:-}" == "--pull" ]]; then
  PULL="--pull"
fi

docker build ${PULL} -t open-webui-ocr:local .
echo
docker images open-webui-ocr:local --format 'gebaut: {{.Repository}}:{{.Tag}} {{.Size}} ({{.CreatedSince}})'
