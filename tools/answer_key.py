"""First-round referee points of a test paper, read by a model, not the parser.

The evaluation's answer key had come from the parser, and a check of ten keys
found it losing points (a referee numbering "2, 3, …", a short list followed by
unnumbered minor points) and keeping fragments. For the 109 test papers the key
matters more than anywhere else, so a model reads each peer review file whole
and returns what each referee asked in the first round, and nothing else.

    python -m tools.answer_key SLUGS OUT --db corpus.sqlite [--model claude-sonnet-5-5]

Writes OUT/<slug>.json. `precedent_eval --answers OUT` then judges against it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, Field

from tools.precedent_eval import claude_cli

INSTRUCTION = """\
Below is the peer review file of a published paper: the referees' reports and
the authors' replies, often over several rounds of review.

Return the points each referee raised in the FIRST round only, the reports on
the manuscript as originally submitted. One entry per distinct request or
criticism, in the order the referee wrote them, in the referee's own words
(shorten an entry past about 120 words, keeping what is asked and where).

Leave out:
- anything the authors wrote: replies, responses, revised text quoted back,
  their change lists;
- the editor's text, the journal's form labels, licence lines;
- second and later rounds (comments on the revised manuscript, "the authors
  have addressed my concerns", follow-ups to the replies);
- a referee's opening summary of what the paper does, and a bare verdict
  ("I recommend publication") with no request in it;
- reference lists, unless a referee's point is to cite them, in which case
  the point is that request.

A first-round report quoted again inside the rebuttal counts once.

PEER REVIEW FILE
{text}
"""


class Report(BaseModel):
    reviewer: int = Field(description="The referee's number as the file gives it.")
    points: list[str]


class Key(BaseModel):
    reports: list[Report]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slugs", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    slugs = args.slugs.read_text().split()
    if args.limit:
        slugs = slugs[: args.limit]
    lock = threading.Lock()

    def one(slug: str) -> None:
        path = args.out / f"{slug}.json"
        if path.is_file():
            return
        try:
            connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
            text = connection.execute(
                "SELECT text FROM review_text WHERE slug = ?", (slug,)).fetchone()[0]
            connection.close()
            reply = claude_cli("You extract referee comments from peer review files.",
                               INSTRUCTION.format(text=text), args.model,
                               Key.model_json_schema(), effort=args.effort)
            key = Key.model_validate(reply["structured_output"])
            points = sum(len(r.points) for r in key.reports)
            path.write_text(json.dumps({"slug": slug, "model": args.model, "effort": args.effort,
                                        **key.model_dump()}, indent=1))
            with lock:
                print(f"  {slug}: {len(key.reports)} referees, {points} points",
                      file=sys.stderr, flush=True)
        except Exception as exc:
            with lock:
                print(f"  {slug} failed: {type(exc).__name__}: {exc}"[:300], file=sys.stderr, flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(one, slugs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
