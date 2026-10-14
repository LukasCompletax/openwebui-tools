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
| `create_document/` | Aus Markdown-Text des Modells eine Datei erzeugen: DOCX, PDF, XLSX, MD, TXT, CSV | Kern getestet, Installation steht aus |

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

Wie bei `pdf_translate`: Inhalt von `create_document/create_document_tool.py` unter
Workspace → Tools einfügen, speichern, im Admin-Bereich dem Modell zuweisen.
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

1. Open WebUI als Admin öffnen, **Workspace → Tools → +**.
2. Inhalt von `pdf_translate/pdf_translate_tool.py` einfügen, speichern.
   Beim ersten Laden installiert Open WebUI `reportlab` aus dem Docstring-Frontmatter.
   Das setzt voraus, dass der Container PyPI erreichen darf.
3. **Workspace → Models → Modell bearbeiten → Tools**: das Werkzeug für das Qwen-Modell aktivieren.
   Alternativ im Chat über das Werkzeug-Symbol pro Unterhaltung zuschalten.
4. Function Calling muss auf **Native** stehen (Standard in 0.11). Qwen 3 unterstützt das.
5. Im Chat eine PDF anhängen und schreiben: „Übersetze dieses Dokument nach Englisch als PDF.“

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
