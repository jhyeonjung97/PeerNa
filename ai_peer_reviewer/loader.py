"""Turn a manuscript file into provider-neutral content parts.

The guiding rule: preserve the figures. A referee who cannot see the figures
cannot review the paper, and figures are exactly what text extraction destroys.
PDFs are therefore sent whole, as a native document block, so the model sees the
rendered pages. Formats with no native support are converted to text and their
images are re-attached separately.
"""

from __future__ import annotations


import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import ModelSpec
from .parts import ImagePart, Part, PdfPart, TextPart

#: The API rejects requests over 32 MB. Leave headroom for the prompt itself.
MAX_REQUEST_BYTES = 30 * 1024 * 1024

#: Attaching every figure from a long paper is rarely worth the tokens.
MAX_ATTACHED_IMAGES = 20

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass
class Manuscript:
    path: Path
    parts: list[Part]
    #: Human-readable notes about what was and wasn't preserved, shown to the
    #: user before the run so they know what the model is actually seeing.
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name


def load(path: Path, spec: ModelSpec) -> Manuscript:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _load_pdf(path, spec)
    if suffix == ".docx":
        return _load_docx(path)
    if suffix in (".tex", ".txt", ".md"):
        return _load_text(path)
    raise SystemExit(
        f"Unsupported file type {suffix!r}. Supported: .pdf, .docx, .tex, .txt, .md"
    )


def _load_pdf(path: Path, spec: ModelSpec) -> Manuscript:
    import pymupdf

    data = path.read_bytes()
    notes: list[str] = []

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = doc.page_count

    if pages > spec.max_pdf_pages:
        raise SystemExit(
            f"{path.name} has {pages} pages; {spec.label} accepts at most "
            f"{spec.max_pdf_pages} in one request. Split it, or use a "
            f"model with a bigger context window."
        )

    # Base64 inflates by ~4/3.
    if len(data) * 4 / 3 > MAX_REQUEST_BYTES:
        raise SystemExit(
            f"{path.name} is {len(data) / 1e6:.1f} MB, too large to send in one "
            "request. Compress the PDF (downsample images) and retry."
        )

    notes.append(f"{pages} pages sent as rendered PDF — figures and layout preserved.")

    return Manuscript(
        path=path,
        parts=[PdfPart(filename=path.name, data=data)],
        notes=notes,
    )


def _load_docx(path: Path) -> Manuscript:
    """Walk the document body in order so each figure lands where it appears.

    Dumping the text and then a pile of images loses the figure-to-caption
    binding, and a referee comment about "Figure 3" is worthless if the model
    had to guess which image that was. Interleaving costs nothing and keeps each
    image adjacent to the caption that names it.
    """
    import docx
    from docx.oxml.ns import qn

    document = docx.Document(str(path))
    notes = ["Converted from .docx; text and figures are interleaved in document order."]

    collected: list[Part] = []
    pending: list[str] = []
    image_count = 0
    skipped_images = 0

    def flush_text() -> None:
        joined = "\n\n".join(chunk for chunk in pending if chunk.strip())
        if joined.strip():
            collected.append(TextPart(joined))
        pending.clear()

    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]

        if tag == "tbl":
            pending.append(_render_table(child, qn))
            continue
        if tag != "p":
            continue

        for blip in child.findall(".//" + qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            if not rid:
                continue
            image = document.part.related_parts.get(rid)
            media_type = getattr(image, "content_type", None) if image else None
            if media_type not in IMAGE_MEDIA_TYPES.values():
                continue
            if image_count >= MAX_ATTACHED_IMAGES:
                skipped_images += 1
                continue
            image_count += 1
            flush_text()
            collected.append(TextPart(f"[Figure {image_count}, as placed]"))
            collected.append(ImagePart(media_type=media_type, data=image.blob))

        text = "".join(node.text or "" for node in child.iter(qn("w:t")))
        if text.strip():
            pending.append(text)

    flush_text()

    if not collected:
        raise SystemExit(f"{path.name} appears to be empty.")

    if image_count:
        note = f"{image_count} figure(s) attached in place."
        if skipped_images:
            note += f" {skipped_images} beyond the limit were skipped."
        notes.append(note)
    else:
        notes.append(
            "No embedded images found — figure comments will be unreliable. "
            "If the figures are linked rather than embedded, export to PDF instead."
        )

    return Manuscript(path=path, parts=collected, notes=notes)


def _render_table(element, qn) -> str:
    rows = []
    for row in element.findall(".//" + qn("w:tr")):
        cells = [
            "".join(node.text or "" for node in cell.iter(qn("w:t"))).strip()
            for cell in row.findall(".//" + qn("w:tc"))
        ]
        if any(cells):
            rows.append(" | ".join(cells))
    return "[TABLE]\n" + "\n".join(rows) if rows else ""


def _load_text(path: Path) -> Manuscript:
    text = path.read_text(encoding="utf-8", errors="replace")
    notes: list[str] = []
    collected: list[Part] = [TextPart(text)]

    if path.suffix.lower() == ".tex":
        figures = _resolve_tex_figures(path, text)
        if figures:
            for media_type, data, name in figures:
                collected.append(TextPart(f"[Figure file: {name}]"))
                collected.append(ImagePart(media_type=media_type, data=data))
            notes.append(
                f"{len(figures)} figure file(s) found next to the .tex and attached."
            )
        else:
            notes.append(
                "No figure files resolved from \\includegraphics. "
                "Figure comments will be unreliable — compile to PDF and review that instead."
            )

    return Manuscript(path=path, parts=collected, notes=notes)


def _resolve_tex_figures(path: Path, text: str) -> list[tuple[str, bytes, str]]:
    """Find \\includegraphics targets and load the ones that exist on disk."""
    refs = re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", text)
    root = path.parent
    found: list[tuple[str, bytes, str]] = []
    seen: set[Path] = set()

    for ref in refs:
        if len(found) >= MAX_ATTACHED_IMAGES:
            break
        candidate = Path(ref)
        # LaTeX conventionally omits the extension.
        options = (
            [candidate]
            if candidate.suffix
            else [candidate.with_suffix(ext) for ext in IMAGE_MEDIA_TYPES]
        )
        for option in options:
            resolved = (root / option).resolve()
            if resolved in seen or not resolved.is_file():
                continue
            media_type = IMAGE_MEDIA_TYPES.get(resolved.suffix.lower())
            if not media_type:
                continue
            seen.add(resolved)
            found.append((media_type, resolved.read_bytes(), resolved.name))
            break

    return found
