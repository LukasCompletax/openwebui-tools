"""Tests für den Kern von beratungsvertrag_docx, ohne Open WebUI und ohne die echte Vorlage.

Die Vorlage wird hier synthetisch mit denselben Platzhaltern nachgebaut. Alle Inhalte sind erfunden.

Ausführen:
    docker run --rm -v "$PWD:/w:ro" -w /w --entrypoint python open-webui-ocr:local beratungsvertrag_docx/tests/test_core.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import beratungsvertrag_docx_tool as t  # noqa: E402


def make_template(path: str) -> None:
    """Synthetische Vorlage mit der Struktur der echten: einzelne Runs, Blöcke, Listenzeilen."""
    import docx

    document = docx.Document()
    p = document.add_paragraph()
    p.add_run("An: ").bold = True
    p.add_run("{{Mandant_Name}}")
    document.add_paragraph("{{Mandant_Anschrift}}")
    document.add_paragraph("{{Mandant_Email}}")
    document.add_paragraph("Mandatsvertrag {{Mandant_Kurzname}} {{Jahr}}")
    document.add_paragraph("Datum: {{Vertragsdatum}}")
    # Platzhalter über mehrere Runs verteilt, wie Word es nach Bearbeitung oft speichert
    p = document.add_paragraph()
    p.add_run("Gespräch vom {{Gespr")
    p.add_run("aechsdatum}} war gut.")
    document.add_paragraph("Anpassung zum {{Erste_Anpassung_Datum}}.")
    document.add_paragraph(
        "{{#Anzahlung}}Anzahlung von {{Anzahlung_Prozent}} % vorab. {{/Anzahlung}}Rechnung bei Zugang fällig."
    )
    document.add_paragraph("{{#Verbraucher}}")
    document.add_paragraph("Widerrufsbelehrung (nur für Verbraucher)")
    document.add_paragraph("Sie können widerrufen.")
    document.add_paragraph("{{/Verbraucher}}")
    document.add_paragraph("Annahmeerklärung")
    document.add_paragraph("Anlage 1 vom {{Vertragsdatum}} mit {{Mandant_Name}}")
    document.add_paragraph("Registrierung: einmalig EUR 250,00", style="List Bullet")
    document.add_paragraph(
        "Finanzbuchführung ({{Buchungen_Anzahl}} Buchungen): EUR {{Pauschale_Buchfuehrung_Monat}} je Monat",
        style="List Bullet",
    )
    document.add_paragraph("Lohnbuchführung: Einrichtung EUR {{Lohn_Einrichtung}}", style="List Bullet")
    document.add_paragraph(
        "Jahresabschluss: EUR {{Pauschale_Jahresabschluss}} für {{Erstes_Geschaeftsjahr}}", style="List Bullet"
    )
    document.add_paragraph("Steuererklärungen: EUR {{Pauschale_Steuererklaerung}} je Jahr", style="List Bullet")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Unterschrift {{Mandant_Name}}"
    document.save(path)


def _texts(path: str) -> list[str]:
    import docx

    document = docx.Document(path)
    texts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.extend(p.text for p in cell.paragraphs)
    return texts


FULL = dict(
    mandant_name="Beispiel Handel GmbH",
    mandant_anschrift="Musterstraße 1, 12345 Musterstadt",
    gespraechsdatum="02.10.2026",
    mandant_email="buchhaltung@beispiel-handel.example",
    mandant_kurzname="Beispiel Handel",
    vertragsdatum="16.10.2026",
    verbraucher=False,
    anzahlung_prozent="30",
    buchungen_anzahl=120,
    pauschale_buchfuehrung_monat="450",
    lohn_einrichtung="150,00",
    pauschale_jahresabschluss="1.800,00",
    erstes_geschaeftsjahr="2026",
    pauschale_steuererklaerung="600",
)


def test_erste_anpassung():
    cases = {
        "16.10.2026": "01.01.2028",
        "01.01.2027": "01.01.2028",
        "31.12.2026": "01.01.2028",
        "02.01.2026": "01.01.2028",
        "01.01.2026": "01.01.2027",
        "29.02.2028": "01.01.2030",
    }
    for given, expected in cases.items():
        assert t.format_date(t.erste_anpassung(t.parse_date(given))) == expected, given


def test_parse_date_and_year():
    assert t.parse_date("2026-10-16") == dt.date(2026, 10, 16)
    for bad in ("16/10/2026", "", "heute", "32.01.2026"):
        try:
            t.parse_date(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    assert t.parse_year("2026", "x") == "2026"
    for bad in ("26", "3026", "2026/27"):
        try:
            t.parse_year(bad, "x")
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_amounts():
    cases = {
        "450": "450,00",
        "450,00": "450,00",
        "450.00": "450,00",
        "450,5": "450,50",
        "1.250,00": "1.250,00",
        "1,250.00": "1.250,00",
        "1250.5": "1.250,50",
        "1.250": "1.250,00",
        "1.250.000": "1.250.000,00",
        "EUR 450": "450,00",
        "450 €": "450,00",
        "0,005": "0,01",
    }
    for given, expected in cases.items():
        assert t.format_amount(t.parse_amount(given)) == expected, given
    for bad in ("", "abc", "-5", "4,5,6"):
        try:
            t.parse_amount(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_percent():
    assert t.parse_percent("") is None and t.parse_percent("0") is None and t.parse_percent("nein") is None
    assert t.format_percent(t.parse_percent("30")) == "30"
    assert t.format_percent(t.parse_percent("30 %")) == "30"
    assert t.format_percent(t.parse_percent("12,5")) == "12,5"
    for bad in ("150", "abc", "-1"):
        try:
            t.parse_percent(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_build_contract_full():
    c = t.build_contract(**FULL)
    v = c["values"]
    assert v["Mandant_Name"] == "Beispiel Handel GmbH"
    assert v["Jahr"] == "2026" and v["Vertragsdatum"] == "16.10.2026"
    assert v["Erste_Anpassung_Datum"] == "01.01.2028"
    assert v["Anzahlung_Prozent"] == "30" and c["blocks"] == {"Anzahlung": True, "Verbraucher": False}
    assert v["Pauschale_Buchfuehrung_Monat"] == "450,00" and v["Buchungen_Anzahl"] == "120"
    assert v["Pauschale_Jahresabschluss"] == "1.800,00" and v["Erstes_Geschaeftsjahr"] == "2026"
    assert c["drops"] == []
    assert c["file_name"] == "Mandatsvertrag Beispiel Handel 2026.docx"
    assert c["summary"]["anzahlung"] == "30 %" and c["summary"]["vertragstyp"] == "Unternehmer"


def test_build_contract_defaults_and_drops():
    c = t.build_contract(
        mandant_name="Erika Musterfrau",
        mandant_anschrift="Beispielweg 2\n98765 Beispielhausen",
        gespraechsdatum="2026-10-05",
        verbraucher=True,
        pauschale_steuererklaerung="300",
        today=dt.date(2026, 10, 16),
    )
    v = c["values"]
    assert v["Mandant_Anschrift"] == "Beispielweg 2 98765 Beispielhausen"
    assert v["Mandant_Kurzname"] == "Erika Musterfrau" and v["Jahr"] == "2026"
    assert v["Vertragsdatum"] == "16.10.2026" and v["Erste_Anpassung_Datum"] == "01.01.2028"
    assert c["blocks"] == {"Anzahlung": False, "Verbraucher": True}
    assert sorted(c["drops"]) == sorted(
        ["Pauschale_Buchfuehrung_Monat", "Lohn_Einrichtung", "Pauschale_Jahresabschluss", "Mandant_Email"]
    )
    assert c["summary"]["pauschalen"]["Pauschale_Steuererklaerung"] == "300,00 EUR"
    assert c["summary"]["pauschalen"]["Lohn_Einrichtung"] == "gestrichen"


def test_build_contract_collects_all_errors():
    try:
        t.build_contract(
            mandant_name="",
            mandant_anschrift="",
            gespraechsdatum="gestern",
            pauschale_buchfuehrung_monat="450",
            pauschale_jahresabschluss="abc",
            anzahlung_prozent="200",
        )
    except ValueError as exc:
        message = str(exc)
        for needle in ("mandant_name", "mandant_anschrift", "gespraechsdatum", "buchungen_anzahl", "pauschale_jahresabschluss", "Anzahlung"):
            assert needle in message, needle
    else:
        raise AssertionError("expected ValueError")
    try:
        t.build_contract(mandant_name="A", mandant_anschrift="B", gespraechsdatum="01.01.2026", pauschale_jahresabschluss="1000")
    except ValueError as exc:
        assert "erstes_geschaeftsjahr" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def _fill(**overrides) -> tuple[list[str], dict]:
    tmp = tempfile.mkdtemp()
    template = os.path.join(tmp, "vorlage.docx")
    make_template(template)
    contract = t.build_contract(**{**FULL, **overrides})
    out = os.path.join(tmp, contract["file_name"])
    stats = t.fill_docx(template, contract["values"], contract["blocks"], contract["drops"], out)
    return _texts(out), stats


def test_fill_full_contract():
    texts, stats = _fill()
    joined = "\n".join(texts)
    assert "{{" not in joined and stats["remaining"] == []
    assert "An: Beispiel Handel GmbH" in texts
    assert "Gespräch vom 02.10.2026 war gut." in texts, "Platzhalter über Run-Grenze"
    assert "Anzahlung von 30 % vorab. Rechnung bei Zugang fällig." in texts
    assert "Widerrufsbelehrung (nur für Verbraucher)" not in texts and "Sie können widerrufen." not in texts
    assert "Annahmeerklärung" in texts
    assert "Finanzbuchführung (120 Buchungen): EUR 450,00 je Monat" in texts
    assert "Jahresabschluss: EUR 1.800,00 für 2026" in texts
    assert "Unterschrift Beispiel Handel GmbH" in texts, "Tabellenzelle"
    assert stats["blocks"] == {"Anzahlung": "behalten", "Verbraucher": "entfernt"}
    assert stats["paragraphs_removed"] == 2, "nur die beiden Markerabsätze samt Inhalt dazwischen"


def test_fill_keeps_run_formatting():
    import docx

    tmp = tempfile.mkdtemp()
    template = os.path.join(tmp, "vorlage.docx")
    make_template(template)
    contract = t.build_contract(**FULL)
    out = os.path.join(tmp, "out.docx")
    t.fill_docx(template, contract["values"], contract["blocks"], contract["drops"], out)
    first = docx.Document(out).paragraphs[0]
    assert first.runs[0].text == "An: " and first.runs[0].bold is True
    assert first.runs[1].text == "Beispiel Handel GmbH" and first.runs[1].bold is None


def test_fill_consumer_without_extras():
    texts, stats = _fill(
        verbraucher=True,
        anzahlung_prozent="",
        mandant_email="",
        pauschale_buchfuehrung_monat="",
        lohn_einrichtung="",
        pauschale_jahresabschluss="",
        buchungen_anzahl=0,
        erstes_geschaeftsjahr="",
    )
    joined = "\n".join(texts)
    assert "{{" not in joined and stats["remaining"] == []
    assert "Rechnung bei Zugang fällig." in texts and not any("Anzahlung von" in x for x in texts)
    assert "Widerrufsbelehrung (nur für Verbraucher)" in texts and "Sie können widerrufen." in texts
    assert "{{#Verbraucher}}" not in texts and "{{/Verbraucher}}" not in texts
    assert "Registrierung: einmalig EUR 250,00" in texts, "Registrierung bleibt immer"
    assert "Steuererklärungen: EUR 600,00 je Jahr" in texts
    assert not any(x.startswith(("Finanzbuchführung", "Lohnbuchführung", "Jahresabschluss")) for x in texts)
    assert "" not in [x for x in texts[:4]], "leere E-Mail-Zeile wurde entfernt"
    assert texts[1] == "Musterstraße 1, 12345 Musterstadt" and texts[2].startswith("Mandatsvertrag")


def test_fill_reports_unknown_placeholder():
    import docx

    tmp = tempfile.mkdtemp()
    template = os.path.join(tmp, "vorlage.docx")
    make_template(template)
    document = docx.Document(template)
    document.add_paragraph("Neu: {{Unbekannt}}")
    document.save(template)
    contract = t.build_contract(**FULL)
    out = os.path.join(tmp, "out.docx")
    stats = t.fill_docx(template, contract["values"], contract["blocks"], contract["drops"], out)
    assert stats["remaining"] == ["{{Unbekannt}}"]


def test_incomplete_block_is_an_error():
    import docx

    tmp = tempfile.mkdtemp()
    template = os.path.join(tmp, "vorlage.docx")
    document = docx.Document()
    document.add_paragraph("{{#Anzahlung}} ohne Ende")
    document.save(template)
    try:
        t.fill_docx(template, {}, {"Anzahlung": False}, [], os.path.join(tmp, "out.docx"))
    except ValueError as exc:
        assert "Anzahlung" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_safe_file_name():
    assert t.safe_file_name("Mandatsvertrag Beispiel 2026") == "Mandatsvertrag Beispiel 2026.docx"
    assert t.safe_file_name("../../x/Name GmbH & Co. KG") == "Name GmbH _ Co. KG.docx"
    assert t.safe_file_name("") == "Mandatsvertrag.docx"


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
