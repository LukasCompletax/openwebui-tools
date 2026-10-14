# openwebui-tools

Eigene Werkzeuge für die lokale Open-WebUI-Instanz der Kanzlei. Jedes Werkzeug
liegt in einem eigenen Ordner als einzelne Python-Datei, die in Open WebUI
unter **Workspace → Tools** eingefügt wird, plus Tests, die ohne Open WebUI laufen.

Grundsatz: Das Sprachmodell macht nur, was es gut kann (Sprache). Lesen,
Zerlegen, Rendern und Speichern laufen deterministisch im Werkzeug.

## Werkzeuge

| Ordner | Zweck | Status |
|---|---|---|
| `pdf_translate/` | PDF oder DOCX in eine Zielsprache übersetzen, Ergebnis als PDF oder DOCX zum Download | Installiert, Probelauf mit qwen3.8:27b-mlx erfolgreich |
| `create_document/` | Aus Markdown-Text des Modells eine Datei erzeugen: DOCX, PDF, XLSX, MD, TXT, CSV | Installiert, Probelauf mit qwen3.8:27b-mlx erfolgreich |
| `read_table/` | Angehängte XLSX/CSV beschreiben und deterministisch auswerten: Summe, Anzahl, Durchschnitt, Min, Max, gruppiert und gefiltert | Installiert, Probelauf mit qwen3.8:27b-mlx erfolgreich |
| `n8n_trigger/` | Freigegebene n8n-Workflows per Webhook auslösen, Ergebnis zurückgeben | Installiert, Probelauf mit qwen3.8:27b-mlx und n8n erfolgreich |
| `ocr_scan/` | Gescannte PDF oder Bild per OCR durchsuchbar machen, Text zurückgeben | Kern und Image getestet, Container-Wechsel steht aus |

## n8n_trigger

### Was es tut

Zwei Funktionen für das Modell:

- `list_workflows`: zeigt die freigegebenen Workflows mit Beschreibung.
- `run_workflow`: POST auf `N8N_BASE_URL/webhook/<name>` mit einem JSON-Objekt als
  Parameter, gibt die Antwort des Workflows zurück.

Sicherheitsregeln, die im Tool fest verdrahtet sind:

- **Deny by default.** Nur Namen aus `ALLOWED_WORKFLOWS` dürfen aufgerufen werden.
  Die Liste pflegt der Admin in den Tool-Einstellungen, das Modell kann sie nicht ändern.
- Workflow-Namen müssen `[a-z0-9_-]` sein, keine Pfade, keine Query-Strings.
- Jeder Aufruf trägt einen Idempotenz-Schlüssel im Header `X-Idempotency-Key` und im Body.
  Wiederholungen (nur bei 502/503/504 oder Verbindungsfehler, Standard 0) nutzen denselben Schlüssel.
- Ein Fehlerstatus wird gemeldet, nicht automatisch erneut ausgelöst.
- An n8n gehen nur Nutzer-ID und Chat-ID, keine E-Mail, kein Name.
- Das Geheimnis liegt in der Valve `AUTH_TOKEN`, nie in der URL. Antworten sind begrenzt.

### Installation

1. Tool importieren, Tool-ID `n8n_workflow`. httpx ist im Container vorhanden.
2. In den Tool-Einstellungen (Zahnrad beim Tool) setzen:
   - `N8N_BASE_URL`: `http://host.docker.internal:5678` (Open WebUI und n8n laufen in
     getrennten Docker-Netzen, die Verbindung geht über den veröffentlichten Port).
   - `AUTH_TOKEN`: ein zufälliger Wert, derselbe wie in der Header-Auth des Webhook-Nodes.
   - `ALLOWED_WORKFLOWS`: eine Zeile je Workflow, z. B. `echo = Gibt Parameter zurück`.
3. Dem Modell zuweisen.

### n8n-Seite

Zum Ausprobieren liegt `n8n_trigger/samples/n8n_echo_workflow.json` bereit: Webhook `POST /webhook/echo`,
Code-Node, Respond to Webhook. In n8n über **Workflows → Import from File** einlesen und **aktivieren**.
Der Beispiel-Workflow hat keine Header-Auth, für produktive Workflows im Webhook-Node
**Authentication → Header Auth** setzen, Header `X-Webhook-Token`, Wert wie `AUTH_TOKEN`.

