"""Turn a finished report into a PDF or a Word file.

Neither goes through LibreOffice's HTML importer, which understands roughly
HTML 3.2 and renders anything using CSS variables or flexbox as a wall of
left-aligned text. PDFs are printed by headless Chrome, which renders exactly
what the browser shows. Word files are built from the report structure directly
with python-docx, so the styling is real Word styling rather than a translation
of a translation.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .render import RECOMMENDATION_LABELS, STATUS_LABELS, AI_DISCLOSURE, _coverage_line
from .schema import RefereeReport

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def _chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def to_pdf(html_text: str) -> bytes | None:
    """Print the report with a headless browser, falling back to LibreOffice."""
    browser = _chrome()
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / "report.html"
        source.write_text(html_text, encoding="utf-8")
        target = Path(work) / "report.pdf"

        if browser:
            try:
                subprocess.run(
                    [browser, "--headless", "--disable-gpu", "--no-sandbox",
                     "--no-pdf-header-footer", f"--print-to-pdf={target}",
                     source.as_uri()],
                    capture_output=True, timeout=180, check=True,
                )
                if target.is_file() and target.stat().st_size > 0:
                    return target.read_bytes()
            except (subprocess.SubprocessError, OSError):
                pass

        from .fetch import _soffice

        binary = _soffice()
        if not binary:
            return None
        try:
            subprocess.run(
                [binary, "--headless", "--convert-to", "pdf", "--outdir", work, str(source)],
                capture_output=True, timeout=180, check=True,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return target.read_bytes() if target.is_file() else None


#: A Word watermark is a VML shape parked in the page header, which is what puts
#: it behind the text on every page. python-docx has no API for this, so the
#: shape is written as XML — the same markup Word itself produces for its
#: built-in text watermarks.
WATERMARK_XML = """<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:r>
    <w:pict xmlns:v="urn:schemas-microsoft-com:vml"
            xmlns:o="urn:schemas-microsoft-com:office:office">
      <v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800"
                   path="m@7,l@8,m@5,21600l@6,21600e">
        <v:formulas>
          <v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/>
          <v:f eqn="sum 21600 0 @1"/><v:f eqn="sum 0 0 @2"/>
          <v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/>
          <v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/>
          <v:f eqn="if @0 @4 21600"/><v:f eqn="mid @5 @6"/>
          <v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/>
          <v:f eqn="mid @6 @7"/><v:f eqn="sum @6 0 @5"/>
        </v:formulas>
        <v:path textpathok="t" o:connecttype="custom"
                o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800"/>
        <v:textpath on="t" fitshape="t"/>
      </v:shapetype>
      <v:shape id="PeerNaWatermark" type="#_x0000_t136"
               style="position:absolute;margin-left:0;margin-top:0;width:400pt;height:130pt;
                      rotation:315;z-index:-251654144;mso-position-horizontal:center;
                      mso-position-horizontal-relative:margin;mso-position-vertical:center;
                      mso-position-vertical-relative:margin"
               fillcolor="#d8d2ca" stroked="f">
        <v:textpath style="font-family:&quot;Calibri&quot;;font-size:1pt" string="PeerNa"/>
      </v:shape>
    </w:pict>
  </w:r>
</w:p>"""


def _watermark(document) -> None:
    """Put the PeerNa mark behind every page."""
    from docx.oxml import parse_xml
    from docx.shared import Pt

    for section in document.sections:
        section.header.is_linked_to_previous = False
        header = section.header
        header._element.append(parse_xml(WATERMARK_XML))
        # The header's own empty paragraph would otherwise push the text down.
        for paragraph in header.paragraphs:
            paragraph.paragraph_format.space_after = Pt(0)


def to_docx(report: RefereeReport, manuscript_name: str, model_label: str,
            warnings: list[str] | None = None) -> bytes | None:
    """Build a Word document from the report itself, not from its HTML."""
    try:
        import docx
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        return None

    document = docx.Document()

    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(8)

    _watermark(document)

    mark = document.add_paragraph()
    mark_run = mark.add_run("PeerNa")
    mark_run.font.size = Pt(9)
    mark_run.font.color.rgb = RGBColor(0x8A, 0x82, 0x7A)
    mark.paragraph_format.space_after = Pt(2)

    heading = document.add_paragraph()
    run = heading.add_run(manuscript_name)
    run.bold = True
    run.font.size = Pt(14)

    verdict = document.add_paragraph()
    verdict.add_run("Recommendation: ").bold = True
    call = verdict.add_run(
        RECOMMENDATION_LABELS.get(report.recommendation, report.recommendation)
    )
    call.bold = True
    call.font.color.rgb = RGBColor(0xA0, 0x2C, 0x2C)

    document.add_paragraph(report.general_comments.strip())

    for index, comment in enumerate(report.comments, start=1):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Pt(18)
        paragraph.paragraph_format.first_line_indent = Pt(-18)
        paragraph.add_run(f"{index}. ").bold = True
        paragraph.add_run(comment.text.strip())

    document.add_page_break()

    citations = document.add_paragraph()
    citations.add_run("Citations checked").bold = True

    note = document.add_paragraph(_coverage_line(report))
    note.runs[0].italic = True
    note.runs[0].font.size = Pt(9.5)

    for check in report.citation_checks:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Pt(18)
        label = paragraph.add_run(
            f"[{STATUS_LABELS.get(check.status, check.status)}] "
        )
        label.bold = True
        if check.status != "verified":
            label.font.color.rgb = RGBColor(0xA0, 0x2C, 0x2C)
        paragraph.add_run(f"{check.reference} — {check.location}")
        if check.note.strip():
            detail = document.add_paragraph(check.note.strip())
            detail.paragraph_format.left_indent = Pt(36)
            detail.runs[0].font.size = Pt(10)

    for line in (f"Reviewer confidence: {report.confidence}.", ""):
        if line:
            document.add_paragraph(line).runs[0].italic = True

    for warning in dict.fromkeys(warnings or []):
        entry = document.add_paragraph(warning)
        entry.runs[0].font.size = Pt(9.5)
        entry.runs[0].font.color.rgb = RGBColor(0xA0, 0x2C, 0x2C)

    footer = document.add_paragraph(f"{AI_DISCLOSURE} Model: {model_label}.")
    footer.runs[0].font.size = Pt(9)
    footer.runs[0].font.color.rgb = RGBColor(0x6B, 0x65, 0x60)
    footer.alignment = WD_ALIGN_PARAGRAPH.LEFT

    buffer = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    buffer.close()
    try:
        document.save(buffer.name)
        return Path(buffer.name).read_bytes()
    finally:
        Path(buffer.name).unlink(missing_ok=True)
