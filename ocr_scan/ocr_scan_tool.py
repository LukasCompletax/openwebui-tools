"""
title: Scan mit OCR lesbar machen (PDF/Bild → durchsuchbare PDF)
author: completax
version: 0.1.0
description: Legt mit ocrmypdf und Tesseract eine Textebene über gescannte PDFs oder Bilder und liefert eine durchsuchbare PDF zum Download plus den erkannten Text. Braucht ocrmypdf im Container (Image open-webui-ocr).
requirements: pypdf
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SUPPORTED_INPUT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
_LANG = re.compile(r"^[a-z]{3}(\+[a-z]{3})*$")

# ---------------------------------------------------------------------------
# Kern ohne Open-WebUI-Abhängigkeit
# ---------------------------------------------------------------------------


def ocr_available() -> bool:
    return shutil.which("ocrmypdf") is not None


def validate_languages(languages: str) -> str:
    cleaned = (languages or "deu+eng").strip().lower().replace(" ", "")
    if not _LANG.match(cleaned):
        raise ValueError("Sprachen als Tesseract-Codes angeben, z. B. deu oder deu+eng.")
    return cleaned


def build_command(
    input_path: str,
    output_path: str,
    languages: str,
    jobs: int,
    rotate_pages: bool,
    deskew: bool,
    force: bool,
) -> list[str]:
    """ocrmypdf-Aufruf zusammenstellen. --skip-text lässt Seiten mit Textebene unangetastet."""
    cmd = [
        "ocrmypdf",
        "--language", validate_languages(languages),
        "--output-type", "pdf",
        "--optimize", "0",
        "--oversample", "300",  # Bilder unter 300 dpi vor der Erkennung hochrechnen, hilft bei groben Scans
        "--jobs", str(max(1, min(int(jobs), 8))),
        "--quiet",
    ]
    cmd.append("--force-ocr" if force else "--skip-text")
    if rotate_pages:
        cmd.append("--rotate-pages")
    if deskew:
        cmd.append("--deskew")
    if not input_path.lower().endswith(".pdf"):
        cmd += ["--image-dpi", "300"]
    cmd += [input_path, output_path]
    return cmd


async def run_ocr(cmd: list[str], timeout_seconds: float) -> tuple[int, str]:
    """ocrmypdf ausführen, Rückgabe (Exit-Code, letzte stderr-Zeilen ohne Pfade)."""
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "Zeitlimit überschritten"
    text = (stderr or b"").decode("utf-8", "replace")
    lines = [re.sub(r"/\S+", "<pfad>", line) for line in text.strip().splitlines() if line.strip()]
    return process.returncode or 0, " | ".join(lines[-3:])


EXIT_MESSAGES = {
    0: "",
    1: "Ungültige Parameter.",
    2: "Eingabedatei ist keine lesbare PDF oder kein Bild.",
    3: "Ein benötigtes Programm fehlt (Tesseract, Ghostscript).",
    4: "Ausgabe konnte nicht geschrieben werden.",
    5: "Unerwarteter Fehler in ocrmypdf.",
    6: "Die Datei hat bereits eine Textebene auf allen Seiten.",
    7: "Ein Kindprozess ist abgestürzt.",
    8: "PDF ist verschlüsselt und kann nicht verarbeitet werden.",
    9: "Ungültige Konfiguration.",
    10: "Die PDF ist beschädigt oder entspricht nicht dem Standard.",
    15: "Ausgabedatei existiert bereits.",
    124: "Zeitlimit überschritten.",
    130: "Abgebrochen.",
}


def extract_text(pdf_path: str, max_chars: int) -> tuple[str, int, int]:
    """Text der OCR-PDF für das Modell: (Auszug, Seitenzahl, Gesamtzeichen)."""
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    pages: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        total += len(text)
        if text:
            pages.append(f"--- Seite {index} ---\n{text}")
    joined = "\n\n".join(pages)
    return joined[:max_chars], len(reader.pages), total


def output_name(source_name: str) -> str:
    base = os.path.splitext(os.path.basename(source_name))[0] or "scan"
    base = re.sub(r"[^\w\-. ]+", "_", base)[:80]
    return f"{base}.ocr.pdf"


# ---------------------------------------------------------------------------
# Open-WebUI-Tool
# ---------------------------------------------------------------------------


class Tools:
    class Valves(BaseModel):
        LANGUAGES: str = Field(default="deu+eng", description="Tesseract-Sprachen, z. B. deu+eng.")
        TIMEOUT_SECONDS: int = Field(default=600, description="Zeitlimit für die OCR je Datei.")
        JOBS: int = Field(default=4, description="Parallele Seiten für ocrmypdf.")
        MAX_PAGES: int = Field(default=100, description="Obergrenze für Seiten je Dokument.")
        MAX_INPUT_MB: int = Field(default=50, description="Obergrenze für die Eingabedatei.")
        ROTATE_PAGES: bool = Field(default=True, description="Gedrehte Seiten automatisch ausrichten.")
        DESKEW: bool = Field(default=False, description="Schräg eingescannte Seiten begradigen (langsamer).")
        TEXT_PREVIEW_CHARS: int = Field(default=12_000, description="So viel erkannten Text bekommt das Modell zurück.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def ocr_document(
        self,
        file_name: str = "",
        force: bool = False,
        __user__: Optional[dict] = None,
        __files__: Optional[list] = None,
        __event_emitter__: Any = None,
    ) -> str:
        """
        Macht einen angehängten Scan lesbar: Legt per OCR eine Textebene über eine gescannte PDF oder ein Bild (PNG, JPG, TIFF) und liefert eine durchsuchbare PDF zum Download sowie den erkannten Text. Nutze dieses Tool, wenn ein angehängtes Dokument ein Scan ist, kein Text extrahiert werden konnte oder der Nutzer OCR verlangt. Danach kann die OCR-PDF übersetzt oder zusammengefasst werden.

        :param file_name: Name der angehängten Datei, falls mehrere angehängt sind. Leer = zuletzt angehängte PDF oder Bild.
        :param force: True erzwingt OCR auch auf Seiten, die schon Text haben (ersetzt deren Textebene). Standard False.
        :return: JSON mit Download-Link der OCR-PDF, Seitenzahl, Zeichen und einem Textauszug.
        """
        started = time.monotonic()
        if not __user__ or not __user__.get("id"):
            return self._fail("Kein Nutzerkontext vorhanden. Abgebrochen.")
        if not ocr_available():
            return self._fail(
                "ocrmypdf ist in diesem Container nicht installiert. Open WebUI muss mit dem Image "
                "open-webui-ocr laufen (siehe docker/open-webui-ocr im Repository openwebui-tools)."
            )
        try:
            source = await self._resolve_source_file(__files__ or [], file_name, __user__["id"])
        except LookupError as exc:
            return self._fail(str(exc))

        size_mb = os.path.getsize(source["path"]) / (1024 * 1024)
        if size_mb > self.valves.MAX_INPUT_MB:
            return self._fail(f"Datei zu groß ({size_mb:.1f} MB, erlaubt {self.valves.MAX_INPUT_MB} MB).")
        source_chars = 0
        if source["path"].lower().endswith(".pdf"):
            try:
                _, page_count, source_chars = await asyncio.to_thread(extract_text, source["path"], 0)
            except Exception:
                page_count = None
            if page_count and page_count > self.valves.MAX_PAGES:
                return self._fail(f"Das Dokument hat {page_count} Seiten, erlaubt sind höchstens {self.valves.MAX_PAGES}.")

        await self._status(__event_emitter__, f"OCR läuft für {source['name']} …")
        out_name = output_name(source["name"])
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, out_name)
            try:
                cmd = build_command(
                    source["path"], out_path, self.valves.LANGUAGES, self.valves.JOBS,
                    self.valves.ROTATE_PAGES, self.valves.DESKEW, force,
                )
            except ValueError as exc:
                return self._fail(str(exc))
            code, detail = await run_ocr(cmd, float(self.valves.TIMEOUT_SECONDS))
            if code == 6 and not force:
                return self._fail(
                    "Alle Seiten haben bereits eine Textebene, OCR ist nicht nötig. "
                    "Mit force=true kann die Textebene trotzdem neu erzeugt werden."
                )
            if code != 0 or not os.path.exists(out_path):
                log.warning("ocr_scan: ocrmypdf exit=%s", code)
                reason = EXIT_MESSAGES.get(code, f"ocrmypdf endete mit Code {code}.")
                return self._fail(f"OCR fehlgeschlagen: {reason}" + (f" ({detail})" if detail else ""))

            try:
                preview, pages, total_chars = await asyncio.to_thread(
                    extract_text, out_path, self.valves.TEXT_PREVIEW_CHARS
                )
            except Exception:
                preview, pages, total_chars = "", 0, 0

            with open(out_path, "rb") as fh:
                payload = fh.read()

        try:
            record = await self._store(payload, out_name, __user__["id"], source["name"])
        except Exception as exc:
            log.warning("ocr_scan: store failed (%s)", type(exc).__name__)
            return self._fail(f"Ergebnis konnte nicht gespeichert werden: {type(exc).__name__}")

        url = f"/api/v1/files/{record['id']}/content"
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "files",
                    "data": {
                        "files": [
                            {
                                "type": "file",
                                "id": record["id"],
                                "url": url,
                                "name": out_name,
                                "size": record["size"],
                                "content_type": "application/pdf",
                            }
                        ]
                    },
                }
            )
        seconds = round(time.monotonic() - started, 1)
        log.info("ocr_scan: pages=%s chars=%s seconds=%s", pages, total_chars, seconds)
        await self._status(__event_emitter__, "OCR fertig.", done=True)
        return json.dumps(
            {
                "status": "success",
                "message": f"OCR abgeschlossen. Download: [{out_name}]({url})",
                "download_url": url,
                "file_name": out_name,
                "pages": pages,
                "characters": total_chars,
                "seconds": seconds,
                "text_preview": preview,
                "text_truncated": total_chars > len(preview),
                "low_text_warning": total_chars < 50 * max(pages, 1),
                "already_had_text": bool(source_chars) and total_chars <= source_chars and not force,
                "instruction_for_assistant": (
                    "Download-Link als Markdown-Link zeigen. Den erkannten Text nur auf Nachfrage wiedergeben. "
                    "OCR-Text kann Erkennungsfehler enthalten, besonders bei Zahlen, Beträgen und Namen: "
                    "Beträge nie als gesichert darstellen, sondern auf Prüfung am Original hinweisen. "
                    "Bei low_text_warning darauf hinweisen, dass die Scanqualität schlecht sein könnte. "
                    "Bei already_had_text sagen, dass die Datei bereits durchsuchbar war und keine neue Textebene nötig war."
                ),
            },
            ensure_ascii=False,
        )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _fail(message: str) -> str:
        return json.dumps({"status": "error", "message": message}, ensure_ascii=False)

    @staticmethod
    async def _status(emitter: Any, description: str, done: bool = False) -> None:
        if not emitter:
            return
        try:
            await emitter({"type": "status", "data": {"description": description, "done": done}})
        except Exception:
            pass

    async def _resolve_source_file(self, files: list, wanted_name: str, user_id: str) -> dict:
        from open_webui.models.files import Files
        from open_webui.storage.provider import Storage

        candidates = []
        for item in files:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            name = item.get("name") or (item.get("file") or {}).get("filename") or ""
            if os.path.splitext(name)[1].lower() in SUPPORTED_INPUT and item.get("id"):
                candidates.append({"id": item["id"], "name": name})
        if not candidates:
            raise LookupError("Keine PDF oder kein Bild (PNG, JPG, TIFF) im Chat angehängt.")
        if wanted_name:
            wanted = wanted_name.strip().lower()
            matches = [c for c in candidates if c["name"].lower() == wanted] or [
                c for c in candidates if wanted in c["name"].lower()
            ]
            if not matches:
                raise LookupError(f"Datei '{wanted_name}' nicht gefunden. Angehängt: {', '.join(c['name'] for c in candidates)}")
            chosen = matches[-1]
        else:
            chosen = candidates[-1]
        record = await Files.get_file_by_id(chosen["id"])
        if record is None or record.user_id != user_id:
            raise LookupError("Zugriff auf die angehängte Datei verweigert.")
        if not record.path:
            raise LookupError("Die Datei hat keinen Speicherpfad. Bitte erneut hochladen.")
        local_path = await asyncio.to_thread(Storage.get_file, record.path)
        if not local_path or not os.path.exists(local_path):
            raise LookupError("Die Datei ist auf dem Server nicht mehr vorhanden.")
        return {"id": record.id, "name": chosen["name"] or record.filename, "path": local_path}

    async def _store(self, payload: bytes, out_name: str, user_id: str, source_name: str) -> dict:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        file_id = str(uuid.uuid4())
        tags = {"OpenWebUI-User-Id": user_id, "OpenWebUI-File-Id": file_id}
        contents, stored_path = await asyncio.to_thread(
            Storage.upload_file, io.BytesIO(payload), f"{file_id}_{out_name}", tags
        )
        record = await Files.insert_new_file(
            user_id,
            FileForm(
                id=file_id,
                filename=out_name,
                path=stored_path,
                data={},
                meta={
                    "name": out_name,
                    "content_type": "application/pdf",
                    "size": len(contents),
                    "data": {"generated_by": "ocr_scan", "source_name": source_name},
                },
            ),
        )
        if record is None:
            raise RuntimeError("Dateieintrag konnte nicht angelegt werden.")
        return {"id": file_id, "size": len(contents)}
