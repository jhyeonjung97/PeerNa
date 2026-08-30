"""Provider-neutral manuscript content.

The loader produces these; each backend converts them into whatever shape its
API wants. Keeping the middle neutral means adding a provider does not mean
touching the parsing code, and adding a file format does not mean touching the
providers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    media_type: str
    data: bytes


@dataclass(frozen=True)
class PdfPart:
    filename: str
    data: bytes


Part = TextPart | ImagePart | PdfPart


def as_text_only(parts: list[Part]) -> list[Part]:
    """Strip the images, keeping the words.

    A pass about citations and framing does not need to see the rendered pages,
    and a 27-page PDF full of figures costs several times what its text does.
    That matters most on the web search pass, where search results pile onto a
    context that already holds the manuscript.
    """
    flattened: list[Part] = []
    for part in parts:
        if isinstance(part, TextPart):
            flattened.append(part)
        elif isinstance(part, PdfPart):
            flattened.append(TextPart(_pdf_text(part)))
        # Images are dropped; a caption-level mention already sits in the text.
    return flattened or list(parts)


def _pdf_text(part: PdfPart) -> str:
    import pymupdf

    with pymupdf.open(stream=part.data, filetype="pdf") as document:
        pages = [page.get_text() for page in document]
    body = "\n\n".join(f"[Page {n}]\n{t}" for n, t in enumerate(pages, 1) if t.strip())
    return body or f"[{part.filename}: no extractable text]"
