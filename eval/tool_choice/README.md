# Prüflauf: Tool-Wahl des lokalen Modells

Frage: Wählt qwen3.8:27b-mlx bei typischen Kanzlei-Anfragen zuverlässig das richtige
Tool, und lässt es die Finger von Tools, wenn keines nötig ist? Das Ergebnis entscheidet,
ob Skills als Prompt-Bausteine mit diesem Modell tragen oder ein größeres Modell nötig ist.

## Aufbau

- `cases.json`: 22 Fälle, alle Inhalte erfunden. 17 Fälle erwarten ein bestimmtes Tool,
  5 Fälle erwarten ausdrücklich keines (Fachfrage, Umformulierung, Frist, Zusammenfassung
  und Zahl aus einem Dokument, dessen Text schon im Kontext liegt).
- `run_eval.py`: holt die Werkzeugdefinitionen live aus Open WebUI (dieselben Specs, die
  Open WebUI dem Modell bei nativem Function Calling übergibt) und schickt jeden Fall direkt
  an Ollama. Kein Tool wird ausgeführt, nur die Wahl wird gewertet.
- Dateikontext wird nachgebildet: ein `<source>`-Block mit Dateiname und kurzem Auszug,
  bei Scans leer, so wie Open WebUI den extrahierten Text bereitstellt.
- Zweistufig: Fälle, bei denen die Tool-Beschreibung einen Vorbereitungs-Aufruf verlangt
  (`describe_spreadsheet` vor `aggregate_spreadsheet`, `list_workflows` vor `run_workflow`),
  bekommen ein vorgefertigtes Ergebnis zurück; gewertet wird der zweite Aufruf.

```bash
python3 eval/tool_choice/run_eval.py --runs 1 --temperature 0
python3 eval/tool_choice/run_eval.py --runs 3 --temperature 0.7
python3 eval/tool_choice/run_eval.py --model qwen3.6:latest
```

Berichte landen unter `results/` als Markdown und JSON.

## Ergebnis vom 14.10.2026, qwen3.8:27b-mlx, 7 Werkzeuge

| Lauf | richtig | falsches Tool | Tool fehlt | Tool unnötig | Ø Antwortzeit |
|---|---|---|---|---|---|
| Temperatur 0, 1 Lauf je Fall | 22 von 22 | 0 | 0 | 0 | 4,4 s |
| Temperatur 0,7, 3 Läufe je Fall | 65 von 66 | 0 | 1 | 0 | 4,3 s |

Der einzige Ausreißer: Beim Anschreiben an das Finanzamt (c01, dritter Lauf) hat das Modell
den Brief direkt in den Chat geschrieben statt das Datei-Tool zu rufen. Inhaltlich richtig,
nur ohne Download. In den anderen fünf Läufen desselben Falls kam das Tool.

Die fünf Fälle ohne Tool wurden in allen Läufen ohne Werkzeug beantwortet, auch die beiden
mit angehängtem Dokument. Das Modell ruft also nicht reflexhaft OCR oder Übersetzung, wenn
der Text schon im Kontext liegt.

Die zweistufigen Ketten (erst beschreiben, dann rechnen; erst auflisten, dann starten)
funktionierten in allen Läufen.

## Einordnung

Für Skills als Prompt-Bausteine reicht dieses Modell. Die Tool-Beschreibungen sind der
wirksame Hebel: Die Formulierung „Nutze dieses Tool immer, wenn …“ und die Abgrenzung
zu Open WebUIs eigener Automations-Funktion haben die Wahl spürbar stabilisiert. Ein
größeres Modell ist für die Tool-Wahl nicht nötig. Wo es fehlen könnte, ist die Qualität
der Übersetzung und Zusammenfassung selbst, das misst dieser Prüflauf nicht.

Was dieser Prüflauf nicht abbildet:

- Die Reihenfolge und Menge an Kontext, die Open WebUI tatsächlich einfügt (Systemprompt,
  RAG-Auszüge mit mehreren Quellen, Chatverlauf). Lange Verläufe mit vielen Werkzeugaufrufen
  sind die bekannte Schwäche kleiner Modelle und sollten gesondert geprüft werden.
- Die Qualität der Parameter. Gewertet wird nur, welches Tool gerufen wird, nicht ob
  Zielsprache, Spaltenname oder Dateiname korrekt übergeben werden.
- Tool-Ergebnisse sind vorgefertigt. Ob das Modell mit echten, längeren Ergebnissen weiter
  richtig entscheidet, zeigt nur der Chat.

Bei jeder Änderung an Tool-Beschreibungen oder beim Hinzufügen eines Tools den Lauf
wiederholen. Ein neues Tool kann die Wahl für alle anderen verschieben.
