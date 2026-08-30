#!/usr/bin/env python3
"""Download Nature Communications papers, referee reports and supplementary PDFs.

Standalone on purpose: copy this one file to whatever machine has the disk and
the patience, and run it. Nothing but the Python standard library is needed, and
nothing here imports the rest of PeerNa.

    python3 download_corpus.py --out /Volumes/big/corpus --limit 50000

It resumes. Files already on disk are skipped, so stopping with Ctrl-C and
starting again later picks up where it left off — which matters, because a full
run takes days.

    python3 download_corpus.py --out DIR --estimate      # size and time, no downloads
    python3 download_corpus.py --out DIR --query "electrocatalysis"
    python3 download_corpus.py --out DIR --parts review  # reports only, ~1/3 the size

Sizes, measured rather than guessed: an article PDF averages 3.7 MB, a referee
report 6.7 MB, supplementary information 6.1 MB. All three for fifty thousand
papers is roughly 800 GB; reports alone are about 330 GB.

Everything fetched is open access under CC BY. Requests go through Europe PMC,
which distributes this corpus for automated use, and to the publisher's own
static host for the supplementary PDFs. Do not raise the concurrency much: these
are public services, and being blocked out of them costs far more than the hours
saved.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EPMC_WEB = "https://europepmc.org"
SPRINGER = "https://static-content.springer.com/esm"

USER_AGENT = "peerna-corpus/1.0 (academic research; open-access harvesting)"

#: Average bytes per file, from a 25-paper sample. Used only for the estimate.
TYPICAL = {"article": 3_750_000, "review": 6_700_000, "si": 6_060_000}

#: Labels the publisher uses for each kind of supplementary file. The numbering
#: is not stable — MOESM6 on one paper is MOESM4 on the next — so the label in
#: the JATS is the only reliable way to tell them apart. "Description of
#: Additional Supplementary Files" is an index of the others and must not match,
#: which is what the negative lookahead on the plural is for.
LABELS = {
    "review": re.compile(r"peer\s*review", re.I),
    "si": re.compile(r"supp(?:lementary|orting)\s+(?:information|material|file)\b(?!s)", re.I),
}

stopping = threading.Event()


@dataclass
class Tally:
    seen: int = 0
    downloaded: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0
    bytes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, **counts: int) -> None:
        with self.lock:
            for name, value in counts.items():
                setattr(self, name, getattr(self, name) + value)


def fetch(url: str, timeout: int = 240, tries: int = 3) -> bytes:
    """GET with retries. Raises on a 404 immediately — that file is simply absent."""
    last: Exception | None = None
    for attempt in range(tries):
        if stopping.is_set():
            raise KeyboardInterrupt
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403):
                raise
            last = exc
        except Exception as exc:
            last = exc
        time.sleep(2 ** attempt * 3)
    raise last or RuntimeError("unreachable")


def search(query: str, limit: int, from_year: int):
    """Yield paper records, paging through Europe PMC with a cursor."""
    terms = [
        'JOURNAL:"Nature communications"',
        "OPEN_ACCESS:Y",
        "HAS_SUPPL:Y",
        f"FIRST_PDATE:[{from_year}-01-01 TO 2030-12-31]",
    ]
    if query:
        terms.append(f"({query})")
    joined = " AND ".join(terms)

    cursor, yielded = "*", 0
    while yielded < limit and not stopping.is_set():
        params = urllib.parse.urlencode(
            {"query": joined, "format": "json", "pageSize": 100,
             "cursorMark": cursor, "resultType": "lite"}
        )
        payload = json.loads(fetch(f"{EPMC}/search?{params}", timeout=90).decode())
        results = payload.get("resultList", {}).get("result", [])
        if not results:
            return
        for record in results:
            if not record.get("pmcid") or not record.get("doi"):
                continue
            yield record
            yielded += 1
            if yielded >= limit:
                return
        nxt = payload.get("nextCursorMark")
        if not nxt or nxt == cursor:
            return
        cursor = nxt


def count_hits(query: str, from_year: int) -> int:
    terms = ['JOURNAL:"Nature communications"', "OPEN_ACCESS:Y", "HAS_SUPPL:Y",
             f"FIRST_PDATE:[{from_year}-01-01 TO 2030-12-31]"]
    if query:
        terms.append(f"({query})")
    params = urllib.parse.urlencode(
        {"query": " AND ".join(terms), "format": "json", "pageSize": 1, "resultType": "idlist"}
    )
    return int(json.loads(fetch(f"{EPMC}/search?{params}", timeout=90).decode()).get("hitCount", 0))


def supplementary_names(xml: str) -> dict[str, str]:
    """Map each wanted kind to its filename, read off the JATS labels."""
    found: dict[str, str] = {}
    for block in re.finditer(r"<supplementary-material.*?</supplementary-material>", xml, re.S):
        href = re.search(r'xlink:href="([^"]+)"', block.group(0))
        if not href or not href.group(1).lower().endswith(".pdf"):
            continue
        label = re.sub(r"<[^>]+>", " ", block.group(0))
        for kind, pattern in LABELS.items():
            if kind not in found and pattern.search(label):
                found[kind] = href.group(1)
    return found


def save(path: Path, data: bytes) -> None:
    """Write via a temporary name so an interrupted run leaves no half file."""
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def handle(record: dict, out: Path, parts: set[str], tally: Tally, pause: float) -> None:
    doi = record["doi"]
    slug = doi.rsplit("/", 1)[-1]
    targets = {
        "article": out / f"{slug}.pdf",
        "review": out / f"{slug}.review.pdf",
        "si": out / f"{slug}.si.pdf",
    }
    wanted = {k for k in parts if not targets[k].exists()}
    if not wanted:
        tally.add(seen=1, skipped=1)
        return

    try:
        names: dict[str, str] = {}
        if wanted & {"review", "si"}:
            xml = fetch(f"{EPMC}/{record['pmcid']}/fullTextXML", timeout=90).decode("utf-8", "replace")
            names = supplementary_names(xml)

        quoted = urllib.parse.quote(f"art:{doi}", safe="")
        for kind in ("article", "review", "si"):
            if kind not in wanted or stopping.is_set():
                continue
            if kind == "article":
                url = f"{EPMC_WEB}/articles/{record['pmcid']}?pdf=render"
            else:
                filename = names.get(kind)
                if not filename:
                    tally.add(missing=1)
                    continue
                url = f"{SPRINGER}/{quoted}/MediaObjects/{filename}"
            try:
                data = fetch(url)
            except urllib.error.HTTPError:
                tally.add(missing=1)
                continue
            if not data.startswith(b"%PDF-"):
                # nature.com serves a bot-challenge page to non-browser clients;
                # anything that is not a PDF here is that, or an error page.
                tally.add(missing=1)
                continue
            save(targets[kind], data)
            tally.add(downloaded=1, bytes=len(data))
        tally.add(seen=1)
    except KeyboardInterrupt:
        raise
    except Exception:
        tally.add(seen=1, failed=1)
    finally:
        time.sleep(pause)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download the Nature Communications open-access corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Sizes,")[1].strip() if "Sizes," in __doc__ else None,
    )
    parser.add_argument("--out", type=Path, required=True, help="Directory to fill")
    parser.add_argument("--limit", type=int, default=50_000)
    parser.add_argument("--query", default="", help="Restrict to a topic")
    parser.add_argument("--from-year", type=int, default=2016,
                        help="Transparent peer review began in 2016")
    parser.add_argument("--parts", default="article,review,si",
                        help="Any of article, review, si")
    parser.add_argument("--workers", type=int, default=3,
                        help="Concurrent papers. Please leave this small.")
    parser.add_argument("--pause", type=float, default=1.5,
                        help="Seconds each worker waits between papers")
    parser.add_argument("--estimate", action="store_true",
                        help="Report expected size and time, download nothing")
    args = parser.parse_args(argv)

    parts = {p.strip() for p in args.parts.split(",") if p.strip()}
    unknown = parts - set(TYPICAL)
    if unknown:
        raise SystemExit(f"Unknown part(s): {', '.join(sorted(unknown))}")

    total = min(count_hits(args.query, args.from_year), args.limit)
    per_paper = sum(TYPICAL[p] for p in parts)
    seconds = total * (1.3 + 2.9 * len(parts) + args.pause) / max(args.workers, 1)

    print(f"Matching papers : {total:,}")
    print(f"Parts           : {', '.join(sorted(parts))}")
    print(f"Expected size   : {human(total * per_paper)}")
    print(f"Expected time   : {seconds / 3600:.1f} h  ({seconds / 86400:.1f} days)")

    if args.estimate:
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(args.out).free
    print(f"Free on target  : {human(free)}")
    if free < total * per_paper:
        print("\nNot enough free space for the full run. It will fill the disk and stop;\n"
              "narrow it with --limit, --query, or --parts.", file=sys.stderr)
        if input("Continue anyway? [y/N] ").strip().lower() != "y":
            return 1

    signal.signal(signal.SIGINT, lambda *_: (stopping.set(), print("\nStopping…", file=sys.stderr)))

    tally = Tally()
    started = time.time()

    def report() -> None:
        elapsed = max(time.time() - started, 1)
        rate = tally.seen / elapsed
        left = (total - tally.seen) / rate / 3600 if rate else 0
        print(f"  {tally.seen:,}/{total:,}  files {tally.downloaded:,}  "
              f"{human(tally.bytes)}  skipped {tally.skipped:,}  "
              f"absent {tally.missing:,}  failed {tally.failed:,}  "
              f"~{left:.1f} h left", file=sys.stderr, flush=True)

    print()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = []
        for record in search(args.query, args.limit, args.from_year):
            if stopping.is_set():
                break
            pending.append(pool.submit(handle, record, args.out, parts, tally, args.pause))
            if len(pending) >= args.workers * 4:
                for future in pending:
                    try:
                        future.result()
                    except KeyboardInterrupt:
                        stopping.set()
                pending.clear()
                if tally.seen % 50 < args.workers * 4:
                    report()
        for future in pending:
            try:
                future.result()
            except KeyboardInterrupt:
                stopping.set()

    report()
    print(f"\nDone. {tally.downloaded:,} files, {human(tally.bytes)}, "
          f"in {(time.time() - started) / 3600:.1f} h.", file=sys.stderr)
    if tally.missing:
        print(f"{tally.missing:,} files were not published for their paper "
              "(transparent peer review is opt-in).", file=sys.stderr)
    print(f"Re-run the same command to resume; existing files are skipped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
