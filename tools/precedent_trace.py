"""What does a test paper share with the paper whose referee asked the same thing?

Exploratory, on a sample. tools/precedent_ceiling.py found that for more than a
third of the referee points, a comment asking the same thing is in the corpus,
but the search from the manuscript does not find it. Here each covered point is
traced back: its nearest comments are found again, and a model is shown the
referee point, the test paper's parts, and the candidate comments with their
papers' titles and abstracts. It says which comment asks the same thing, which
part of the test paper the point is about, and what that part and the precedent
paper have in common that makes the same request apply. If the answers share a
kind, that kind is what the search should look for.

    CLAUDE_CONFIG_DIR=… python -m tools.precedent_trace RUN CEILING ANSWERS OUT \\
        --testset testset.json --dates published-dates.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from pydantic import BaseModel, Field  # noqa: E402

from ai_peer_reviewer import config, corpus_index  # noqa: E402
from tools.precedent_ceiling import PER_POINT  # noqa: E402
from tools.precedent_eval import backend_for, received_dates, visible_before  # noqa: E402

SYSTEM = "You compare research papers and referee comments and reply with JSON only."

PROMPT = """\
A referee reviewing the TEST PAPER made the REFEREE POINT below. The CANDIDATES
are comments other referees wrote about other, earlier papers, each with that
paper's title and abstract. The TEST PARTS are what the test paper does, as a
model broke it down from the manuscript.

1. match: the number of the candidate that makes substantially the same request
   or criticism as the referee point (the same object and the same ask), or 0 if
   none does.
2. test_part: the test part the referee point is about, copied from the list,
   or "none" if it is about none of them.
3. shared: if there is a match, one sentence on what the test part and the
   candidate's paper have in common that makes the same request apply to both.
   Name the thing itself (for example "both choose a time window for counting
   linked admissions"), not that they are "similar".
4. shared_kind: the kind of that common ground.
5. topic: how close the two papers are in subject.
6. query: a short search phrase, written only from what the test part says and
   not from the referee point, that would find the matching comment among
   comments on papers of any subject.

TEST PAPER
{title}

TEST PARTS
{parts}

REFEREE POINT
{point}

CANDIDATES
{candidates}
"""


class Trace(BaseModel):
    match: int
    test_part: str
    shared: str
    shared_kind: Literal[
        "same method or technique",
        "same kind of design choice or parameter",
        "same kind of data or measurement",
        "same kind of claim or inference",
        "same reporting or presentation issue",
        "same subject only",
        "no match",
    ]
    topic: Literal["same subfield", "related field", "different field"]
    query: str = Field(description="A short search phrase from the test part alone.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("ceiling", type=Path)
    parser.add_argument("answers", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--testset", type=Path, required=True)
    parser.add_argument("--dates", type=Path, required=True)
    parser.add_argument("--papers", type=int, default=20)
    parser.add_argument("--per-paper", type=int, default=3)
    parser.add_argument("--predictor", default="claude-sonnet-5-5")
    parser.add_argument("--model", default="cli:claude-sonnet-5-5")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)
    config.load()
    args.out.mkdir(parents=True, exist_ok=True)

    titles = {t["slug"]: t.get("title") or t.get("preprint_title", "") for t in json.loads(args.testset.read_text())}
    received = received_dates(args.testset)
    published = json.loads(args.dates.read_text())
    db = sqlite3.connect(f"file:{corpus_index.DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    abstracts = dict(db.execute("SELECT slug, abstract FROM papers"))
    paper_titles = dict(db.execute("SELECT slug, title FROM papers"))

    jobs = []
    for path in sorted(args.ceiling.glob("s*.json")):
        result = json.loads(path.read_text())
        slug = result["slug"]
        if not result["covered"] or slug not in received:
            continue
        key = [p for r in sorted(json.loads((args.answers / f"{slug}.json").read_text())["reports"],
                                 key=lambda r: r["reviewer"]) for p in r["points"]]
        parts = json.loads((args.run / "predict" / args.predictor / f"{slug}.json").read_text())["parts"]
        visible = visible_before(received[slug], published)
        for rid in result["covered"][: args.per_paper]:
            jobs.append((slug, int(rid[1:]), key[int(rid[1:]) - 1], parts, visible))
        if len({j[0] for j in jobs}) >= args.papers:
            break
    print(f"{len(jobs)} points from {len({j[0] for j in jobs})} papers", file=sys.stderr)

    search_lock, lock = threading.Lock(), threading.Lock()
    backend, _ = backend_for(args.model)

    def one(job):
        slug, n, point, parts, visible = job
        path = args.out / f"{slug}-R{n}.json"
        if path.is_file():
            return json.loads(path.read_text())
        try:
            with search_lock:
                found = corpus_index.precedents([point[:800]], papers=visible, k=PER_POINT, exclude=slug)[0]
            candidates = "\n\n".join(
                f"{i}. Paper: {paper_titles.get(c.slug, '')}\n   Abstract: {(abstracts.get(c.slug) or '')[:700]}\n"
                f"   Comment: {c.text[:700]}" for i, c in enumerate(found, 1))
            trace = backend.run_structured(SYSTEM, [], PROMPT.format(
                title=titles.get(slug, slug), parts=parts, point=point[:800], candidates=candidates),
                Trace, effort="medium")
            record = {"slug": slug, "point": n, "referee_point": point,
                      "candidates": [{"slug": c.slug, "comment": c.text} for c in found],
                      **trace.model_dump()}
            path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
            with lock:
                print(f"  {slug} R{n}: match {trace.match}, {trace.shared_kind}, {trace.topic}",
                      file=sys.stderr, flush=True)
            return record
        except Exception as error:  # noqa: BLE001
            with lock:
                print(f"  {slug} R{n} failed: {str(error)[:200]}", file=sys.stderr, flush=True)
            return None

    with ThreadPoolExecutor(args.workers) as pool:
        records = [r for r in pool.map(one, jobs) if r]
    matched = [r for r in records if r["match"]]
    kinds = collections.Counter(r["shared_kind"] for r in matched)
    topics = collections.Counter(r["topic"] for r in matched)
    lines = [f"{len(records)} points traced, {len(matched)} with a matching comment", "", "common ground:"]
    lines += [f"  {n:3}  {k}" for k, n in kinds.most_common()]
    lines += ["", "subject distance:"] + [f"  {n:3}  {k}" for k, n in topics.most_common()]
    lines += ["", "examples:"]
    for r in matched[:15]:
        lines.append(f"  - [{r['shared_kind']}; {r['topic']}] {r['shared']}\n      query: {r['query']}")
    summary = "\n".join(lines)
    (args.out / "summary.txt").write_text(summary + "\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
