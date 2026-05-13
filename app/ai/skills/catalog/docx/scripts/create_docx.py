#!/usr/bin/env python3
"""Create a simple Word .docx file without external dependencies."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a simple .docx document.")
    parser.add_argument("--output", default="document.docx", help="Output .docx path.")
    parser.add_argument("--title", required=True, help="Document title.")
    parser.add_argument("--subtitle", default="", help="Optional subtitle.")
    parser.add_argument(
        "--section",
        action="append",
        default=[],
        help="Repeatable section in 'heading::body' format.",
    )
    parser.add_argument(
        "--sections-json",
        default="",
        help="JSON array of objects with heading and body fields.",
    )
    args = parser.parse_args()

    output = Path(args.output)
    if output.suffix.lower() != ".docx":
        raise SystemExit("--output must end with .docx")
    output.parent.mkdir(parents=True, exist_ok=True)

    sections = _parse_sections(args.section, args.sections_json)
    document_xml = _document_xml(title=args.title, subtitle=args.subtitle, sections=sections)

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        docx.writestr("_rels/.rels", RELS_XML)
        docx.writestr("docProps/core.xml", _core_xml(args.title))
        docx.writestr("docProps/app.xml", APP_XML)
        docx.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS_XML)
        docx.writestr("word/styles.xml", STYLES_XML)
        docx.writestr("word/document.xml", document_xml)

    print(json.dumps({"ok": True, "output": str(output.resolve()), "sections": len(sections)}, ensure_ascii=False))
    return 0


def _parse_sections(raw_sections: list[str], sections_json: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for raw_section in raw_sections:
        heading, separator, body = raw_section.partition("::")
        if not separator:
            heading, body = "Section", raw_section
        sections.append((heading.strip() or "Section", body.strip()))

    if sections_json:
        payload = json.loads(sections_json)
        if not isinstance(payload, list):
            raise SystemExit("--sections-json must be a JSON array")
        for item in payload:
            if not isinstance(item, dict):
                raise SystemExit("--sections-json items must be objects")
            sections.append((str(item.get("heading") or "Section"), str(item.get("body") or "")))

    if not sections:
        sections.append(("Overview", ""))
    return sections


def _document_xml(*, title: str, subtitle: str, sections: list[tuple[str, str]]) -> str:
    paragraphs = [_paragraph(title, style="Title")]
    if subtitle:
        paragraphs.append(_paragraph(subtitle, style="Subtitle"))
    for heading, body in sections:
        paragraphs.append(_paragraph(heading, style="Heading1"))
        for line in body.splitlines() or [""]:
            paragraphs.append(_paragraph(line))
    paragraphs.append(SECTION_PROPERTIES_XML)
    return XML_HEADER + (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(paragraphs)}</w:body></w:document>"
    )


def _paragraph(text: str, *, style: str | None = None) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{escape(style)}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _core_xml(title: str) -> str:
    timestamp = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return XML_HEADER + f"""<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <dc:title>{escape(title)}</dc:title>
 <dc:creator>exile-agent</dc:creator>
 <cp:lastModifiedBy>exile-agent</cp:lastModifiedBy>
 <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
 <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>"""


XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

CONTENT_TYPES_XML = XML_HEADER + """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
 <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
 <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
 <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

RELS_XML = XML_HEADER + """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

DOCUMENT_RELS_XML = XML_HEADER + """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

APP_XML = XML_HEADER + """<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
 <Application>exile-agent</Application>
</Properties>"""

STYLES_XML = XML_HEADER + """<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
  <w:name w:val="Normal"/>
  <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="24"/></w:rPr>
 </w:style>
 <w:style w:type="paragraph" w:styleId="Title">
  <w:name w:val="Title"/>
  <w:basedOn w:val="Normal"/>
  <w:rPr><w:b/><w:sz w:val="40"/></w:rPr>
 </w:style>
 <w:style w:type="paragraph" w:styleId="Subtitle">
  <w:name w:val="Subtitle"/>
  <w:basedOn w:val="Normal"/>
  <w:rPr><w:i/><w:sz w:val="28"/></w:rPr>
 </w:style>
 <w:style w:type="paragraph" w:styleId="Heading1">
  <w:name w:val="heading 1"/>
  <w:basedOn w:val="Normal"/>
  <w:uiPriority w:val="9"/>
  <w:qFormat/>
  <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
  <w:rPr><w:b/><w:sz w:val="32"/></w:rPr>
 </w:style>
</w:styles>"""

SECTION_PROPERTIES_XML = """<w:sectPr>
 <w:pgSz w:w="12240" w:h="15840"/>
 <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
</w:sectPr>"""


if __name__ == "__main__":
    raise SystemExit(main())
