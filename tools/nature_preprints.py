"""Research Square v1 manuscripts of the Nature papers in the staging database.

As for Nature Communications (DECISIONS: "A test set of manuscripts as
submitted"): Springer Nature's In Review posts the manuscript when it is
submitted, so v1 is the text the referees read. A v1 is found from Crossref's
has-preprint relation or, since Crossref adds that relation months late, by
title and first author among Research Square's DOIs. Kept only if v1 was posted
from 30 days before receipt to before acceptance.

    python -m tools.nature_preprints DB OUT [--download]

Writes OUT/pairs.jsonl (one line per paper, resumable) and OUT/testset.json in
the format of the Nature Communications test set; --download fetches the v1
PDFs into OUT/pdf (Research Square's robots.txt allows crawling). The posting
stamp is removed afterwards with ~/PeerNa-data/rsquare/clean.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {"User-Agent": "peerna-research/0.1"}


def get(url: str, raw: bool = False):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                data = r.read()
            return data if raw else json.loads(data)["message"]
        except Exception:  # noqa: BLE001
            time.sleep(3 * (attempt + 1))
    return None


def norm(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", re.sub(r"<[^>]+>", "", s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def posted_of(item: dict) -> dt.date | None:
    parts = (item.get("posted") or {}).get("date-parts") or [[]]
    return dt.date(*(parts[0] + [1, 1])[:3]) if parts[0] else None


def one(row: dict) -> dict:
    slug = row["slug"]
    out = {"slug": slug}
    paper = get(f"https://api.crossref.org/works/10.1038/{slug}")
    if not paper:
        return {**out, "error": "crossref"}
    asserted = {a.get("name"): a.get("value") for a in paper.get("assertion", [])}
    day = lambda s: dt.date.fromisoformat(s) if s else None  # noqa: E731
    received = day(row["received"]) or (dt.datetime.strptime(asserted["received"], "%d %B %Y").date()
                                        if asserted.get("received") else None)
    accepted = day(row["accepted"]) or (dt.datetime.strptime(asserted["accepted"], "%d %B %Y").date()
                                        if asserted.get("accepted") else None)
    if not received or not accepted:
        return {**out, "error": "no dates"}
    out.update(received=received.strftime("%-d %B %Y"), accepted=accepted.strftime("%-d %B %Y"))

    candidates = []
    linked = [x["id"] for x in paper.get("relation", {}).get("has-preprint", [])
              if x.get("id", "").startswith("10.21203")]
    if linked:
        v1 = re.sub(r"/v\d+$", "/v1", sorted(linked)[0])
        item = get(f"https://api.crossref.org/works/{v1}")
        if item:
            candidates.append((1.0, v1, item, "relation"))
    else:
        title = (paper.get("title") or [""])[0]
        first = (paper.get("author") or [{}])[0].get("family") or ""
        q = urllib.parse.urlencode({"query.bibliographic": f"{title} {first}",
                                    "filter": "prefix:10.21203,type:posted-content", "rows": 5})
        for item in (get(f"https://api.crossref.org/works?{q}") or {}).get("items", []):
            authors = item.get("author") or []
            if not authors or norm(authors[0].get("family")) != norm(first):
                continue
            ratio = difflib.SequenceMatcher(None, " ".join(norm(title)),
                                            " ".join(norm((item.get("title") or [""])[0]))).ratio()
            if ratio >= 0.5:
                candidates.append((ratio, re.sub(r"/v\d+$", "/v1", item["DOI"]), item, "title"))
    for ratio, v1, item, how in sorted(candidates, key=lambda c: -c[0]):
        posted = posted_of(item)
        if posted and received - dt.timedelta(days=30) <= posted < accepted:
            out.update(v1=v1, posted=str(posted), how=how, title_ratio=round(ratio, 3),
                       license=(item.get("license") or [{}])[0].get("URL"), gap=(posted - received).days)
            break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    pairs = args.out / "pairs.jsonl"

    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = [dict(zip(("slug", "received", "accepted"), r))
            for r in connection.execute("SELECT slug, received, accepted FROM nature ORDER BY slug")]
    connection.close()
    done = {json.loads(l)["slug"] for l in pairs.read_text().splitlines()} if pairs.exists() else set()
    todo = [r for r in rows if r["slug"] not in done]
    print(f"{len(rows)} papers, {len(todo)} to look up", file=sys.stderr, flush=True)
    with pairs.open("a") as f, ThreadPoolExecutor(4) as pool:
        for i, row in enumerate(pool.map(one, todo), 1):
            f.write(json.dumps(row) + "\n")
            f.flush()
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}", file=sys.stderr, flush=True)

    found = [json.loads(l) for l in pairs.read_text().splitlines()]
    kept = [r for r in found if r.get("v1")]
    (args.out / "testset.json").write_text(json.dumps(kept, indent=1))
    print(f"{len(kept)} of {len(found)} have a v1 posted in time "
          f"({sum(r['how'] == 'relation' for r in kept)} by relation, {sum(r['how'] == 'title' for r in kept)} by title)",
          file=sys.stderr, flush=True)

    if args.download:
        pdf = args.out / "pdf"
        pdf.mkdir(exist_ok=True)
        ok = bad = 0
        for r in kept:
            path = pdf / f"{r['slug']}.pdf"
            if path.is_file():
                ok += 1
                continue
            rs = re.search(r"(rs-\d+)/v1", r["v1"]).group(1)
            data = get(f"https://www.researchsquare.com/article/{rs}/v1.pdf", raw=True)
            if data and data[:5] == b"%PDF-":
                path.write_bytes(data)
                ok += 1
            else:
                bad += 1
                print(f"  {r['slug']}: no PDF", file=sys.stderr, flush=True)
            time.sleep(1)
        print(f"PDFs: {ok}, missing {bad}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
