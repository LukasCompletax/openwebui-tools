"""
title: Dokument übersetzen (PDF/DOCX → PDF/DOCX)
author: completax
version: 0.1.0
description: Übersetzt ein im Chat angehängtes PDF- oder DOCX-Dokument in eine Zielsprache und liefert das Ergebnis als neue PDF- oder DOCX-Datei zum Download. Das Modell übersetzt nur Text, Extraktion und Rendering laufen deterministisch im Tool.
requirements: pypdf, reportlab, python-docx
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from xml.sax.saxutils import escape as xml_escape

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kern ohne Open-WebUI-Abhängigkeit (testbar auf jedem Rechner)
# ---------------------------------------------------------------------------

SUPPORTED_INPUT = {".pdf", ".docx"}
SUPPORTED_OUTPUT = {"pdf", "docx"}
DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@dataclass
class Block:
    """Ein Absatz des Quelldokuments."""

    page: int
    text: str


@dataclass
class Chunk:
    """Mehrere Absätze, die zusammen an das Modell gehen."""

    blocks: list[Block] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)


_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_SOFT_BREAK = re.compile(r"(?<!\n)\n(?!\n)")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


_SENTENCE_CLOSERS = ".!?:;"
_LIST_STARTERS = "-•–*·"
_SHORT_LINE_RATIO = 0.75


def normalize_page_text(raw: str) -> list[str]:
    """Rohtext einer Seite in Absätze zerlegen.

    PDF-Extraktion liefert meist keine Leerzeilen zwischen Absätzen. Deshalb
    gilt eine Zeile als Absatzende, wenn sie eine Leerzeile ist, oder wenn sie
    deutlich kürzer als die längste Zeile der Seite ist und entweder mit
    Satzzeichen endet oder die nächste Zeile groß, mit Ziffer oder Aufzählungs-
    zeichen beginnt. Silbentrennung am Zeilenende wird zusammengefügt.
    """
    if not raw:
        return []
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    lines = [line.strip() for line in text.split("\n")]
    non_empty = [line for line in lines if line]
    if not non_empty:
        return []
    threshold = _SHORT_LINE_RATIO * max(len(line) for line in non_empty)

    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            joined = _MULTI_SPACE.sub(" ", " ".join(current)).strip()
            if joined:
                paragraphs.append(joined)
            current.clear()

    for index, line in enumerate(lines):
        if not line:
            flush()
            continue
        current.append(line)
        next_line = next((candidate for candidate in lines[index + 1 :] if candidate), "")
        if not next_line:
            continue
        is_short = len(line) < threshold
        ends_sentence = line[-1] in _SENTENCE_CLOSERS
        next_starts_new = next_line[0].isupper() or next_line[0].isdigit() or next_line[0] in _LIST_STARTERS
        if is_short and (ends_sentence or next_starts_new):
            flush()
    flush()
    return paragraphs


def extract_blocks_from_pdf(path: str, max_pages: int) -> tuple[list[Block], int]:
    """Text seitenweise aus einer PDF holen. Gibt (Blöcke, Seitenzahl) zurück."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise ValueError(
            f"Das Dokument hat {page_count} Seiten, erlaubt sind höchstens {max_pages}."
        )
    blocks: list[Block] = []
    for index, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        for paragraph in normalize_page_text(raw):
            blocks.append(Block(page=index, text=paragraph))
    return blocks, page_count


def extract_blocks_from_docx(path: str) -> tuple[list[Block], int]:
    """Absätze aus einer DOCX holen. DOCX kennt keine festen Seiten, alles ist Seite 1."""
    import docx

    document = docx.Document(path)
    blocks = [
        Block(page=1, text=p.text.strip()) for p in document.paragraphs if p.text.strip()
    ]
    return blocks, 1


