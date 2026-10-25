"""Referee comments of every corpus paper, read by a model instead of the parser.

On 14 test papers the parser had found half the first-round points a model
found (153 of 303), losing lists numbered "2, 3, …", the unnumbered points
after a short list, and reports it mistook for rebuttals. Haiku at medium
effort found 96% of what Sonnet did, at a fraction of its cost, so the corpus
is re-read with it: every referee, every round, an opening assessment and the
points, the authors' text left out.

    CLAUDE_CONFIG_DIR=… python -m tools.extract_comments OUT --db corpus.sqlite --since 2022

Writes OUT/<slug>.json, one per paper, so a stopped run resumes. When the
subscription's limit is reached it waits and carries on rather than failing
the rest. `--load` then writes the results into the database's comments_model
table, beside the parser's comments, which are kept.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, Field

from ai_peer_reviewer.embed_index import since_clause
from tools.precedent_eval import claude_cli

INSTRUCTION = """\
Below is the peer review file of a published paper: the referees' reports and
the authors' replies, usually over several rounds of review.

Return every referee report in it. For each, give the referee's number, the
round (1 for the reports on the manuscript as first submitted, 2 for the
reports on the first revision, and so on), the referee's opening assessment of
the paper if there is one, and the points: one entry per distinct request or
criticism, in the order written, in the referee's own words (shorten an entry
past about 120 words, keeping what is asked and where in the paper).

Leave out everything that is not a referee's: the authors' replies and
responses, revised text they quote back, their change lists, the editor's
text, the journal's form labels and licence lines. A report quoted again
inside the rebuttal counts once, in its own round. A reference list is not a
point unless the referee asks for those works to be cited.

PEER REVIEW FILE
{text}
"""


class Report(BaseModel):
    reviewer: int = Field(description="The referee's number as the file gives it.")
    round: int = Field(description="1 for the original submission, 2 for the first revision, …")
    summary: str = Field(description="The opening assessment, or an empty string.")
    points: list[str]


class Extracted(BaseModel):
    reports: list[Report]


#: How long to wait when the subscription's limit is reached before trying again.
LIMIT_WAIT = 600


def extract(slug: str, text: str, model: str, effort: str) -> dict:
    for attempt in range(100):
        try:
            reply = claude_cli("You extract referee comments from peer review files.",
                               INSTRUCTION.format(text=text), model,
                               Extracted.model_json_schema(), effort=effort)
            if reply.get("structured_output") is None:
                raise RuntimeError(f"no structured output ({reply.get('subtype')})")
            return Extracted.model_validate(reply["structured_output"]).model_dump()
        except RuntimeError as exc:
            message = str(exc).lower()
            if "limit" in message or "429" in message or "overloaded" in message:
                print(f"  limit reached; waiting {LIMIT_WAIT // 60} min", file=sys.stderr, flush=True)
                time.sleep(LIMIT_WAIT)
                continue
            if attempt < 2:
                continue
            raise
    raise RuntimeError("gave up after repeated limits")


def load(out: Path, db: Path) -> None:
    """Write the extracted comments into comments_model, replacing what is there."""
    connection = sqlite3.connect(db, timeout=60)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS comments_model (slug TEXT, reviewer INTEGER, round INTEGER, "
        "kind TEXT, position INTEGER, text TEXT)")
    connection.execute("CREATE INDEX IF NOT EXISTS comments_model_slug ON comments_model(slug)")
    files = sorted(out.glob("*.json"))
    for path in files:
        data = json.loads(path.read_text())
        rows = []
        for report in data["reports"]:
            position = 0
            if report["summary"].strip():
                position += 1
                rows.append((data["slug"], report["reviewer"], report["round"], "summary",
                             position, report["summary"].strip()))
            for point in report["points"]:
                if point.strip():
                    position += 1
                    rows.append((data["slug"], report["reviewer"], report["round"], "point",
                                 position, point.strip()))
        connection.execute("DELETE FROM comments_model WHERE slug = ?", (data["slug"],))
        connection.executemany("INSERT INTO comments_model VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    total = connection.execute("SELECT kind, count(*) FROM comments_model GROUP BY kind").fetchall()
    connection.close()
    print(f"Loaded {len(files)} papers: {dict(total)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--since", type=int, default=0)
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--workers-file", type=Path,
                        help="A file holding the number of papers to run at once, read every "
                             "few seconds, so the rate can change without a restart.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--load", action="store_true",
                        help="Write what has been extracted into the database and stop.")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.load:
        load(args.out, args.db)
        return 0

    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    slugs = [s for (s,) in connection.execute(
        "SELECT slug FROM review_text WHERE 1" + since_clause(args.since) + " ORDER BY slug DESC")]
    connection.close()
    todo = [s for s in slugs if not (args.out / f"{s}.json").is_file()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(slugs)} papers, {len(slugs) - len(todo)} done, {len(todo)} to go", file=sys.stderr)
    lock = threading.Lock()
    count = [0, 0]
    started = time.monotonic()

    def one(slug: str) -> None:
        try:
            db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
            text = db.execute("SELECT text FROM review_text WHERE slug = ?", (slug,)).fetchone()[0]
            db.close()
            data = extract(slug, text, args.model, args.effort)
            (args.out / f"{slug}.json").write_text(json.dumps(
                {"slug": slug, "model": args.model, "effort": args.effort, **data}))
            ok = True
        except Exception as exc:
            ok = False
            with lock:
                print(f"  {slug} failed: {type(exc).__name__}: {exc}"[:300], file=sys.stderr, flush=True)
        with lock:
            count[0 if ok else 1] += 1
            n = sum(count)
            if n % 50 == 0:
                rate = (time.monotonic() - started) / n
                print(f"  {n}/{len(todo)} ({count[1]} failed), ~{rate * (len(todo) - n) / 3600:.1f} h left",
                      file=sys.stderr, flush=True)

    # A gate in front of a large pool: the pool never limits, the gate does,
    # and the gate's size follows --workers-file when there is one.
    wanted = [args.workers]
    active = [0]
    gate = threading.Condition()

    def watch() -> None:
        while True:
            try:
                n = int(args.workers_file.read_text().strip())
                if n > 0 and n != wanted[0]:
                    with gate:
                        wanted[0] = n
                        gate.notify_all()
                    print(f"  now {n} at once", file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass
            time.sleep(10)

    if args.workers_file:
        threading.Thread(target=watch, daemon=True).start()

    def gated(slug: str) -> None:
        with gate:
            while active[0] >= wanted[0]:
                gate.wait(5)
            active[0] += 1
        try:
            one(slug)
        finally:
            with gate:
                active[0] -= 1
                gate.notify_all()

    with ThreadPoolExecutor(64) as pool:
        list(pool.map(gated, todo))
    print(f"DONE {count[0]} extracted, {count[1]} failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
