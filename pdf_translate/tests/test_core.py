"""Tests für den Kern des Übersetzungs-Tools, ohne Open WebUI.

Ausführen:
    python -m pytest pdf_translate/tests -q
oder ohne pytest:
    python pdf_translate/tests/test_core.py

Alle Inhalte sind frei erfunden.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pdf_translate_tool as t  # noqa: E402

SAMPLE_PAGES = [
    [
        "Musterfirma Beispiel GmbH",
        "Sehr geehrte Damen und Herren, hiermit übersenden wir Ihnen die Unterlagen "
        "für das Geschäftsjahr. Die Angaben beruhen auf fiktiven Zahlen und dienen "
        "ausschließlich dem Test der Übersetzungsfunktion.",
        "Der Gesamtbetrag von 12.345,67 € ist bis zum 31.12. zu überweisen. "
        "Bitte geben Sie das Aktenzeichen TEST-0001 an.",
    ],
    [
        "Anlage 1: Erläuterungen",
        "Die Straße, die Größe und die Übergänge enthalten Umlaute und ein ß, "
        "damit die Zeichenkodierung geprüft wird.",
    ],
]


def make_sample_pdf(path: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    style = getSampleStyleSheet()["Normal"]
    story = []
    for index, page in enumerate(SAMPLE_PAGES):
        if index:
            story.append(PageBreak())
        for paragraph in page:
            story.append(Paragraph(paragraph, style))
    SimpleDocTemplate(path, pagesize=A4).build(story)


def make_sample_docx(path: str) -> None:
    import docx

    document = docx.Document()
    for page in SAMPLE_PAGES:
        for paragraph in page:
            document.add_paragraph(paragraph)
    document.save(path)


def read_pdf_text(path: str) -> list[str]:
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(path).pages]


async def fake_translator(system_prompt: str, user_text: str) -> str:
    """Spiegelt die Marker zurück und markiert jeden Absatz als übersetzt."""
    assert "[[1]]" in user_text
    out = []
    for match in t._MARKER_RE.finditer(user_text):
        n = int(match.group(1))
        out.append(f"[[{n}]] EN{n}")
    return "<think>kurz nachgedacht</think>\n" + "\n\n".join(out)


def test_normalize_page_text():
    raw = "Erste Zeile\nzweite Zeile mit Silben-\ntrennung.\n\nZweiter   Absatz."
    assert t.normalize_page_text(raw) == [
        "Erste Zeile zweite Zeile mit Silbentrennung.",
        "Zweiter Absatz.",
    ]


def test_normalize_page_text_without_blank_lines():
    """pypdf liefert meist keine Leerzeilen; Absätze müssen über Zeilenlänge erkannt werden."""
    raw = (
        "Musterfirma Beispiel GmbH\n"
        "Sehr geehrte Damen und Herren, hiermit übersenden wir Ihnen die Unterlagen für das\n"
        "Geschäftsjahr. Die Angaben beruhen auf fiktiven Zahlen und dienen ausschließlich dem Test der\n"
        "Übersetzungsfunktion.\n"
        "Der Gesamtbetrag von 12.345,67 € ist bis zum 31.12. zu überweisen. Bitte geben Sie das\n"
        "Aktenzeichen TEST-0001 an.\n"
    )
    paragraphs = t.normalize_page_text(raw)
    assert len(paragraphs) == 3, paragraphs
    assert paragraphs[0] == "Musterfirma Beispiel GmbH"
    assert paragraphs[1].endswith("Übersetzungsfunktion.")
    assert paragraphs[2].startswith("Der Gesamtbetrag") and paragraphs[2].endswith("an.")


def test_chunking_keeps_paragraphs_and_splits_long_ones():
    blocks = [t.Block(1, "a" * 50), t.Block(1, "b" * 50), t.Block(2, "c. " * 60)]
    chunks = t.chunk_blocks(blocks, max_chars=120)
    assert all(c.chars <= 120 for c in chunks)
    joined = " ".join(b.text for c in chunks for b in c.blocks)
    assert "a" * 50 in joined and "b" * 50 in joined
    assert {b.page for c in chunks for b in c.blocks} == {1, 2}


def test_parse_translation_variants():
    assert t.parse_translation("[[1]] one\n\n[[2]] two", 2) == ["one", "two"]
    assert t.parse_translation("one\n\ntwo", 2) == ["one", "two"]
    assert t.parse_translation("```\n[[1]] one\n\n[[2]] two\n```", 2) == ["one", "two"]
    # Zu viele Teile: Rest wird zusammengefasst
    assert t.parse_translation("a\n\nb\n\nc", 2) == ["a", "b c"]
    # Zu wenige Teile: wird aufgefüllt, nie verkürzt
    assert t.parse_translation("a", 2) == ["a", ""]


def test_strip_reasoning():
    assert t.strip_reasoning("<think>x</think>\nResult") == "Result"
    assert t.strip_reasoning("Result<think>unfinished") == "Result"
    assert t.strip_reasoning("plain") == "plain"


def test_glossary_and_filename():
    assert t.parse_glossary("Bescheid=tax assessment\n# Kommentar\nFrist = deadline") == [
        ("Bescheid", "tax assessment"),
        ("Frist", "deadline"),
    ]
    assert t.output_filename("Jahresabschluss 2025.pdf", "Englisch", "pdf") == (
        "Jahresabschluss 2025.englisch.pdf"
    )
    assert t.output_filename("x.PDF", "British English", "docx") == "x.british-english.docx"


def test_end_to_end_pdf_to_pdf_and_docx():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "quelle.pdf")
        make_sample_pdf(src)

        blocks, pages = t.extract_blocks(src, max_pages=10)
        assert pages == 2
        assert len(blocks) == sum(len(p) for p in SAMPLE_PAGES)
        assert any("ß" in b.text for b in blocks)
        assert any("12.345,67" in b.text for b in blocks)

        progress = []

        async def on_progress(done, total):
            progress.append((done, total))

        translated = asyncio.run(
            t.translate_blocks(
                blocks, fake_translator, "Englisch", max_chars=400, glossary=[], on_progress=on_progress
            )
        )
        assert len(translated) == len(blocks)
        assert [b.page for b in translated] == [b.page for b in blocks]
        assert all(b.text.startswith("EN") for b in translated)
        assert progress and progress[-1][0] == progress[-1][1]

        out_pdf = os.path.join(tmp, "ziel.pdf")
        t.render_pdf(translated, out_pdf, "Test", font_path=t.DEFAULT_FONT_PATH)
        texts = read_pdf_text(out_pdf)
        assert len(texts) == 2, "Seitenumbrüche der Quelle sollen erhalten bleiben"
        assert "EN1" in texts[0] and "EN4" in texts[1]

        out_docx = os.path.join(tmp, "ziel.docx")
        t.render_docx(translated, out_docx)
        docx_blocks, _ = t.extract_blocks(out_docx, max_pages=10)
        assert [b.text for b in docx_blocks] == [b.text for b in translated]


def test_pdf_with_umlauts_renders_with_fallback_font():
    """Ohne DejaVu muss Helvetica (WinAnsi) deutsche Umlaute tragen."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "u.pdf")
        t.render_pdf([t.Block(1, "Größe, Übergang, Straße, 12 €")], out, "U", font_path="/nonexistent.ttf")
        text = read_pdf_text(out)[0]
        assert "Größe" in text and "Straße" in text


def test_docx_source():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "quelle.docx")
        make_sample_docx(src)
        blocks, pages = t.extract_blocks(src, max_pages=10)
        assert pages == 1
        assert len(blocks) == sum(len(p) for p in SAMPLE_PAGES)


def test_translation_failure_is_reported_not_swallowed():
    async def broken(system_prompt, user_text):
        raise ConnectionError("down")

    blocks = [t.Block(1, "Text")]
    try:
        asyncio.run(t.translate_blocks(blocks, broken, "Englisch", 100, [], max_retries=0))
    except RuntimeError as exc:
        assert "ConnectionError" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_max_pages_enforced():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "quelle.pdf")
        make_sample_pdf(src)
        try:
            t.extract_blocks(src, max_pages=1)
        except ValueError as exc:
            assert "höchstens 1" in str(exc)
        else:
            raise AssertionError("expected ValueError")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
