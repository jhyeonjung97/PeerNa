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
    """Print the report with a headless browser, falling back to LibreOffice,
    then to PyMuPDF's own HTML layout.

    The last is what the deployed container uses: it has neither browser nor
    LibreOffice, and installing Chromium would add hundreds of megabytes to an
    instance with 512 MB of memory. PyMuPDF is already a dependency. Its CSS is
    simpler than a browser's, which costs some typography and nothing else.
    """
    printed = _print_pdf(html_text)
    data = printed if printed is not None else _layout_pdf(html_text)
    return _number_pages(data) if data else None


def _number_pages(data: bytes) -> bytes:
    """"n / N" at the foot of every page, whichever route made the PDF."""
    try:
        import pymupdf

        document = pymupdf.open("pdf", data)
        total = document.page_count
        for number, page in enumerate(document, 1):
            label = f"{number} / {total}"
            size = 8
            width = pymupdf.get_text_length(label, fontname="helv", fontsize=size)
            page.insert_text(((page.rect.width - width) / 2, page.rect.height - 28),
                             label, fontname="helv", fontsize=size, color=(0.45, 0.45, 0.45))
        return document.tobytes(garbage=3, deflate=True)
    except Exception:  # noqa: BLE001 - an unnumbered PDF beats no PDF
        return data


PRINT_SPACING = (
    "<style>"
    "li { margin-bottom: 4pt; }"
    "ol.comments li { margin-bottom: 9pt; }"
    "ol.comments { margin-bottom: 18pt; }"
    "li.citation { margin-bottom: 8pt; }"
    "h2 { margin-top: 16pt; margin-bottom: 6pt; }"
    "p { margin-bottom: 6pt; }"
    ".provenance { margin-top: 8pt; }"
    "</style>"
)


def _layout_pdf(html_text: str) -> bytes | None:
    import io

    try:
        import pymupdf

        from .render import PAGE_BREAK, WATERMARK_PRINT_INK, _logo

        # Each piece after a break starts a fresh page. The pieces share the
        # document head, so the styles carry over; the parser closes whatever
        # tags a cut leaves open.
        # The stylesheet spaces things in rem, which this layout does not size;
        # the same spacing again in points, for it alone.
        html_text = html_text.replace("</head>", PRINT_SPACING + "</head>", 1)
        head = html_text[: html_text.find("<main>") + len("<main>")] if "<main>" in html_text else ""
        pieces = html_text.split(PAGE_BREAK)
        pieces = [pieces[0]] + [head + piece for piece in pieces[1:]]

        buffer = io.BytesIO()
        writer = pymupdf.DocumentWriter(buffer)
        page = pymupdf.paper_rect("a4")
        body = page + (54, 54, -54, -54)  # three-quarter-inch margins
        for piece in pieces:
            story = pymupdf.Story(html=piece)
            more = True
            while more:
                device = writer.begin_page(page)
                more, _ = story.place(body)
                story.draw(device)
                writer.end_page()
        writer.close()

        # The CSS watermark is a background image, which this layout does not
        # draw, so the mark goes behind each page afterwards.
        document = pymupdf.open("pdf", buffer.getvalue())
        mark = pymupdf.open(stream=_logo(WATERMARK_PRINT_INK).encode(), filetype="svg")
        mark_pdf = pymupdf.open("pdf", mark.convert_to_pdf())
        side = 7 / 2.54 * 72  # 7 cm, as in the print stylesheet
        for p in document:
            centre = p.rect.width / 2, p.rect.height / 2
            box = pymupdf.Rect(centre[0] - side / 2, centre[1] - side / 2,
                               centre[0] + side / 2, centre[1] + side / 2)
            p.show_pdf_page(box, mark_pdf, 0, overlay=False)
        data = document.tobytes(garbage=3, deflate=True)
        return data or None
    except Exception:  # noqa: BLE001 - a failed export must not fail the request
        return None


def _print_pdf(html_text: str) -> bytes | None:
    """Chrome headless if present, else LibreOffice; None if neither is."""
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


