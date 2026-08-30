"""Collect Nature Communications papers together with their referee reports.

Nature Communications publishes the referee reports for papers whose authors
opted into transparent peer review, which makes it possible to compare what this
tool says about a paper against what the actual reviewers said.

The route matters. Scraping nature.com's article pages at volume is against
their terms and gets blocked; instead this goes through Europe PMC, which
distributes the open-access corpus for exactly this purpose. Europe PMC's JATS
full text labels each supplementary file, so the referee report can be picked
out by its label — `Transparent Peer Review file` — rather than by guessing at
filenames or downloading the whole supplementary bundle, which routinely runs to
hundreds of megabytes for a single paper.

Article PDFs come from Europe PMC's own rendering, not from nature.com, which
serves a bot challenge to automated PDF requests. The Europe PMC copy is
byte-identical to the publisher's and keeps the figures, which is most of what a
referee actually looks at. If a paper has no rendered PDF, this falls back to
flattened JATS text, where figures survive only as captions.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EPMC_WEB = "https://europepmc.org"
SPRINGER_ESM = "https://static-content.springer.com/esm"

#: Europe PMC asks that automated clients identify themselves.
USER_AGENT = "ai-peer-reviewer/0.1 (academic research; +https://www.nature.com/ncomms)"

#: Publishers occasionally post supplementary information as a Word file. The
#: corpus is meant to be PDFs, so those are converted on arrival — LibreOffice
#: does it headlessly, without touching whatever the user has open in Word.
SOFFICE_CANDIDATES = (
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)

#: Seconds between requests. Deliberately unhurried — this is a background
#: harvest, and being a good citizen is what keeps the route open.
DELAY_SECONDS = 1.0

#: How the files worth keeping are labelled in the JATS. Everything else in a
#: paper's supplementary bundle — movies, raw data, source files — is skipped,
#: which is what keeps this from being a terabyte-scale download.
WANTED = {
    "review": re.compile(r"peer\s*review", re.I),
    # Nature Communications labels the same thing at least four ways:
    # "Supplementary Information", "Supporting Information", "Supplementary
    # Material", "Supplementary file". The trailing (?!s) is what keeps
    # "Description of Additional Supplementary Files" — an index of the other
    # attachments, not the SI itself — from matching.
    "si": re.compile(
        r"supp(?:lementary|orting)\s+(?:information|material|file)\b(?!s)", re.I
    ),
}

#: Extensions accepted per kind. Reports are always PDFs; SI is usually a PDF
#: but is occasionally a Word file. Everything else in the bundle — movies,
#: spreadsheets, archives — is deliberately skipped.
ACCEPTED = {"review": (".pdf",), "si": (".pdf", ".docx")}


@dataclass
class Paper:
    pmcid: str
    doi: str
    title: str
    year: str
    article_path: Path | None = None
    review_path: Path | None = None
    si_path: Path | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.doi.rsplit("/", 1)[-1] if self.doi else self.pmcid


def _get(url: str, timeout: int = 90) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def search(query: str = "", limit: int = 10, year: str | None = None) -> list[Paper]:
    """Find open-access Nature Communications papers matching a topic."""
    terms = ['JOURNAL:"Nature communications"', "OPEN_ACCESS:Y", "HAS_SUPPL:Y"]
    if query:
        terms.append(f"({query})")
    if year:
        terms.append(f"PUB_YEAR:{year}")

    papers: list[Paper] = []
    cursor = "*"
    while len(papers) < limit:
        params = urllib.parse.urlencode(
            {
                "query": " AND ".join(terms),
                "format": "json",
                "pageSize": min(100, limit - len(papers)),
                "cursorMark": cursor,
                "resultType": "lite",
            }
        )
        payload = json.loads(_get(f"{EPMC}/search?{params}").decode())
        results = payload.get("resultList", {}).get("result", [])
        if not results:
            break
        for item in results:
            if not item.get("pmcid"):
                continue
            papers.append(
                Paper(
                    pmcid=item["pmcid"],
                    doi=item.get("doi", ""),
                    title=item.get("title", ""),
                    year=str(item.get("pubYear", "")),
                )
            )
        next_cursor = payload.get("nextCursorMark")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(DELAY_SECONDS)

    return papers[:limit]


def find_supplementary(pmcid: str) -> dict[str, str]:
    """Map each wanted kind to its supplementary filename.

    The JATS labels every supplementary file, so this reads the label rather
    than guessing from the filename — `MOESM6_ESM.pdf` means nothing on its own,
    and its number differs from paper to paper. Only PDFs are considered, which
    excludes the movies and spreadsheets that make the full bundle enormous.
    """
    xml = _get(f"{EPMC}/{pmcid}/fullTextXML").decode("utf-8", "replace")
    found: dict[str, str] = {}
    for block in re.finditer(
        r"<supplementary-material.*?</supplementary-material>", xml, re.S
    ):
        chunk = block.group(0)
        href = re.search(r'xlink:href="([^"]+)"', chunk)
        if not href:
            continue
        filename = href.group(1)
        label = re.sub(r"<[^>]+>", " ", chunk)
        for kind, pattern in WANTED.items():
            if kind in found or not pattern.search(label):
                continue
            if filename.lower().endswith(ACCEPTED[kind]):
                found[kind] = filename
    return found


def download(
    paper: Paper,
    out_dir: Path,
    want_article: bool = True,
    want_review: bool = True,
    want_si: bool = False,
    overwrite: bool = False,
) -> Paper:
    """Fetch what is missing for this paper.

    Files already on disk are left alone, so an interrupted harvest can be
    restarted without re-downloading the hundreds of megabytes it already has.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    def already(suffix: str) -> Path | None:
        candidate = out_dir / f"{paper.slug}.{suffix}"
        return candidate if candidate.is_file() and candidate.stat().st_size > 0 else None

    if not overwrite:
        for suffix, attr in (("pdf", "article_path"), ("review.pdf", "review_path"),
                             ("si.pdf", "si_path"), ("si.docx", "si_path")):
            existing = already(suffix)
            if existing:
                setattr(paper, attr, existing)
        if paper.article_path and (paper.review_path or not want_review) and (
            paper.si_path or not want_si
        ):
            paper.problems.append("already downloaded")
            return paper
        want_review = want_review and not paper.review_path
        want_si = want_si and not paper.si_path
        want_article = want_article and not paper.article_path

    if want_review or want_si:
        try:
            available = find_supplementary(paper.pmcid)
        except Exception as exc:
            available = {}
            paper.problems.append(f"supplementary index: {type(exc).__name__}: {exc}")

        quoted = urllib.parse.quote(f"art:{paper.doi}", safe="")
        for kind, enabled, stem, attr in (
            ("review", want_review, "review", "review_path"),
            ("si", want_si, "si", "si_path"),
        ):
            if not enabled:
                continue
            filename = available.get(kind)
            if not filename:
                paper.problems.append(f"no {kind} file published")
                continue
            try:
                ext = Path(filename).suffix.lower() or ".pdf"
                target = out_dir / f"{paper.slug}.{stem}{ext}"
                target.write_bytes(
                    _get(f"{SPRINGER_ESM}/{quoted}/MediaObjects/{filename}", timeout=300)
                )
                if ext == ".docx":
                    converted = to_pdf(target)
                    if converted:
                        target = converted
                    else:
                        paper.problems.append(
                            f"{kind}: kept as .docx — install LibreOffice "
                            "(brew install --cask libreoffice) to convert"
                        )
                setattr(paper, attr, target)
            except Exception as exc:
                paper.problems.append(f"{kind}: {type(exc).__name__}: {exc}")
            time.sleep(DELAY_SECONDS)

    if want_article:
        try:
            paper.article_path = _fetch_article(paper, out_dir)
        except Exception as exc:
            paper.problems.append(f"article: {type(exc).__name__}: {exc}")
        time.sleep(DELAY_SECONDS)

    return paper