Der Webhook bekommt diesen Body:

```json
{
  "workflow": "echo",
  "parameters": { "mandant": "M-100" },
  "idempotency_key": "uuid",
  "requested_by": { "user_id": "…", "chat_id": "…" },
  "source": "open-webui"
}
```

Beispielanfrage im Chat: „Welche n8n-Workflows gibt es?“ und dann
„Starte den Workflow echo mit mandant M-100.“

### Einstellungen (Valves)

| Valve | Standard | Bedeutung |
|---|---|---|
| `N8N_BASE_URL` | `http://host.docker.internal:5678` | n8n aus Sicht des Containers |
| `WEBHOOK_PATH_PREFIX` | `webhook` | n8n-Präfix, `webhook-test` bei `TEST_MODE` |
| `AUTH_HEADER_NAME`, `AUTH_TOKEN` | `X-Webhook-Token`, leer | Header-Auth. Leer = kein Header, nur lokal zum Testen |
| `ALLOWED_WORKFLOWS` | leer | Freigabeliste. Leer = nichts erlaubt |
| `TIMEOUT_SECONDS` | 60 | Zeitlimit je Aufruf |
| `MAX_RETRIES` | 0 | Wiederholungen bei 502/503/504 |
| `MAX_PARAMETER_CHARS`, `MAX_RESPONSE_CHARS` | 20000 | Größenbegrenzungen |
| `TEST_MODE` | false | Nutzt den `webhook-test`-Pfad, wenn der Workflow im Editor auf „Listen“ steht |

### Bekannte Grenzen

- Synchron: Der Workflow muss innerhalb des Zeitlimits antworten. Lange Workflows sollten
  sofort eine Zusage zurückgeben und das Ergebnis separat ablegen.
- Der Idempotenz-Schlüssel wird gesendet, aber n8n prüft ihn nicht von selbst. Workflows
  mit Nebenwirkungen müssen ihn selbst auswerten.

## read_table

### Was es tut

Zwei Funktionen für das Modell:

- `describe_spreadsheet`: Blätter, Zeilenzahl, Spalten mit Typ und Kennzahlen
  (Summe, Min, Max bei Zahlen, Werteliste bei Text), dazu eine Markdown-Vorschau.
- `aggregate_spreadsheet`: Summe, Anzahl, Durchschnitt, Minimum oder Maximum einer
  Spalte, optional gruppiert nach einer zweiten Spalte und gefiltert nach einem Wert.

Das Modell rechnet nicht selbst. Alle Zahlen werden als exakte Dezimalzahlen geführt,
deutsche (`1.234,56 €`) und englische (`1,234.56`) Schreibweisen werden erkannt.
Spalten lassen sich per Name, Buchstabe (B) oder Nummer ansprechen.
CSV mit Semikolon, Komma oder Tab, Kodierung UTF-8 oder Windows-1252.

Beispielfragen: „Wie hoch ist die Summe je Konto?“, „Wie viele Buchungen enthalten
Erlöse?“, „Durchschnittlicher Betrag auf Konto 4400?“

### Installation

Siehe „Installation eines Tools“, Tool-ID `tabelle_lesen`. openpyxl ist im Container vorhanden.
Zum Ausprobieren liegt `read_table/samples/beispiel_buchungen.xlsx` bereit, Inhalt erfunden.

### Einstellungen (Valves)

| Valve | Standard | Bedeutung |
|---|---|---|
| `MAX_ROWS` | 5000 | Datenzeilen je Blatt, die geladen werden. Darüber wird abgeschnitten und gemeldet. |
| `PREVIEW_ROWS` | 15 | Zeilen in der Vorschau für das Modell. |
| `MAX_SHEETS` | 10 | Höchstens so viele Blätter beschreiben. |
| `MAX_GROUPS` | 200 | Höchstens so viele Gruppen zurückgeben. |

### Bekannte Grenzen

