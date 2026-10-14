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
- Parameter: 12 Fälle tragen `expected_args` mit Matchern (`in`, `contains`, `contains_all`,
  `regex`, alle ohne Groß/Klein). Geprüft werden Zielsprache und Format, Spaltennamen,
  Operation und Filterwerte, Workflow-Name und Parameter-JSON sowie das Fehlen von `force`.

```bash
python3 eval/tool_choice/run_eval.py --runs 1 --temperature 0
python3 eval/tool_choice/run_eval.py --runs 3 --temperature 0.7
python3 eval/tool_choice/run_eval.py --model qwen3.6:latest
```

Berichte landen unter `results/` als Markdown und JSON.

## Ergebnis vom 14.10.2026, qwen3.8:27b-mlx, 7 Werkzeuge

| Lauf | Tool richtig | falsches Tool | Tool fehlt | Tool unnötig | Parameter korrekt | Ø Antwortzeit |
|---|---|---|---|---|---|---|
| Temperatur 0, 1 Lauf je Fall | 22 von 22 | 0 | 0 | 0 | 12 von 12 | 5,3 s |
| Temperatur 0,7, 3 Läufe je Fall | 65 von 66 | 0 | 1 | 0 | 36 von 36 | 4,5 s |

Ausreißer über alle Läufe des Tages: je einmal hat das Modell statt eines Tool-Aufrufs direkt
geantwortet (einmal das Anschreiben in den Chat geschrieben, einmal die Frage nach den
n8n-Workflows ohne Liste beantwortet). Nie wurde ein falsches Tool gewählt, nie ein Tool
unnötig gerufen. Alle geprüften Parameter waren korrekt: Zielsprachen, Formate, Spalten,
Operationen, Filterwerte, Workflow-Name, Parameter-JSON.

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
- Parameter werden nur bei 12 Fällen und nur auf die genannten Felder geprüft. Der Inhalt
  erzeugter Dokumente wird nicht bewertet, nur dass Pflichtbegriffe darin vorkommen.
- Tool-Ergebnisse sind vorgefertigt. Ob das Modell mit echten, längeren Ergebnissen weiter
  richtig entscheidet, zeigt nur der Chat.

Bei jeder Änderung an Tool-Beschreibungen oder beim Hinzufügen eines Tools den Lauf
wiederholen. Ein neues Tool kann die Wahl für alle anderen verschieben.
