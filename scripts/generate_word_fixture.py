"""Generate a synthetic DOCX for preprocessing examples and tests."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
PROSE = "Synthetic field notes: the café opened at noon."
MIXED = """<w:p><w:r><w:t xml:space="preserve">Inventory: </w:t></w:r><w:r><w:t>north café</w:t></w:r></w:p>
<w:tbl>
  <w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>
  <w:tr><w:tc><w:p><w:r><w:t>Code</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Label</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>001</w:t></w:r></w:p></w:tc><w:tc><w:p/><w:p><w:r><w:t>café</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
<w:p><w:r><w:t>End</w:t><w:tab/><w:t>of notes.</w:t></w:r></w:p>"""


def write_fixture(path, body=MIXED, *, part="word/document.xml", word_ns=WORD, office_ns=OFFICE,
                  target=None, mode="Internal", content_type=DOCX, document=None, extra=()):
    parts = {
        "[Content_Types].xml": f'<Types xmlns="{PACKAGE}/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Override PartName="/{part}" ContentType="{content_type}"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{PACKAGE}/relationships">'
        f'<Relationship Id="rId1" Type="{office_ns}/officeDocument" Target="{target or part}" TargetMode="{mode}"/></Relationships>',
        part: document if document is not None else (
            f'<w:document xmlns:w="{word_ns}" xmlns:r="{office_ns}"><w:body>{body}<w:sectPr/></w:body></w:document>'
        ),
    }
    with ZipFile(path, "w") as archive:
        for name, xml in [*parts.items(), *extra]:
            archive.writestr(ZipInfo(name), xml, compress_type=ZIP_DEFLATED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--prose-only", action="store_true")
    args = parser.parse_args()
    write_fixture(args.output, f"<w:p><w:r><w:t>{PROSE}</w:t></w:r></w:p>" if args.prose_only else MIXED)
