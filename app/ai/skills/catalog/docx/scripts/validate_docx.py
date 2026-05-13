#!/usr/bin/env python3
"""Validate the basic structure of a .docx file without external dependencies."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree


REQUIRED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/styles.xml",
    "word/_rels/document.xml.rels",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a .docx file created by this skill.")
    parser.add_argument("path", help="Path to the .docx file.")
    args = parser.parse_args()

    path = Path(args.path)
    errors: list[str] = []
    if not path.exists():
        errors.append(f"file not found: {path}")
    elif path.suffix.lower() != ".docx":
        errors.append("file extension must be .docx")
    elif not zipfile.is_zipfile(path):
        errors.append("file is not a valid zip archive")
    else:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            missing = sorted(REQUIRED_PARTS - names)
            if missing:
                errors.append(f"missing required parts: {', '.join(missing)}")
            for xml_part in sorted(name for name in names if name.endswith(".xml")):
                try:
                    ElementTree.fromstring(archive.read(xml_part))
                except ElementTree.ParseError as exc:
                    errors.append(f"invalid xml in {xml_part}: {exc}")
            if "word/document.xml" in names:
                document_xml = archive.read("word/document.xml")
                if b"<w:body" not in document_xml:
                    errors.append("word/document.xml does not contain a document body")

    result = {"ok": not errors, "path": str(path.resolve()), "errors": errors}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
