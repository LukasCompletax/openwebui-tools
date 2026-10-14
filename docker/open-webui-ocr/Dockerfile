# Open WebUI mit OCR-Werkzeugen für das Tool ocr_scan.
#
# Basis ist das offizielle Image. Hinzu kommen ocrmypdf (mit Tesseract, Ghostscript,
# qpdf) und die Sprachpakete Deutsch und Englisch. Das Tool ruft nur die
# Kommandozeile `ocrmypdf` auf, die Python-Umgebung von Open WebUI bleibt unberührt.
#
# Bauen:    docker/open-webui-ocr/build.sh
# Wechseln: docker/open-webui-ocr/recreate.sh   (ersetzt den laufenden Container)
# Zurück:   docker/open-webui-ocr/rollback.sh   (alten Container wieder aktivieren)

ARG BASE_IMAGE=ghcr.io/open-webui/open-webui:main
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ocrmypdf \
        tesseract-ocr-deu \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Kurzer Selbsttest beim Bauen: Binary und Sprachen vorhanden.
RUN ocrmypdf --version && tesseract --list-langs | grep -E "^(deu|eng)$"
