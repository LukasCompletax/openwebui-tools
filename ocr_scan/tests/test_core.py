"""Tests für ocr_scan. Alle Inhalte erfunden.

Teil 1 läuft überall (Argumente, Namen, Fehlertexte).
Teil 2 läuft nur, wenn ocrmypdf installiert ist, etwa im Image open-webui-ocr:

    docker run --rm -v "$PWD:/w" -w /w --entrypoint python open-webui-ocr:local ocr_scan/tests/test_core.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ocr_scan_tool as t  # noqa: E402


def test_languages():
    assert t.validate_languages("") == "deu+eng"
    assert t.validate_languages(" DEU + eng ") == "deu+eng"
    for bad in ("de", "deu;rm -rf", "deu+", "../deu"):
        try:
            t.validate_languages(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_build_command():
    cmd = t.build_command("/in/scan.pdf", "/out/x.pdf", "deu", 99, True, False, False)
    assert cmd[0] == "ocrmypdf" and cmd[-2:] == ["/in/scan.pdf", "/out/x.pdf"]
    assert "--skip-text" in cmd and "--force-ocr" not in cmd
    assert "--rotate-pages" in cmd and "--deskew" not in cmd
    assert cmd[cmd.index("--jobs") + 1] == "8", "Jobs werden auf 8 begrenzt"
    assert cmd[cmd.index("--oversample") + 1] == "300"
    assert "--image-dpi" not in cmd
    cmd = t.build_command("/in/foto.JPG", "/out/x.pdf", "deu+eng", 2, False, True, True)
    assert "--force-ocr" in cmd and "--skip-text" not in cmd
    assert "--deskew" in cmd and "--image-dpi" in cmd


def test_output_name_and_exit_messages():
    assert t.output_name("Bescheid 2025.pdf") == "Bescheid 2025.ocr.pdf"
    assert t.output_name("../../x/y.png") == "y.ocr.pdf"
    assert t.output_name("") == "scan.ocr.pdf"
    assert "Textebene" in t.EXIT_MESSAGES[6] and "Zeitlimit" in t.EXIT_MESSAGES[124]


def test_tool_reports_missing_ocrmypdf_cleanly():
    if t.ocr_available():
        return
    tool = t.Tools()
    result = asyncio.run(tool.ocr_document(__user__={"id": "u1"}, __files__=[]))
    assert "nicht installiert" in result


def _make_scan_pdf(path: str) -> None:
    """Rasterisiert Text in ein Bild und speichert es als PDF ohne Textebene."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1654, 2339), "white")  # A4 bei 200 dpi
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    lines = [
        "Musterfirma Beispiel GmbH",
        "Bescheid fuer das Jahr 2025",
        "Der Betrag von 1.234,56 EUR ist faellig.",
        "Aktenzeichen TEST-0001",
    ]
    for i, line in enumerate(lines):
        draw.text((150, 200 + i * 120), line, fill="black", font=font)
    image.save(path, "PDF", resolution=200.0)


def test_real_ocr_roundtrip():
    if not t.ocr_available():
        print("  (ocrmypdf fehlt, OCR-Roundtrip übersprungen)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "scan.pdf")
        _make_scan_pdf(src)
        from pypdf import PdfReader

        assert not (PdfReader(src).pages[0].extract_text() or "").strip(), "Quelle darf keine Textebene haben"

        out = os.path.join(tmp, "scan.ocr.pdf")
        cmd = t.build_command(src, out, "deu+eng", 2, True, False, False)
        code, detail = asyncio.run(t.run_ocr(cmd, 300))
        assert code == 0, (code, detail)
        preview, pages, total = t.extract_text(out, 5000)
        assert pages == 1 and total > 50
        for needle in ("Musterfirma", "2025", "TEST-0001"):
            assert needle in preview, preview

        # Zweiter Lauf mit --skip-text auf der OCR-PDF: Seiten mit Text werden übersprungen, Code 0
        out2 = os.path.join(tmp, "zweimal.pdf")
        code, _ = asyncio.run(t.run_ocr(t.build_command(out, out2, "deu", 1, False, False, False), 300))
        assert code == 0 and os.path.exists(out2)
        assert t.extract_text(out2, 0)[2] == total, "Textebene bleibt unverändert"

        # Ohne --skip-text/--force-ocr lehnt ocrmypdf eine PDF mit Textebene ab, Code 6
        out3 = os.path.join(tmp, "dreimal.pdf")
        code, _ = asyncio.run(t.run_ocr(["ocrmypdf", "-l", "deu", "--quiet", out, out3], 300))
        assert code == 6

        # Pfade in Fehlermeldungen werden maskiert
        code, detail = asyncio.run(t.run_ocr(["ocrmypdf", "/nicht/da.pdf", out2], 60))
        assert code != 0 and "/nicht/da.pdf" not in detail


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