def extract_blocks(path: str, max_pages: int) -> tuple[list[Block], int]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_blocks_from_pdf(path, max_pages)
    if ext == ".docx":
        return extract_blocks_from_docx(path)
    raise ValueError(f"Dateityp {ext or '(ohne Endung)'} wird nicht unterstützt.")


_SENTENCE_END = re.compile(r"(?<=[.!?:;])\s+")


def split_long_block(block: Block, max_chars: int) -> list[Block]:
    """Einen überlangen Absatz an Satzgrenzen teilen, damit er in einen Chunk passt."""
    if len(block.text) <= max_chars:
        return [block]
    pieces: list[Block] = []
    current = ""
    for sentence in _SENTENCE_END.split(block.text):
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(Block(page=block.page, text=current))
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(Block(page=block.page, text=current))
    # Harte Grenze für Absätze ohne Satzzeichen
    hard: list[Block] = []
    for piece in pieces:
        text = piece.text
        while len(text) > max_chars:
            hard.append(Block(page=piece.page, text=text[:max_chars]))
            text = text[max_chars:]
        hard.append(Block(page=piece.page, text=text))
    return hard


def chunk_blocks(blocks: list[Block], max_chars: int) -> list[Chunk]:
    """Absätze zu Chunks bündeln, ohne Absätze zu zerreißen."""
    chunks: list[Chunk] = []
    current = Chunk()
    for block in blocks:
        for piece in split_long_block(block, max_chars):
            if current.blocks and current.chars + len(piece.text) > max_chars:
                chunks.append(current)
                current = Chunk()
            current.blocks.append(piece)
    if current.blocks:
        chunks.append(current)
    return chunks


SEPARATOR = "\n\n"
BLOCK_MARKER = "[[{n}]]"
_MARKER_RE = re.compile(r"\[\[(\d+)\]\]\s*")


def chunk_to_prompt(chunk: Chunk) -> str:
    """Absätze nummeriert in einen Prompt-Text schreiben."""
    lines = []
    for n, block in enumerate(chunk.blocks, start=1):
        lines.append(f"{BLOCK_MARKER.format(n=n)} {block.text}")
    return SEPARATOR.join(lines)


def parse_translation(reply: str, expected: int) -> list[str]:
    """Antwort des Modells wieder in Absätze zerlegen.

    Erwartet dieselben [[n]]-Marker. Fehlen Marker, wird an Leerzeilen geteilt.
    Stimmt die Zahl nicht, wird aufgefüllt oder zusammengefasst, damit das
    Dokument nie Absätze verliert.
    """
    reply = reply.strip()
    reply = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", reply).strip()
    found: dict[int, str] = {}
    positions = list(_MARKER_RE.finditer(reply))
    if positions:
        for i, match in enumerate(positions):
            start = match.end()
            end = positions[i + 1].start() if i + 1 < len(positions) else len(reply)
            found[int(match.group(1))] = reply[start:end].strip()
        parts = [found.get(n, "") for n in range(1, expected + 1)]
        if all(parts):
            return parts
        # Marker unvollständig: Rest über Leerzeilen auffüllen
    parts = [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()]
    if len(parts) == expected:
        return parts
    if len(parts) > expected:
        head = parts[: expected - 1]
        head.append(" ".join(parts[expected - 1 :]))
        return head
    return parts + [""] * (expected - len(parts))


def parse_glossary(text: str) -> list[tuple[str, str]]:
    """Glossar im Format 'Quelle=Ziel', eine Zeile pro Eintrag."""
    entries: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        source, target = line.split("=", 1)
        if source.strip() and target.strip():
            entries.append((source.strip(), target.strip()))
    return entries