def _page_numbers(document) -> None:
    """A centred PAGE field in each footer. python-docx has no API for fields."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    for section in document.sections:
        paragraph = section.footer.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run()
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x73, 0x73, 0x73)
        for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
            if kind:
                element = OxmlElement("w:fldChar")
                element.set(qn("w:fldCharType"), kind)
            else:
                element = OxmlElement("w:instrText")
                element.set(qn("xml:space"), "preserve")
                element.text = text
            run._r.append(element)


#: Ink for the Word watermark. Word draws the picture opaque, so the faintness is
#: in the colour: about 10% of the mark's grey over white, a shade darker than
#: the PDF's because Word's page renders lighter on screen.
WORD_WATERMARK_INK = "#efeeec"


def _watermark(document) -> None:
    """Put the PeerNa mark behind the text of every page.

    It used to be a VML text watermark, the markup Word writes for its own. Word
    for Windows draws those; Word for Mac, Pages and Google Docs mostly do not,
    so the mark was missing for most readers, and it was the word "PeerNa" where
    the PDF has the logo. Now it is the logo as a picture, anchored behind the
    text in the header in DrawingML, which every current reader draws.
    """
    import io

    import pymupdf
    from docx.oxml import parse_xml
    from docx.shared import Cm, Pt

    from .render import _logo

    svg = pymupdf.open(stream=_logo(WORD_WATERMARK_INK).encode(), filetype="svg")
    png = svg[0].get_pixmap(dpi=300, alpha=True).tobytes("png")

    for section in document.sections:
        section.header.is_linked_to_previous = False
        paragraph = section.header.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(0)
        run = paragraph.add_run()
        run.add_picture(io.BytesIO(png), width=Cm(7))
        inline = run._r.find(".//{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}inline")
        if inline is None:
            continue
        # Turn the inline picture into one anchored to the page centre, behind
        # the text, taking no space: what Word calls "Behind Text".
        extent = inline.find("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent")
        cx, cy = extent.get("cx"), extent.get("cy")
        graphic = inline.find("{http://schemas.openxmlformats.org/drawingml/2006/main}graphic")
        doc_pr = inline.find("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr")
        anchor = parse_xml(
            '<wp:anchor xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="0" '
            'behindDoc="1" locked="1" layoutInCell="1" allowOverlap="1">'
            '<wp:simplePos x="0" y="0"/>'
            '<wp:positionH relativeFrom="page"><wp:align>center</wp:align></wp:positionH>'
            '<wp:positionV relativeFrom="page"><wp:align>center</wp:align></wp:positionV>'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
            '<wp:wrapNone/></wp:anchor>'
        )
        anchor.append(doc_pr)
        anchor.append(parse_xml(
            '<wp:cNvGraphicFramePr xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"/>'))
        anchor.append(graphic)
        inline.getparent().replace(inline, anchor)


def to_docx(report: RefereeReport, manuscript_name: str, model_label: str,
            warnings: list[str] | None = None, web_search: bool | None = None,
            finished_at: float | None = None, sources: str = "") -> bytes | None:
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
    _page_numbers(document)

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

    # With web search off every check would read "unverified", and the line
    # at the foot already says search was off; see render._citations_were_searched.
    if web_search is not False:
        document.add_page_break()

        citations = document.add_paragraph()
        citations.add_run("Citations checked").bold = True

        note = document.add_paragraph(_coverage_line(report))
        note.runs[0].italic = True
        note.runs[0].font.size = Pt(9.5)

        for check in report.citation_checks:
            paragraph = document.add_paragraph(style="List Bullet")
            label = paragraph.add_run(
                f"[{STATUS_LABELS.get(check.status, check.status)}] "
            )
            label.bold = True
            if check.status != "verified":
                label.font.color.rgb = RGBColor(0xA0, 0x2C, 0x2C)
            paragraph.add_run(f"{check.reference} — {check.location}")
            if check.note.strip():
                detail = document.add_paragraph(check.note.strip())
                detail.paragraph_format.left_indent = Pt(36)  # under the bullet's text
                detail.runs[0].font.size = Pt(10)

    if warnings:
        document.add_paragraph().add_run("Run warnings").bold = True
    for warning in dict.fromkeys(warnings or []):
        entry = document.add_paragraph(warning)
        entry.runs[0].font.size = Pt(9.5)
        entry.runs[0].font.color.rgb = RGBColor(0xA0, 0x2C, 0x2C)

    document.add_paragraph(f"Reviewer confidence: {report.confidence}.").runs[0].italic = True

    from .render import provenance

    # Two paragraphs, not one: run together the disclosure and the provenance
    # read as a single sentence.
    for text, before in ((AI_DISCLOSURE, None), (provenance(model_label, web_search, finished_at), Pt(6))):
        footer = document.add_paragraph(text)
        footer.runs[0].font.size = Pt(9)
        footer.runs[0].font.color.rgb = RGBColor(0x6B, 0x65, 0x60)
        footer.alignment = WD_ALIGN_PARAGRAPH.LEFT
        if before is not None:
            footer.paragraph_format.space_before = before

    # The corpus papers the precedents came from. The page and the PDF carried
    # this credit and Word did not, though CC BY asks for it wherever the
    # comments it shaped are passed on.
    from .render import PRECEDENT_CREDIT

    credits = [line.strip() for line in (sources or "").splitlines() if line.strip()]
    if credits:
        head = document.add_paragraph(PRECEDENT_CREDIT)
        head.runs[0].font.size = Pt(9)
        head.runs[0].font.color.rgb = RGBColor(0x6B, 0x65, 0x60)
        head.paragraph_format.space_before = Pt(6)
        for line in credits:
            item = document.add_paragraph(line, style="List Bullet")
            item.runs[0].font.size = Pt(8.5)
            item.runs[0].font.color.rgb = RGBColor(0x6B, 0x65, 0x60)

    buffer = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    buffer.close()
    try:
        document.save(buffer.name)
        return Path(buffer.name).read_bytes()
    finally:
        Path(buffer.name).unlink(missing_ok=True)
