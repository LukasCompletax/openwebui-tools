#!/usr/bin/env python3
"""Prüflauf: Wie zuverlässig wählt das lokale Modell das richtige Tool?

Schickt die Fälle aus cases.json direkt an Ollama (/api/chat) mit genau den
Werkzeugdefinitionen, die Open WebUI aus den installierten Tools erzeugt, und
vergleicht den ersten Werkzeugaufruf mit der Erwartung. Kein Tool wird dabei
ausgeführt, es geht nur um die Wahl.

Angehängte Dateien werden nachgebildet: Open WebUI stellt dem Modell den
extrahierten Text einer Datei als Quelle im Kontext bereit. Hier steht dafür ein
<source>-Block mit Dateiname und einem kurzen synthetischen Auszug im Prompt; bei
Scans ist der Auszug leer, so wie es bei einer PDF ohne Textebene wäre.

Zweistufig: Erlaubt ein Fall einen vorbereitenden Aufruf (accepted_first, z. B.
describe_spreadsheet vor aggregate_spreadsheet), bekommt das Modell ein
vorgefertigtes Ergebnis dieses Aufrufs zurück und wird erneut gefragt. Gewertet
wird dann der zweite Aufruf.

Aufruf:
    python3 eval/tool_choice/run_eval.py                      # Specs live aus dem Container
    python3 eval/tool_choice/run_eval.py --specs specs.json   # Specs aus Datei
    python3 eval/tool_choice/run_eval.py --model qwen3.6:latest --runs 3 --temperature 0.7
"""

from __future__ import annotations

import argparse
import re
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).parent
EXPORT_SNIPPET = r"""
import sqlite3, json
c = sqlite3.connect("/app/backend/data/webui.db")
out = []
for tid, specs in c.execute("select id, specs from tool"):
    for spec in json.loads(specs):
        out.append({"tool_id": tid, "type": "function", "function": {
            "name": spec["name"], "description": spec.get("description", ""),
            "parameters": spec.get("parameters", {"type": "object", "properties": {}})}})
print(json.dumps(out, ensure_ascii=False))
"""


CANNED_RESULTS = {
    "describe_spreadsheet": json.dumps({"status": "success", "file": "tabelle", "sheets": [{"sheet": "Buchungen", "rows": 6,
        "columns": [{"name": "Datum", "type": "text"}, {"name": "Konto", "type": "text", "values": ["4400", "6300", "6800"]},
                    {"name": "Buchungstext", "type": "text"}, {"name": "Betrag", "type": "number", "sum": "2834.60"}],
        "preview": "| Datum | Konto | Buchungstext | Betrag |"}],
        "instruction_for_assistant": "Für Summen, Gruppierungen oder Filter aggregate_spreadsheet aufrufen, nicht selbst rechnen."}, ensure_ascii=False),
    "list_workflows": json.dumps({"status": "success", "workflows": [{"name": "echo", "description": "Gibt Parameter zurück"}], "count": 1}, ensure_ascii=False),
}


def build_prompt(case: dict) -> str:
    prompt = case["prompt"]
    if case.get("attachment"):
        text = case.get("attachment_text")
        excerpt = text if text else "(kein Text extrahierbar)"
        prompt = f'<source name="{case["attachment"]}">\n{excerpt}\n</source>\n\n{prompt}'
    return prompt


def export_specs(container: str) -> list[dict]:
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-"],
        input=EXPORT_SNIPPET, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def to_ollama_tools(specs: list[dict]) -> list[dict]:
    tools = []
    for spec in specs:
        fn = spec["function"]
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        # Open WebUI schreibt bei manchen Parametern "str" statt "string"
        for prop in params.get("properties", {}).values():
            if prop.get("type") == "str":
                prop["type"] = "string"
        tools.append({"type": "function", "function": {"name": fn["name"], "description": fn["description"], "parameters": params}})
    return tools