def _fetch_article(paper: Paper, out_dir: Path) -> Path:
    """Prefer the rendered PDF; fall back to JATS text if it is unavailable.

    Europe PMC renders the publisher PDF for open-access articles, which keeps
    the figures — and figures are most of what a referee looks at. Only if that
    is missing does this fall back to flattened JATS text, where figures survive
    as captions alone.
    """
    try:
        data = _get(f"{EPMC_WEB}/articles/{paper.pmcid}?pdf=render", timeout=180)
        if data[:5] == b"%PDF-":
            target = out_dir / f"{paper.slug}.pdf"
            target.write_bytes(data)
            return target
        raise ValueError("not a PDF")
    except Exception as exc:
        paper.problems.append(f"no rendered PDF ({exc}); saved JATS text instead")

    xml = _get(f"{EPMC}/{paper.pmcid}/fullTextXML").decode("utf-8", "replace")
    target = out_dir / f"{paper.slug}.txt"
    target.write_text(jats_to_text(xml), encoding="utf-8")
    return target


def _soffice() -> str | None:
    for candidate in SOFFICE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("soffice")


def to_pdf(path: Path) -> Path | None:
    """Convert a Word document to PDF beside it, returning the new path.

    Returns None — leaving the original in place — if LibreOffice is missing or
    the conversion fails. A corpus with one .docx in it is better than a corpus
    with a hole where a file should be.
    """
    binary = _soffice()
    if not binary:
        return None
    target = path.with_suffix(".pdf")
    try:
        subprocess.run(
            [binary, "--headless", "--convert-to", "pdf",
             "--outdir", str(path.parent), str(path)],
            capture_output=True, timeout=300, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if target.is_file() and target.stat().st_size > 0:
        path.unlink(missing_ok=True)
        return target
    return None


def jats_to_text(xml: str) -> str:
    """Flatten JATS full text into something the reviewer can read.

    Figures survive only as their captions — the images live in the
    supplementary bundle, which is often hundreds of megabytes per paper. The
    figures pass will say it cannot see them, which is the honest outcome.
    """
    body = re.search(r"<body\b.*?</body>", xml, re.S)
    title = re.search(r"<article-title[^>]*>(.*?)</article-title>", xml, re.S)
    abstract = re.search(r"<abstract\b.*?</abstract>", xml, re.S)

    chunks: list[str] = []
    if title:
        chunks.append(_flatten(title.group(1)))
    if abstract:
        chunks.append("ABSTRACT\n" + _flatten(abstract.group(0)))
    if body:
        chunks.append(_flatten(body.group(0)))

    if not chunks:
        raise ValueError("No article body in the JATS response.")
    return "\n\n".join(chunks)


def _flatten(fragment: str) -> str:
    # Section and paragraph boundaries become blank lines; figure and table
    # captions are kept and marked so the model knows what it is missing.
    text = re.sub(r"<title[^>]*>(.*?)</title>", r"\n\n## \1\n", fragment, flags=re.S)
    text = re.sub(
        r"<caption[^>]*>(.*?)</caption>", r"\n[CAPTION] \1\n", text, flags=re.S
    )
    text = re.sub(r"</(p|sec|abstract|fig|table-wrap)>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="ai-peer-review-fetch",
        description="Download Nature Communications papers and their referee reports.",
    )
    parser.add_argument("query", nargs="?", default="", help='e.g. "electrocatalysis"')
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--year", help="Restrict to a publication year")
    parser.add_argument("--out", type=Path, default=Path("corpus"))
    parser.add_argument(
        "--reviews-only",
        action="store_true",
        help="Fetch only the referee reports, not the article",
    )
    parser.add_argument(
        "--si",
        action="store_true",
        help="Also fetch the Supplementary Information PDF (roughly 14 MB per "
        "paper; movies and raw data are never fetched)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download files that are already present",
    )
    parser.add_argument(
        "--list", action="store_true", help="Show what matches without downloading"
    )
    args = parser.parse_args(argv)

    papers = search(args.query, limit=args.limit, year=args.year)
    if not papers:
        print("Nothing matched.", file=sys.stderr)
        return 1

    print(f"{len(papers)} paper(s) matched.\n", file=sys.stderr)
    if args.list:
        for paper in papers:
            print(f"{paper.year}  {paper.doi}  {paper.title[:70]}")
        return 0

    got_review = got_si = 0
    for index, paper in enumerate(papers, start=1):
        print(f"[{index}/{len(papers)}] {paper.slug} — {paper.title[:60]}", file=sys.stderr)
        download(
            paper,
            args.out,
            want_article=not args.reviews_only,
            want_si=args.si,
            overwrite=args.overwrite,
        )
        if paper.review_path:
            got_review += 1
        if paper.si_path:
            got_si += 1
        for problem in paper.problems:
            marker = "·" if problem == "already downloaded" else "!"
            print(f"    {marker} {problem}", file=sys.stderr)

    summary = f"\n{got_review}/{len(papers)} had a published referee report."
    if args.si:
        summary += f" {got_si}/{len(papers)} had Supplementary Information."
    print(f"{summary} Saved to {args.out}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