- Formeln werden als zuletzt gespeicherter Wert gelesen. Nie berechnete Formeln sind leer.
- Die Kopfzeile muss die erste nicht leere Zeile sein. Mehrzeilige Köpfe werden nicht erkannt.
- Gruppierung nach mehreren Spalten gleichzeitig gibt es nicht; dafür zweimal filtern.
- Zellinhalte gehen an das lokale Modell. Für Mandantendaten gilt weiterhin: kein Cloud-Modell wählen.

## Installation eines Tools

Der Code-Editor in Open WebUI hat eingefügten Code beschnitten („No Tools class
found in the module“). Zuverlässig ist der Import einer JSON-Datei:

```bash
python3 scripts/build_import.py create_document/create_document_tool.py --id datei_erzeugen
```

Das Skript schreibt `build/<id>.tools.json`, liest die Datei zur Kontrolle zurück und
legt eine Kopie in `~/Downloads`, weil der Dateidialog dort am einfachsten hinfindet.

1. In Open WebUI als Admin: **Workspace → Tools**, Menü neben dem Plus, **Import JSON**,
   die Datei aus Downloads wählen.
2. **Admin Panel → Settings → Models**, beim Modell auf den Stift, unter **Tools** das
   Werkzeug anhaken, speichern. Basismodelle aus Ollama erscheinen nur dort, nicht
   unter Workspace → Models.
3. Alternativ pro Chat: Plus neben dem Eingabefeld, Tool einschalten.

Ein erneuter Import mit derselben ID überschreibt den gespeicherten Code.
Die Tool-IDs: `pdf_translate` → `dokument_ubersetzen_pdf_docx_pdf_docx` (historisch,
über den Editor angelegt), `create_document` → `datei_erzeugen`.

## create_document

### Was es tut

Das Modell schreibt den Inhalt als Markdown, das Tool rendert daraus die Datei und
legt sie als Download im Chat ab. Unterstützt werden Überschriften, Absätze,
Aufzählungen, nummerierte Listen, Tabellen, Codeblöcke, Trennlinien sowie **fett**,
*kursiv* und `code` im Text.

| Format | Besonderheit |
|---|---|
| docx | Word-Überschriften, Listen mit Word-Formatvorlagen, Tabellen mit Rahmen |
| pdf | DejaVu Sans eingebettet, Tabellen mit Kopfzeile, Codeblöcke in Courier |
| xlsx | Jede Markdown-Tabelle wird ein Blatt, Blattname aus der vorangehenden Überschrift. Reine Zahlen wie `42` oder `99.5` werden Zahlen, deutsche Formate wie `1.234,56 €` bleiben Text. Fällt keine Tabelle an, wird CSV-artiger Text erkannt. |
| csv | Erste Tabelle, Semikolon als Trenner, UTF-8 mit BOM, damit Excel Umlaute richtig liest |
| md, txt | Markdown unverändert, Text ohne Auszeichnungen |

### Installation

Siehe „Installation eines Tools“ oben, Tool-ID `datei_erzeugen`.
Die Abhängigkeiten reportlab, python-docx und openpyxl sind im Container vorhanden.

Beispielanfrage im Chat: „Schreib mir ein Anschreiben an das Finanzamt wegen
Fristverlängerung als Word-Datei.“ oder „Mach aus dieser Liste eine Excel-Tabelle.“

### Einstellungen (Valves)

| Valve | Standard | Bedeutung |
|---|---|---|
| `MAX_CONTENT_CHARS` | 200000 | Obergrenze für den Inhalt. |
| `FONT_PATH`, `BOLD_FONT_PATH` | DejaVu Sans im Container | TTF für PDF. Fehlen sie, Helvetica. |
| `CSV_DELIMITER` | `;` | Trennzeichen für CSV. |

Nutzer-Valve: `DEFAULT_OUTPUT_FORMAT` (docx).

### Tests

```bash
.venv/bin/pip install openpyxl
.venv/bin/python create_document/tests/test_core.py
```

### Bekannte Grenzen

- Verschachtelte Listen werden flach. Bilder werden nicht eingebettet.
- Tabellen in DOCX und PDF haben keine Spaltenausrichtung aus Markdown (`---:`).
- Das Tool erzeugt genau das, was das Modell in `content` schreibt. Lücken im Inhalt
  sind Lücken des Modells, nicht des Tools.