def ask(base_url: str, model: str, messages: list[dict], tools: list[dict], temperature: float, timeout: int) -> tuple[list[str], float, dict]:
    body = {
        "model": model, "stream": False, "think": False,
        "options": {"temperature": temperature},
        "messages": messages,
        "tools": tools,
    }
    req = urllib.request.Request(f"{base_url}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.load(response)
    seconds = time.monotonic() - started
    message = payload.get("message", {})
    calls = [c["function"]["name"] for c in (message.get("tool_calls") or [])]
    return calls, seconds, message


def first_args(message: dict) -> dict:
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return {}
    args = tool_calls[0].get("function", {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    return args


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def check_args(expected_args: dict | None, args: dict | None) -> list[str]:
    """Vergleicht übergebene Parameter mit Erwartungen. Gibt Abweichungen zurück, leer = alles ok.

    Matcher: {"in": [...]} exakt aus Liste, {"contains": "x"} Teilstring, {"contains_all": [...]},
    {"regex": "..."}; alles ohne Groß/Klein.
    """
    if not expected_args:
        return []
    args = args or {}
    problems: list[str] = []
    for name, matcher in expected_args.items():
        got = _as_text(args.get(name)).strip().lower()
        if "in" in matcher:
            if got not in [str(v).lower() for v in matcher["in"]]:
                problems.append(f"{name}={got!r} nicht in {matcher['in']}")
        if "contains" in matcher and matcher["contains"].lower() not in got:
            problems.append(f"{name}={got[:40]!r} enthält nicht {matcher['contains']!r}")
        if "contains_all" in matcher:
            missing = [w for w in matcher["contains_all"] if w.lower() not in got]
            if missing:
                problems.append(f"{name} fehlt {missing}")
        if "regex" in matcher and not re.search(matcher["regex"], got, re.IGNORECASE | re.DOTALL):
            problems.append(f"{name} passt nicht auf /{matcher['regex']}/")
    return problems


def classify(expected: str | None, calls: list[str]) -> str:
    first = calls[0] if calls else None
    if expected is None and first is None:
        return "ok"
    if expected is None and first is not None:
        return "tool_unnoetig"
    if first is None:
        return "tool_fehlt"
    if first == expected:
        return "ok"
    return "tool_falsch"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="qwen3.8:27b-mlx")
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--container", default="open-webui")
    parser.add_argument("--specs", type=pathlib.Path, help="Specs aus Datei statt aus dem Container")
    parser.add_argument("--cases", type=pathlib.Path, default=HERE / "cases.json")
    parser.add_argument("--runs", type=int, default=1, help="Wiederholungen je Fall")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out", type=pathlib.Path, default=HERE / "results")
    args = parser.parse_args(argv)

    specs = json.loads(args.specs.read_text(encoding="utf-8")) if args.specs else export_specs(args.container)
    tools = to_ollama_tools(specs)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    print(f"model={args.model} tools={len(tools)} cases={len(cases)} runs={args.runs} temperature={args.temperature}")

    rows = []
    for case in cases:
        prompt = build_prompt(case)
        accepted_first = set(case.get("accepted_first") or [])
        for run in range(1, args.runs + 1):
            messages = [{"role": "user", "content": prompt}]
            steps: list[str] = []
            seconds_total = 0.0
            error = ""
            try:
                calls, seconds, message = ask(args.ollama, args.model, messages, tools, args.temperature, args.timeout)
                seconds_total += seconds
                steps.append(calls[0] if calls else "-")
                if calls and calls[0] in accepted_first and calls[0] in CANNED_RESULTS:
                    # Zweiter Schritt: Ergebnis des Vorbereitungs-Aufrufs zurückgeben
                    messages.append(message)
                    messages.append({"role": "tool", "content": CANNED_RESULTS[calls[0]], "tool_name": calls[0]})
                    calls, seconds, message = ask(args.ollama, args.model, messages, tools, args.temperature, args.timeout)
                    seconds_total += seconds
                    steps.append(calls[0] if calls else "-")
            except Exception as exc:  # Netz oder Modell
                calls, error = [], type(exc).__name__
            verdict = "fehler" if error else classify(case["expected"], calls)
            call_args = first_args(message) if not error else {}
            arg_problems = check_args(case.get("expected_args"), call_args) if verdict == "ok" and case["expected"] else []
            arg_verdict = "" if not case.get("expected_args") else ("ok" if verdict == "ok" and not arg_problems else "abweichung")
            rows.append({"id": case["id"], "run": run, "expected": case["expected"], "got": calls[0] if calls else None,
                         "steps": steps, "verdict": verdict, "args": call_args, "arg_verdict": arg_verdict, "arg_problems": arg_problems,
                         "seconds": round(seconds_total, 1), "error": error})
            mark = {"ok": "✓", "tool_falsch": "✗", "tool_fehlt": "–", "tool_unnoetig": "+", "fehler": "!"}[verdict]
            extra = f"  parameter: {'ok' if not arg_problems else '; '.join(arg_problems)}" if case.get("expected_args") and verdict == "ok" else ""
            print(f"  {mark} {case['id']} run{run}: erwartet={case['expected']} schritte={' → '.join(steps) or '-'} ({seconds_total:.1f}s){extra}")

    total = len(rows)
    counts = {k: sum(1 for r in rows if r["verdict"] == k) for k in ("ok", "tool_falsch", "tool_fehlt", "tool_unnoetig", "fehler")}
    accuracy = counts["ok"] / total if total else 0.0
    per_case: dict[str, list[str]] = {}
    for r in rows:
        per_case.setdefault(r["id"], []).append(r["verdict"])
    unstable = sorted(cid for cid, vs in per_case.items() if len(set(vs)) > 1)
    avg_seconds = sum(r["seconds"] for r in rows) / total if total else 0.0
    arg_rows = [r for r in rows if r["arg_verdict"]]
    arg_ok = sum(1 for r in arg_rows if r["arg_verdict"] == "ok")

    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{stamp}_{args.model.replace(':', '_')}.json").write_text(
        json.dumps({"model": args.model, "temperature": args.temperature, "runs": args.runs, "tools": [t["function"]["name"] for t in tools],
                    "summary": {**counts, "accuracy": round(accuracy, 3), "avg_seconds": round(avg_seconds, 1), "unstable_cases": unstable,
                                "args_checked": len(arg_rows), "args_ok": arg_ok},
                    "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# Tool-Wahl: {args.model}", "",
             f"Stand {stamp}, Temperatur {args.temperature}, {args.runs} Lauf/Läufe je Fall, {len(tools)} Werkzeuge, {len(cases)} Fälle.", "",
             "| Ergebnis | Anzahl |", "|---|---|",
             f"| richtig | {counts['ok']} von {total} ({accuracy:.0%}) |",
             f"| falsches Tool | {counts['tool_falsch']} |",
             f"| Tool fehlt | {counts['tool_fehlt']} |",
             f"| Tool unnötig | {counts['tool_unnoetig']} |",
             f"| Fehler | {counts['fehler']} |",
             f"| Ø Antwortzeit | {avg_seconds:.1f} s |",
             f"| Parameter korrekt | {arg_ok} von {len(arg_rows)} geprüften Aufrufen |", ""]
    if unstable:
        lines += [f"Instabil über die Läufe: {', '.join(unstable)}", ""]
    lines += ["| Fall | erwartet | bekommen | Urteil | Parameter |", "|---|---|---|---|---|"]
    for r in rows:
        params = "–" if not r["arg_verdict"] else ("ok" if r["arg_verdict"] == "ok" else "; ".join(r["arg_problems"]) or "nicht geprüft")
        lines.append(f"| {r['id']} (Lauf {r['run']}) | {r['expected'] or '–'} | {' → '.join(r['steps']) or '–'} | {r['verdict']} | {params} |")
    report = args.out / f"{stamp}_{args.model.replace(':', '_')}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nrichtig: {counts['ok']}/{total} ({accuracy:.0%})  falsch: {counts['tool_falsch']}  fehlt: {counts['tool_fehlt']}  unnötig: {counts['tool_unnoetig']}  Ø {avg_seconds:.1f}s  parameter: {arg_ok}/{len(arg_rows)}")
    print(f"Bericht: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