def build_system_prompt(target_language: str, glossary: list[tuple[str, str]]) -> str:
    glossary_text = ""
    if glossary:
        rows = "\n".join(f"- {src} → {dst}" for src, dst in glossary)
        glossary_text = f"\n\nVerbindliches Glossar:\n{rows}"
    return (
        "Du bist ein professioneller Fachübersetzer für Steuer-, Rechts- und "
        f"Geschäftsdokumente. Übersetze den Text in {target_language}.\n"
        "Regeln:\n"
        "- Jeder Absatz beginnt mit einem Marker wie [[3]]. Behalte jeden Marker "
        "exakt bei und übersetze den Text dahinter.\n"
        "- Gib genau so viele Absätze zurück, wie du bekommst, in derselben Reihenfolge.\n"
        "- Zahlen, Daten, Beträge, Aktenzeichen, Paragraphen und Eigennamen bleiben zeichengenau "
        "unverändert, auch Tausenderpunkte und Dezimalkommas (12.345,67 bleibt 12.345,67).\n"
        "- Keine Erklärungen, keine Anmerkungen, keine Einleitung. Nur die Übersetzung."
        f"{glossary_text}"
    )


Translator = Callable[[str, str], Awaitable[str]]
"""async def translator(system_prompt, user_text) -> reply"""