## pdf_translate

### Was es tut

1. Nimmt die im Chat angehängte PDF oder DOCX (zuletzt angehängte, oder per `file_name`).
2. Prüft, dass die Datei dem anfragenden Nutzer gehört.
3. Extrahiert den Text seitenweise und erkennt Absätze auch ohne Leerzeilen.
4. Bündelt Absätze zu Abschnitten (Standard 3000 Zeichen) und übersetzt jeden Abschnitt
   über die interne Chat-Pipeline von Open WebUI mit dem Modell des aktuellen Chats.
   Absätze werden nummeriert übergeben, damit kein Absatz verloren geht.
5. Rendert eine neue PDF (DejaVu Sans, Seitenumbrüche wie in der Quelle) oder DOCX.
6. Legt die Datei als Upload des Nutzers in Open WebUI ab und liefert den Download-Link
   sowohl als Datei-Karte im Chat als auch als Markdown-Link in der Antwort.

Nicht enthalten: Layouterhalt (Spalten, Tabellen, Bilder), OCR für Scans.
Gescannte PDFs ohne Textebene werden mit einer klaren Meldung abgelehnt.

### Installation in Open WebUI

Siehe „Installation eines Tools“ oben. Beim ersten Laden installiert Open WebUI
`reportlab` aus dem Docstring-Frontmatter, dafür muss der Container PyPI erreichen.
Function Calling muss auf **Native** stehen (Standard in 0.11), Qwen 3 unterstützt das.

Probelauf: im Chat eine PDF anhängen und schreiben:
„Übersetze dieses Dokument nach Englisch als PDF.“

### Einstellungen (Valves)

| Valve | Standard | Bedeutung |
|---|---|---|
| `MODEL_ID` | leer | Festes Modell für die Übersetzung. Leer = Modell des Chats. |
| `MAX_CHUNK_CHARS` | 3000 | Zeichen pro Modellanfrage. Bei kleinem Kontextfenster senken. |
| `MAX_PAGES` | 60 | Obergrenze je Dokument. |
| `MAX_RETRIES` | 2 | Wiederholungen pro Abschnitt. |
| `TEMPERATURE` | 0.2 | Übersetzungen sollen reproduzierbar sein. |
| `FONT_PATH` | DejaVu Sans im Container | TTF für die PDF. Fehlt sie, Helvetica (reicht für Deutsch). |
| `GLOSSARY` | leer | Verbindliche Begriffe, eine Zeile `Quelle=Ziel`. |

Nutzer-Valves: `DEFAULT_TARGET_LANGUAGE` (Englisch), `DEFAULT_OUTPUT_FORMAT` (pdf).

### Datenschutz

- Das Modell läuft lokal. Dokumenttext verlässt den Rechner nicht.
- Das Werkzeug protokolliert nur Ereignisnamen und Fehlertypen, nie Inhalte oder Dateinamen.
- Die erzeugte Datei liegt wie jeder Upload im Open-WebUI-Datenverzeichnis und gehört dem Nutzer.
  Open WebUI ist kein Dokumentenspeicher. Ergebnisse, die aufbewahrt werden sollen,
  gehören nach SharePoint; Uploads in Open WebUI sollten regelmäßig gelöscht werden.
- Tests enthalten ausschließlich erfundene Inhalte.

### Tests

```bash
python3 -m venv .venv && .venv/bin/pip install pypdf reportlab python-docx pydantic
.venv/bin/python pdf_translate/tests/test_core.py
```

Die Tests decken Absatzerkennung, Chunking, Marker-Parsing, Reasoning-Bereinigung,
Glossar, Dateinamen, PDF→PDF, PDF→DOCX, DOCX-Quelle, Umlaute ohne TTF,
Fehlerweitergabe und die Seitenobergrenze ab.

### Bekannte Grenzen

- Fließtext-Ausgabe. Tabellen werden zu Absätzen, Bilder entfallen.
- Absatzerkennung ist heuristisch. Bei mehrspaltigen Dokumenten kann die Reihenfolge leiden.
- Qualität der Übersetzung hängt am Modell. Bei Fachbegriffen das Glossar pflegen.

