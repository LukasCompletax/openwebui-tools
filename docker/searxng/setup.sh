#!/usr/bin/env bash
# Legt das Geheimnis für SearXNG an (einmalig) und startet den Container.
# Die Datei .env bleibt lokal, sie steht in .gitignore.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  secret=$(openssl rand -hex 32)
  printf 'SEARXNG_SECRET=%s\n' "$secret" > .env
  chmod 600 .env
  echo ".env mit neuem Geheimnis angelegt."
fi

docker compose up -d
echo "Warte auf SearXNG ..."
for _ in $(seq 1 30); do
  if curl -fsS -m 3 "http://127.0.0.1:8081/search?q=test&format=json" >/dev/null 2>&1; then
    echo "SearXNG antwortet unter http://127.0.0.1:8081 (JSON-Format aktiv)."
    exit 0
  fi
  sleep 2
done
echo "SearXNG antwortet nicht. Logs: docker logs kanzlei-searxng" >&2
exit 1
