"""Referee reports of open-access Nature papers, into a separate staging database.

Nature (the journal) publishes a Peer Review File with a paper when its authors
chose to (from 2020), and with every paper submitted from 16 June 2025 on. This
takes the open-access (CC BY) ones the same way harvest.py takes Nature
Communications: Europe PMC's search and full-text XML, and the review PDF from
Springer's static host, all with an honest User-Agent. nature.com itself is not
read: it serves a bot challenge to automated clients (DECISIONS: "Do not fetch
article PDFs from nature.com"). Paywalled papers are left out, since their full
text is not in Europe PMC and their review files carry no licence.

    python -m tools.harvest_nature DB [--since 2022] [--pdfs DIR]

Same tables as the main corpus (papers, review_text, comments, unusable), so
tools.extract_comments reads it as it is, plus `nature` (slug, published,
received, accepted, pmcid). --pdfs also keeps each article PDF, rendered by
Europe PMC. Resumes: papers stored or turned down are skipped.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_peer_reviewer.corpus_index import parse_review, schema, store_comments
from ai_peer_reviewer.fetch import EPMC, EPMC_WEB, SPRINGER_ESM, _get
from ai_peer_reviewer.harvest import abstract_from_xml, review_filename, search_page

COURTESY = 1.0


def _date(xml: str, kind: str) -> str:
    m = re.search(r'<date date-type="' + kind + r'"[^>]*>(.*?)</date>', xml, re.S)
    if not m:
        return ""
    parts = {k: re.search(f"<{k}>(\\d+)</{k}>", m.group(1)) for k in ("year", "month", "day")}
    if not parts["year"]:
        return ""
    return "-".join(f"{int(parts[k].group(1)):02d}" if parts[k] else "01" for k in ("year", "month", "day"))


def _published(xml: str) -> str:
    for kind in ("epub", "pub", ""):
        m = re.search(r'<pub-date[^>]*(?:pub-type|date-type)="' + kind + r'"[^>]*>(.*?)</pub-date>', xml, re.S) if kind \
            else re.search(r"<pub-date[^>]*>(.*?)</pub-date>", xml, re.S)
        if m:
            y, mo, d = (re.search(f"<{k}>(\\d+)</{k}>", m.group(1)) for k in ("year", "month", "day"))
            if y:
                return f"{y.group(1)}-{int(mo.group(1)) if mo else 1:02d}-{int(d.group(1)) if d else 1:02d}"
    return ""


def harvest_one(paper: dict, pdfs: Path | None) -> dict | None:
    import pymupdf

    doi = paper["doi"]
    slug = doi.split("/", 1)[1]
    xml = _get(f"{EPMC}/{paper['pmcid']}/fullTextXML", timeout=90).decode("utf-8", "replace")
    if not re.search(r'article-type="research-article"', xml):
        return None
    title, abstract = abstract_from_xml(xml)
    filename = review_filename(xml)
    if len(abstract) < 150 or not filename:
        return None
    quoted = urllib.parse.quote(f"art:{doi}", safe="")
    data = _get(f"{SPRINGER_ESM}/{quoted}/MediaObjects/{filename}", timeout=240)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "".join(p.get_text() for p in doc)
    if len(text) < 2000:
        return None
    if pdfs is not None and not (pdfs / f"{slug}.pdf").is_file():
        article = _get(f"{EPMC_WEB}/articles/{paper['pmcid']}?pdf=render", timeout=240)
        if article[:5] == b"%PDF-":
            (pdfs / f"{slug}.pdf").write_bytes(article)
    return {"slug": slug, "title": title, "abstract": abstract, "raw_text": text,
            "reviews": parse_review(text), "published": _published(xml),
            "received": _date(xml, "received"), "accepted": _date(xml, "accepted"), "pmcid": paper["pmcid"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--since", type=int, default=2022)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--pdfs", type=Path)
    args = parser.parse_args(argv)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.pdfs:
        args.pdfs.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(args.db, check_same_thread=False)
    schema(connection)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS review_text (slug TEXT PRIMARY KEY, text TEXT);
        CREATE TABLE IF NOT EXISTS unusable (slug TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS nature (slug TEXT PRIMARY KEY, published TEXT, received TEXT,
                                           accepted TEXT, pmcid TEXT);
    """)
    have = {r[0] for r in connection.execute("SELECT slug FROM papers UNION SELECT slug FROM unusable")}

    # Registered in `since` or later, by the year in the DOI; a paper registered
    # in December can be dated the next year, so the search starts half a year early.
    query = (f'ISSN:"0028-0836" AND OPEN_ACCESS:Y AND HAS_SUPPL:Y '
             f'AND FIRST_PDATE:[{args.since - 1}-07-01 TO 2030-12-31]')
    papers, cursor = [], "*"
    while True:
        batch, following = search_page(query, cursor)
        papers += [p for p in batch if (p.get("doi") or "").lower().startswith("10.1038/s41586-")]
        if not batch or following == cursor:
            break
        cursor = following
    papers = [p for p in {p["doi"].lower(): p for p in papers}.values()
              if int(p["doi"][15:18]) >= args.since - 2000]
    todo = [p for p in papers if p["doi"].split("/", 1)[1].lower() not in have]
    print(f"{len(papers)} open-access papers from {args.since}, {len(todo)} to look at", file=sys.stderr, flush=True)

    lock = threading.Lock()
    counts = {"seen": 0, "stored": 0, "skipped": 0, "failed": 0}

    def work(paper: dict) -> None:
        paper = {**paper, "doi": paper["doi"].lower()}
        slug = paper["doi"].split("/", 1)[1]
        try:
            result, error = harvest_one(paper, args.pdfs), None
        except Exception as e:  # noqa: BLE001
            result, error = None, e
        with lock:
            counts["seen"] += 1
            if error is not None:
                counts["failed"] += 1
                print(f"  {slug} failed: {str(error)[:150]}", file=sys.stderr, flush=True)
            elif result is None:
                counts["skipped"] += 1
                connection.execute("INSERT OR IGNORE INTO unusable VALUES (?)", (slug,))
            else:
                connection.execute("INSERT OR REPLACE INTO papers VALUES (?,?,?)",
                                   (slug, result["title"], result["abstract"]))
                connection.execute("INSERT OR REPLACE INTO review_text VALUES (?,?)", (slug, result["raw_text"]))
                connection.execute("INSERT OR REPLACE INTO nature VALUES (?,?,?,?,?)",
                                   (slug, result["published"], result["received"], result["accepted"],
                                    result["pmcid"]))
                if result["reviews"]:
                    store_comments(connection, slug, result["reviews"])
                counts["stored"] += 1
            if counts["seen"] % 25 == 0:
                connection.commit()
                print(f"  {counts['seen']}/{len(todo)}: stored {counts['stored']}, skipped {counts['skipped']}, "
                      f"failed {counts['failed']}", file=sys.stderr, flush=True)
        time.sleep(COURTESY)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(work, todo))
    connection.commit()
    print(f"DONE {counts}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
