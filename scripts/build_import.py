#!/usr/bin/env python3
"""Baut aus einer Tool-Datei die JSON-Datei für „Import JSON“ in Open WebUI.

Der Editor in Open WebUI hat eingefügten Code beschnitten, der Import über eine
JSON-Datei kommt dagegen vollständig an. Deshalb ist der Import der Standardweg.

Aufruf:
    python3 scripts/build_import.py pdf_translate/pdf_translate_tool.py
    python3 scripts/build_import.py create_document/create_document_tool.py --id datei_erzeugen

Die Ausgabe landet unter build/<name>.tools.json und zusätzlich in ~/Downloads,
weil der Dateidialog von Open WebUI dort am einfachsten hinfindet.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def read_frontmatter(source: str) -> dict[str, str]:
    """Kopfzeilen aus dem Docstring am Dateianfang (title, description, requirements ...)."""
    parts = source.split('"""')
    if len(parts) < 3:
        raise SystemExit("Kein Docstring mit Frontmatter am Dateianfang gefunden.")
    frontmatter: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            frontmatter[key.strip()] = value.strip()
    if "title" not in frontmatter:
        raise SystemExit("Frontmatter braucht mindestens 'title:'.")
    return frontmatter


def default_id(path: Path) -> str:
    stem = path.stem.removesuffix("_tool")
    return re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")


def build(path: Path, tool_id: str | None, out_dir: Path, copy_to_downloads: bool) -> Path:
    source = path.read_text(encoding="utf-8")
    if "class Tools" not in source:
        raise SystemExit(f"{path}: keine Klasse 'Tools' gefunden.")
    frontmatter = read_frontmatter(source)
    tool_id = tool_id or default_id(path)
    payload = [
        {
            "id": tool_id,
            "name": frontmatter["title"],
            "meta": {"description": frontmatter.get("description", ""), "manifest": frontmatter},
            "content": source,
        }
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{tool_id}.tools.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Rücklesen und vergleichen, damit nie eine beschnittene Datei importiert wird.
    back = json.loads(out_path.read_text(encoding="utf-8"))[0]["content"]
    if back != source:
        raise SystemExit("Rücklesen ergab abweichenden Inhalt. Abgebrochen.")

    if copy_to_downloads:
        downloads = Path.home() / "Downloads"
        if downloads.is_dir():
            (downloads / out_path.name).write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
    return out_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool_file", type=Path, help="Pfad zur Tool-Datei (.py)")
    parser.add_argument("--id", dest="tool_id", help="Tool-ID in Open WebUI (Standard: Dateiname ohne _tool)")
    parser.add_argument("--out", type=Path, default=Path("build"), help="Ausgabeordner (Standard: build/)")
    parser.add_argument("--no-downloads", action="store_true", help="Keine Kopie nach ~/Downloads")
    args = parser.parse_args(argv)

    out_path = build(args.tool_file, args.tool_id, args.out, not args.no_downloads)
    print(f"geschrieben: {out_path}" + ("" if args.no_downloads else f"  (Kopie in ~/Downloads/{out_path.name})"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