Hinweis: Die Frage „Welche Automatisierungen gibt es?“ landet bei der eingebauten
Automations-Funktion von Open WebUI (geplante Chat-Aufgaben), nicht bei n8n.
Deshalb im Chat von Workflows oder n8n sprechen.

## ocr_scan

### Was es tut

Legt mit `ocrmypdf` (Tesseract, Ghostscript) eine Textebene über eine gescannte PDF oder
ein Bild (PNG, JPG, TIFF) und liefert eine durchsuchbare PDF zum Download sowie den
erkannten Text als Auszug für das Modell. Seiten, die schon Text haben, bleiben
unverändert (`--skip-text`); `force=true` erzwingt eine neue Textebene.
Danach können die anderen Tools weiterarbeiten, etwa `pdf_translate` auf der OCR-PDF.

Das Modell ist an der OCR nicht beteiligt. Das Tool meldet, wenn kaum Text erkannt wurde
(`low_text_warning`) oder die Datei schon durchsuchbar war (`already_had_text`).

### Voraussetzung: Image mit OCR-Werkzeugen

Das offizielle Open-WebUI-Image hat kein Tesseract. `docker/open-webui-ocr/` enthält
ein Dockerfile, das auf dem offiziellen Image aufsetzt und `ocrmypdf` plus die
Sprachpakete Deutsch und Englisch nachinstalliert (rund 200 MB mehr). Die
Python-Umgebung von Open WebUI bleibt unverändert, das Tool ruft nur die Kommandozeile.

```bash
docker/open-webui-ocr/build.sh          # Image open-webui-ocr:local bauen
docker/open-webui-ocr/recreate.sh       # laufenden Container durch das neue Image ersetzen
docker/open-webui-ocr/rollback.sh       # zurück zum alten Container
```

`recreate.sh` übernimmt Datenvolume, Port, `host.docker.internal`, Neustartregel und
alle Umgebungsvariablen (inklusive Secret Key, damit Logins gültig bleiben). Die
Umgebung wird per Prozess-Substitution durchgereicht, nie in eine Datei geschrieben.
Der alte Container wird nicht gelöscht, sondern gestoppt und als `open-webui-old`
behalten. Ausfallzeit: die Startzeit von Open WebUI, etwa eine halbe Minute.

Updates von Open WebUI laufen künftig über `build.sh --pull` und `recreate.sh`
(vorher `open-webui-old` entfernen), nicht mehr über `docker pull` allein.

### Installation

Tool importieren, Tool-ID `ocr_scan`, dem Modell zuweisen. Ohne OCR-Image antwortet
das Tool mit einer klaren Meldung statt eines Absturzes.

Beispielanfrage: Scan anhängen und „Mach diesen Scan durchsuchbar.“ oder
„Was steht in diesem Scan?“

### Einstellungen (Valves)

| Valve | Standard | Bedeutung |
|---|---|---|
| `LANGUAGES` | `deu+eng` | Tesseract-Sprachen. Weitere brauchen ein Sprachpaket im Image. |
| `TIMEOUT_SECONDS` | 600 | Zeitlimit je Datei. |
| `JOBS` | 4 | Parallele Seiten. |
| `MAX_PAGES`, `MAX_INPUT_MB` | 100, 50 | Obergrenzen. |
| `ROTATE_PAGES` | true | Gedrehte Seiten ausrichten. |
| `DESKEW` | false | Schräge Scans begradigen, langsamer. |
| `TEXT_PREVIEW_CHARS` | 12000 | Textauszug für das Modell. |

### Tests

```bash
.venv/bin/python ocr_scan/tests/test_core.py        # Roundtrip nur, wenn ocrmypdf lokal vorhanden
docker run --rm -v "$PWD:/w:ro" -w /w --entrypoint python open-webui-ocr:local ocr_scan/tests/test_core.py
```

### Bekannte Grenzen

- OCR-Qualität hängt an Scanauflösung und Schrift. Handschrift wird nicht erkannt.
- Die OCR-PDF liegt wie jeder Upload in Open WebUI. Produktive Dokumente gehören nach SharePoint.
- Zeitbedarf grob: wenige Sekunden je Seite auf dem Mac Studio.
