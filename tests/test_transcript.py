from pathlib import Path
from zipfile import ZipFile

from priv_sanitizer.transcript import hardening_intervals, parse


def test_docx_parser(tmp_path):
    source = tmp_path / "input.docx"
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>00:00:00</w:t></w:r></w:p>
    <w:p><w:r><w:t>Person Alpha: Welcome</w:t></w:r></w:p>
    <w:p><w:r><w:t>Person Beta: Person Alpha is joining</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    data = parse(source)
    assert data["format"] == "docx"
    assert data["names"] == ["Person Alpha", "Person Beta"]
    assert hardening_intervals(data, 30.0)


def test_srt_parser(tmp_path):
    source = tmp_path / "sample.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nPerson Alpha: Welcome\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nPerson Beta: Person Alpha is joining\n",
        encoding="utf-8",
    )
    data = parse(source)
    assert data["names"] == ["Person Alpha", "Person Beta"]
    assert len(hardening_intervals(data, 10.0)) == 1