async def translate_blocks(
    blocks: list[Block],
    translator: Translator,
    target_language: str,
    max_chars: int,
    glossary: list[tuple[str, str]],
    max_retries: int = 2,
    on_progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> list[Block]:
    """Alle Absätze übersetzen. Seitenzuordnung bleibt erhalten."""
    system_prompt = build_system_prompt(target_language, glossary)
    chunks = chunk_blocks(blocks, max_chars)
    translated: list[Block] = []
    for index, chunk in enumerate(chunks, start=1):
        if on_progress:
            await on_progress(index, len(chunks))
        prompt = chunk_to_prompt(chunk)
        parts: list[str] = []
        last_error: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                reply = await translator(system_prompt, prompt)
                parts = parse_translation(reply, len(chunk.blocks))
                if any(parts):
                    break
            except Exception as exc:  # Modell- oder Netzfehler
                last_error = exc
                await asyncio.sleep(min(2**attempt, 8))
        if not any(parts):
            raise RuntimeError(
                f"Abschnitt {index} von {len(chunks)} konnte nicht übersetzt werden"
                + (f": {type(last_error).__name__}" if last_error else ".")
            )
        for block, text in zip(chunk.blocks, parts):
            translated.append(Block(page=block.page, text=text or block.text))
    return translated


def _register_font(font_path: str) -> str:
    """DejaVu (volles Unicode) registrieren, sonst Helvetica (WinAnsi, reicht für Deutsch)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if font_path and os.path.exists(font_path):
        name = "DocFont"
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, font_path))
        return name
    return "Helvetica"


def render_pdf(
    blocks: list[Block],
    out_path: str,
    title: str,
    font_path: str = DEFAULT_FONT_PATH,
    page_breaks: bool = True,
) -> None:
    """Übersetzte Absätze als Fließtext-PDF rendern. Layout der Quelle wird nicht nachgebaut."""
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    font = _register_font(font_path)
    style = ParagraphStyle(
        "Body",
        fontName=font,
        fontSize=10.5,
        leading=14,
        alignment=TA_JUSTIFY,
        spaceAfter=6,
    )
    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=title,
    )
    story: list[Any] = []
    current_page = None
    for block in blocks:
        if page_breaks and current_page is not None and block.page != current_page:
            story.append(PageBreak())
        current_page = block.page
        story.append(Paragraph(xml_escape(block.text), style))
    if not story:
        story.append(Spacer(1, 1))
    doc.build(story)


def render_docx(blocks: list[Block], out_path: str, page_breaks: bool = True) -> None:
    """Übersetzte Absätze als DOCX schreiben."""
    import docx
    from docx.enum.text import WD_BREAK

    document = docx.Document()
    current_page = None
    for block in blocks:
        if page_breaks and current_page is not None and block.page != current_page:
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        current_page = block.page
        document.add_paragraph(block.text)
    document.save(out_path)


def output_filename(source_name: str, target_language: str, output_format: str) -> str:
    base = os.path.splitext(os.path.basename(source_name))[0] or "dokument"
    lang = re.sub(r"[^A-Za-z0-9]+", "-", target_language).strip("-").lower() or "translated"
    base = re.sub(r"[^\w\-. ]+", "_", base)[:80]
    return f"{base}.{lang}.{output_format}"


# ---------------------------------------------------------------------------
# Open-WebUI-Tool
# ---------------------------------------------------------------------------


class Tools:
    class Valves(BaseModel):
        MODEL_ID: str = Field(
            default="",
            description="Modell für die Übersetzung. Leer = das Modell des aktuellen Chats.",
        )
        MAX_CHUNK_CHARS: int = Field(
            default=3000,
            description="Zeichen pro Anfrage an das Modell. Kleiner = sicherer bei kleinen Kontextfenstern.",
        )
        MAX_PAGES: int = Field(default=60, description="Obergrenze für Seiten je Dokument.")
        MAX_RETRIES: int = Field(default=2, description="Wiederholungen pro Abschnitt bei Fehlern.")
        TEMPERATURE: float = Field(default=0.2, description="Temperatur für die Übersetzung.")
        FONT_PATH: str = Field(
            default=DEFAULT_FONT_PATH,
            description="TTF-Schrift für die PDF-Ausgabe. Fehlt sie, wird Helvetica genutzt.",
        )
        GLOSSARY: str = Field(
            default="",
            description="Verbindliche Begriffe, eine Zeile pro Eintrag im Format Quelle=Ziel.",
        )

    class UserValves(BaseModel):
        DEFAULT_TARGET_LANGUAGE: str = Field(
            default="Englisch", description="Zielsprache, wenn im Chat keine genannt wird."
        )
        DEFAULT_OUTPUT_FORMAT: str = Field(
            default="pdf", description="Ausgabeformat, wenn im Chat keines genannt wird: pdf oder docx."
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def translate_document(
        self,
        target_language: str = "",
        output_format: str = "",
        file_name: str = "",
        __user__: Optional[dict] = None,
        __files__: Optional[list] = None,
        __request__: Any = None,
        __event_emitter__: Any = None,
        __model__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> str:
        """
        Übersetzt ein im Chat angehängtes PDF- oder DOCX-Dokument vollständig in eine andere Sprache und erzeugt daraus eine neue Datei zum Download. Nutze dieses Tool immer, wenn der Nutzer ein Dokument übersetzt haben möchte und eine Datei angehängt ist. Übersetze den Dokumentinhalt nicht selbst.

        :param target_language: Zielsprache, z. B. "Englisch", "English", "Französisch". Leer = Standard des Nutzers.
        :param output_format: "pdf" oder "docx". Leer = Standard des Nutzers.
        :param file_name: Name der angehängten Datei, falls mehrere angehängt sind. Leer = die zuletzt angehängte PDF/DOCX.
        :return: Kurzer Ergebnisbericht mit Download-Link. Gib dem Nutzer den Link unverändert weiter.
        """
        started = time.monotonic()
        user_valves = (__user__ or {}).get("valves")
        target_language = (target_language or "").strip() or getattr(
            user_valves, "DEFAULT_TARGET_LANGUAGE", "Englisch"
        )
        output_format = (output_format or "").strip().lower().lstrip(".") or getattr(
            user_valves, "DEFAULT_OUTPUT_FORMAT", "pdf"
        )
        if output_format not in SUPPORTED_OUTPUT:
            return self._fail(f"Ausgabeformat '{output_format}' wird nicht unterstützt. Erlaubt: pdf, docx.")

        if not __user__ or not __user__.get("id"):
            return self._fail("Kein Nutzerkontext vorhanden. Abgebrochen.")
        if __request__ is None:
            return self._fail("Kein Request-Kontext vorhanden. Abgebrochen.")

        try:
            source = await self._resolve_source_file(__files__ or [], file_name, __user__["id"])
        except LookupError as exc:
            return self._fail(str(exc))

        await self._status(__event_emitter__, f"Lese {source['name']} …")
        try:
            blocks, page_count = await asyncio.to_thread(
                extract_blocks, source["path"], self.valves.MAX_PAGES
            )
        except Exception as exc:
            log.warning("pdf_translate: extraction failed (%s)", type(exc).__name__)
            return self._fail(f"Dokument konnte nicht gelesen werden: {exc}")

        if not blocks:
            return self._fail(
                "Im Dokument wurde kein Text gefunden. Vermutlich ein Scan ohne Textebene. "
                "Bitte zuerst eine OCR-Version erzeugen."
            )

        model_id = self._pick_model_id(__model__, __metadata__)
        if not model_id:
            return self._fail("Kein Modell ermittelbar. Bitte MODEL_ID in den Tool-Einstellungen setzen.")

        translator = self._make_translator(__request__, __user__, model_id)
        glossary = parse_glossary(self.valves.GLOSSARY)
        total_chunks = len(chunk_blocks(blocks, self.valves.MAX_CHUNK_CHARS))

        async def progress(done: int, total: int) -> None:
            await self._status(
                __event_emitter__,
                f"Übersetze nach {target_language}: Abschnitt {done} von {total} …",
            )

        try:
            translated = await translate_blocks(
                blocks,
                translator,
                target_language,
                self.valves.MAX_CHUNK_CHARS,
                glossary,
                max_retries=self.valves.MAX_RETRIES,
                on_progress=progress,
            )
        except Exception as exc:
            log.warning("pdf_translate: translation failed (%s)", type(exc).__name__)
            return self._fail(f"Übersetzung abgebrochen: {exc}")

        await self._status(__event_emitter__, f"Erzeuge {output_format.upper()} …")
        out_name = output_filename(source["name"], target_language, output_format)
        try:
            file_record = await self._store_output(
                translated, out_name, output_format, __user__["id"], source["name"]
            )
        except Exception as exc:
            log.warning("pdf_translate: render/store failed (%s)", type(exc).__name__)
            return self._fail(f"Datei konnte nicht erzeugt werden: {exc}")

        url = f"/api/v1/files/{file_record['id']}/content"
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "files",
                    "data": {
                        "files": [
                            {
                                "type": "file",
                                "id": file_record["id"],
                                "url": url,
                                "name": out_name,
                                "size": file_record["size"],
                                "content_type": file_record["content_type"],
                            }
                        ]
                    },
                }
            )
        await self._status(__event_emitter__, "Übersetzung fertig.", done=True)

        seconds = round(time.monotonic() - started)
        return json.dumps(
            {
                "status": "success",
                "message": (
                    f"Das Dokument wurde nach {target_language} übersetzt. "
                    f"Download: [{out_name}]({url})"
                ),
                "download_url": url,
                "file_name": out_name,
                "pages": page_count,
                "paragraphs": len(translated),
                "chunks": total_chunks,
                "seconds": seconds,
                "instruction_for_assistant": (
                    "Zeige dem Nutzer den Download-Link als Markdown-Link. "
                    "Gib den Dokumentinhalt nicht wieder."
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
        except Exception:  # Statusanzeige darf den Lauf nie abbrechen
            pass

    def _pick_model_id(self, model: Optional[dict], metadata: Optional[dict]) -> str:
        if self.valves.MODEL_ID.strip():
            return self.valves.MODEL_ID.strip()
        if model and model.get("id"):
            return str(model["id"])
        if metadata and metadata.get("model_id"):
            return str(metadata["model_id"])
        return ""

    async def _resolve_source_file(self, files: list, wanted_name: str, user_id: str) -> dict:
        """Angehängte Datei finden, Besitz prüfen, lokalen Pfad auflösen."""
        from open_webui.models.files import Files
        from open_webui.storage.provider import Storage

        candidates = []
        for item in files:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            name = item.get("name") or (item.get("file") or {}).get("filename") or ""
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_INPUT and item.get("id"):
                candidates.append({"id": item["id"], "name": name})
        if not candidates:
            raise LookupError(
                "Keine PDF- oder DOCX-Datei im Chat angehängt. Bitte das Dokument anhängen und erneut fragen."
            )
        if wanted_name:
            wanted = wanted_name.strip().lower()
            matches = [c for c in candidates if c["name"].lower() == wanted] or [
                c for c in candidates if wanted in c["name"].lower()
            ]
            if not matches:
                names = ", ".join(c["name"] for c in candidates)
                raise LookupError(f"Datei '{wanted_name}' nicht gefunden. Angehängt sind: {names}")
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

    def _make_translator(self, request: Any, user_dict: dict, model_id: str) -> Translator:
        """Übersetzungsaufruf über die interne Chat-Pipeline von Open WebUI."""
        from open_webui.models.users import Users
        from open_webui.utils.chat import generate_chat_completion

        temperature = self.valves.TEMPERATURE
        user_holder: dict = {}

        async def translator(system_prompt: str, user_text: str) -> str:
            if "user" not in user_holder:
                user_holder["user"] = await Users.get_user_by_id(user_dict["id"])
                if user_holder["user"] is None:
                    raise RuntimeError("Nutzer nicht gefunden.")
            form_data = {
                "model": model_id,
                "stream": False,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
            }
            response = await generate_chat_completion(request, form_data, user_holder["user"])
            return _extract_reply(response)

        return translator

    async def _store_output(
        self, blocks: list[Block], out_name: str, output_format: str, user_id: str, source_name: str
    ) -> dict:
        """Datei rendern, in den Open-WebUI-Speicher legen und einen Dateieintrag anlegen."""
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = os.path.join(tmp, out_name)
            if output_format == "pdf":
                await asyncio.to_thread(
                    render_pdf, blocks, tmp_path, os.path.splitext(out_name)[0], self.valves.FONT_PATH
                )
                content_type = "application/pdf"
            else:
                await asyncio.to_thread(render_docx, blocks, tmp_path)
                content_type = (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            with open(tmp_path, "rb") as fh:
                payload = fh.read()

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
                    "content_type": content_type,
                    "size": len(contents),
                    "data": {"generated_by": "pdf_translate", "source_name": source_name},
                },
            ),
        )
        if record is None:
            raise RuntimeError("Dateieintrag konnte nicht angelegt werden.")
        return {"id": file_id, "size": len(contents), "content_type": content_type}


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Denkblöcke von Reasoning-Modellen (z. B. Qwen) aus der Antwort entfernen."""
    cleaned = _THINK_RE.sub("", text)
    if "<think>" in cleaned and "</think>" not in cleaned:
        cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip()


def _extract_reply(response: Any) -> str:
    """Antworttext aus OpenAI- oder Ollama-förmigen Antworten holen."""
    return strip_reasoning(_raw_reply(response))


def _raw_reply(response: Any) -> str:
    if response is None:
        raise RuntimeError("Leere Antwort vom Modell.")
    if hasattr(response, "body"):  # fastapi Response bei Fehlern
        body = response.body.decode("utf-8", "replace") if isinstance(response.body, bytes) else str(response.body)
        try:
            response = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"Unerwartete Modellantwort ({getattr(response, 'status_code', '?')}).")
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        if "error" in response:
            err = response["error"]
            raise RuntimeError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
        choices = response.get("choices")
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # Multimodale Antwortteile
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if content:
                return str(content)
        message = response.get("message")
        if isinstance(message, dict) and message.get("content"):
            return str(message["content"])
        if response.get("response"):
            return str(response["response"])
    raise RuntimeError("Modellantwort enthielt keinen Text.")
