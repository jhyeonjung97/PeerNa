"""Collect referee reports at scale, keeping only the text.

The index needs three things per paper: a title, an abstract, and the reviewers'
comments. It does not need the article PDF, the supplementary information, or the
figures — so none of those are downloaded, and the review PDF itself is read for
its text and thrown away. Fifty thousand papers come to about 2.8 GB stored,
against roughly 900 GB if the PDFs were kept.

Fetching runs a few papers at a time. The limit is politeness rather than
throughput: these are public APIs that exist on someone else's budget, and being
rate-limited out of them would cost far more than the hours saved.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .corpus_index import (
    DB_PATH,
    INDEX_DIR,
    NUMBERED,
    REVIEWER_HEAD,
    LICENSE,
    parse_review,
    schema,
)
from .fetch import EPMC, SPRINGER_ESM, WANTED, _get

#: Simultaneous downloads. Measured at 4.2s of network per paper, so three
#: workers turn a 3.6-day sequential harvest into about a day.
WORKERS = 3

#: Seconds each worker waits between papers.
COURTESY = 2.0

PAGE = 100


@dataclass
class Progress:
    seen: int = 0
    stored: int = 0
    skipped: int = 0
    failed: int = 0


def search_page(query: str, cursor: str) -> tuple[list[dict], str]:
    params = urllib.parse.urlencode(
        {
            "query": query,
            "format": "json",
            "pageSize": PAGE,
            "cursorMark": cursor,
            "resultType": "lite",
        }
    )
    payload = json.loads(_get(f"{EPMC}/search?{params}").decode())
    results = [r for r in payload.get("resultList", {}).get("result", []) if r.get("pmcid")]
    return results, payload.get("nextCursorMark") or cursor


def abstract_from_xml(xml: str) -> tuple[str, str]:
    title = re.search(r"<article-title[^>]*>(.*?)</article-title>", xml, re.S)
    abstract = re.search(r"<abstract\b.*?</abstract>", xml, re.S)
    clean = lambda s: " ".join(re.sub(r"<[^>]+>", " ", s).split())
    return (
        clean(title.group(1)) if title else "",
        clean(abstract.group(0)) if abstract else "",
    )


def review_filename(xml: str) -> str | None:
    for block in re.finditer(r"<supplementary-material.*?</supplementary-material>", xml, re.S):
        label = re.sub(r"<[^>]+>", " ", block.group(0))
        if not WANTED["review"].search(label):
            continue
        href = re.search(r'xlink:href="([^"]+)"', block.group(0))
        if href and href.group(1).lower().endswith(".pdf"):
            return href.group(1)
    return None


#: Where review PDFs land when --keep-pdfs is given.
PDF_DIR = INDEX_DIR / "review_pdfs"
KEEP_PDFS = False


def harvest_one(paper: dict) -> dict | None:
    """Title, abstract and reviewer comments for one paper. None if unusable."""
    import pymupdf

    doi = paper.get("doi") or ""
    slug = doi.rsplit("/", 1)[-1] if doi else paper["pmcid"]
    xml = _get(f"{EPMC}/{paper['pmcid']}/fullTextXML", timeout=90).decode("utf-8", "replace")

    title, abstract = abstract_from_xml(xml)
    if len(abstract) < 150:
        return None
    filename = review_filename(xml)
    if not filename:
        return None

    quoted = urllib.parse.quote(f"art:{doi}", safe="")
    data = _get(f"{SPRINGER_ESM}/{quoted}/MediaObjects/{filename}", timeout=240)
    if KEEP_PDFS:
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        (PDF_DIR / f"{slug}.review.pdf").write_bytes(data)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    # The PDF is not kept; only what it says.
    reviews = parse_review(text)
    if not reviews:
        return None

    return {
        "slug": slug,
        "title": title,
        "abstract": abstract,
        "year": str(paper.get("pubYear", "")),
        "reviews": reviews,
        "raw_text": text,
    }


def run(query: str, limit: int, on_progress=None) -> Progress:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    schema(connection)
    # The whole extracted text is kept, not just the comments parsed out of it.
    # Re-fetching is the expensive, rate-limited step; re-parsing is free. If the
    # parser improves — and it has, repeatedly — this is what saves the harvest
    # from being run again. Only the images are unrecoverable.
    connection.execute(
        "CREATE TABLE IF NOT EXISTS review_text (slug TEXT PRIMARY KEY, text TEXT)"
    )

    have = {r[0] for r in connection.execute("SELECT slug FROM papers")}
    write_lock = threading.Lock()
    progress = Progress()

    def store(result: dict | None) -> None:
        with write_lock:
            progress.seen += 1
            if not result:
                progress.skipped += 1
            else:
                connection.execute(
                    "INSERT OR REPLACE INTO papers VALUES (?,?,?)",
                    (result["slug"], result["title"], result["abstract"]),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO review_text VALUES (?,?)",
                    (result["slug"], result["raw_text"]),
                )
                connection.execute("DELETE FROM comments WHERE slug = ?", (result["slug"],))
                for reviewer, items in result["reviews"]:
                    for position, text in enumerate(items, 1):
                        connection.execute(
                            "INSERT INTO comments VALUES (?,?,?,?)",
                            (result["slug"], reviewer, position, text),
                        )
                progress.stored += 1
                if progress.stored % 25 == 0:
                    connection.commit()
            if on_progress:
                on_progress(progress)

    def work(paper: dict) -> None:
        try:
            store(harvest_one(paper))
        except Exception:
            with write_lock:
                progress.seen += 1
                progress.failed += 1
        time.sleep(COURTESY)

    cursor = "*"
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while progress.seen < limit:
            batch, cursor_next = search_page(query, cursor)
            if not batch or cursor_next == cursor:
                break
            cursor = cursor_next
            fresh = [p for p in batch if (p.get("doi") or "").rsplit("/", 1)[-1] not in have]
            for future in [pool.submit(work, p) for p in fresh[: limit - progress.seen]]:
                future.result()

    connection.commit()
    connection.close()
    return progress


def main(argv: list[str] | None = None) -> int:
    import argparse

    # Downloading needs no credentials, but the embedding rebuild at the end
    # does, and it runs hours after the command was typed. Loading the settings
    # up front means a missing key is a message now rather than a crash then —
    # which is exactly how a 3,700-paper harvest was lost the first time.
    from . import config

    config.load()

    parser = argparse.ArgumentParser(
        prog="peerna-harvest",
        description="Build the referee-comment index. Text only; no PDFs are kept.",
    )
    parser.add_argument("query", nargs="?", default="", help='e.g. "electrocatalysis"')
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--from-year", type=int, default=2016,
                        help="Transparent peer review began in 2016")
    parser.add_argument("--from-date", default="",
                        help="Narrower than --from-year, as YYYY-MM-DD. Use this to "
                             "collect papers published after a model's training "
                             "cutoff, which is the only way to test against papers "
                             "it has certainly never read")
    parser.add_argument("--to-date", default="2030-12-31", help="As YYYY-MM-DD")
    parser.add_argument("--keep-pdfs", action="store_true",
                        help="Also keep the review PDFs (~3.5 MB each) — only the "
                             "images in them cannot be recovered from stored text")
    parser.add_argument("--embed", action="store_true",
                        help="Rebuild the embedding index when the harvest finishes")
    args = parser.parse_args(argv)

    terms = ['JOURNAL:"Nature communications"', "OPEN_ACCESS:Y", "HAS_SUPPL:Y",
             f"FIRST_PDATE:[{args.from_date or f'{args.from_year}-01-01'} "
             f"TO {args.to_date}]"]
    if args.query:
        terms.append(f"({args.query})")
    query = " AND ".join(terms)

    global KEEP_PDFS
    KEEP_PDFS = args.keep_pdfs
    started = time.time()

    def report(p: Progress) -> None:
        if p.seen % 25:
            return
        rate = p.seen / max(time.time() - started, 1)
        left = (args.limit - p.seen) / rate / 60 if rate else 0
        print(f"  {p.seen}/{args.limit}  stored {p.stored}  "
              f"no report {p.skipped}  failed {p.failed}  "
              f"~{left:.0f} min left", file=sys.stderr, flush=True)

    print(f"Harvesting up to {args.limit} papers, {WORKERS} at a time.", file=sys.stderr)
    result = run(query, args.limit, report)
    print(f"\nStored {result.stored}; {result.skipped} had no published report; "
          f"{result.failed} failed.", file=sys.stderr)

    if args.keep_pdfs:
        print(f"Review PDFs kept in {PDF_DIR}", file=sys.stderr)

    if args.embed:
        from .embed_index import rebuild

        print("Rebuilding embeddings…", file=sys.stderr)
        count = rebuild()
        print(f"Embedded {count} papers.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
