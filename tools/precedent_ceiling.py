"""The most precedents could hold: search with the referees' own points.

tools/precedent_answers.py found that the comments the search returned held few
of the referees' points. This asks whether better ones were there to be found:
each first-round point of a test paper is itself the query, the comment index is
searched for the nearest comments on papers published before the manuscript was
received, and the judge marks which points the comments found cover. A search
can do no better than one handed the answer, so this is its ceiling.

    CLAUDE_CONFIG_DIR=… python -m tools.precedent_ceiling RUN ANSWERS OUT \\
        --testset testset.json --dates published-dates.json [--index DIR --db DB]

--index and --db pick the comment index searched: the one the precedent test
used, or the current one. Writes OUT/<slug>.json, so a stopped run resumes.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from ai_peer_reviewer import config, corpus_index  # noqa: E402
from tools.precedent_answers import POINT_CHARS, PROMPT, SYSTEM, Covered  # noqa: E402
from tools.precedent_eval import JUDGE_EFFORT, backend_for, received_dates, visible_before  # noqa: E402

#: Nearest comments kept for each point. Three a point is about eighty a paper,
#: as many as the precedent test handed the predictor.
PER_POINT = 3


def run_one(slug: str, key: list[str], visible: set[str], judge_name: str, out: Path,
            search_lock: threading.Lock, lock: threading.Lock) -> dict:
    path = out / f"{slug}.json"
    if path.is_file():
        return json.loads(path.read_text())
    real, used = [], 0
    for c in key:
        line = c[:POINT_CHARS]
        if used + len(line) > 40_000:
            break
        real.append(line)
        used += len(line)
    # One search at a time: the index is shared and the scoring is memory-heavy.
    with search_lock:
        found = corpus_index.precedents(real, papers=visible, k=PER_POINT, exclude=slug)
    items = [p.text[:600] for group in found for p in group]
    listed = "\n".join(f"M{i}. {t}" for i, t in enumerate(items, 1))
    backend, model_id = backend_for(judge_name)
    answer = backend.run_structured(SYSTEM, [], PROMPT.format(
        real="\n".join(f"R{n}. {c}" for n, c in enumerate(real, 1)), material=listed),
        Covered, effort=JUDGE_EFFORT)
    valid = {f"R{n}" for n in range(1, len(real) + 1)}
    covered = sorted(set(answer.covered) & valid, key=lambda r: int(r[1:]))
    result = {"slug": slug, "judge": model_id, "key_points": len(real), "items": len(items),
              "covered": covered, "visible_papers": len(visible),
              "index": str(corpus_index.COMMENT_VECTORS_PATH.parent), "db": str(corpus_index.DB_PATH)}
    path.write_text(json.dumps(result, indent=1))
    with lock:
        print(f"  {slug}: key {len(real)}, {len(items)} comments, covered {len(covered)}",
              file=sys.stderr, flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("answers", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--testset", type=Path, required=True)
    parser.add_argument("--dates", type=Path, required=True)
    parser.add_argument("--index", type=Path, help="Directory with comment_vectors.npy and comment_ids.json.")
    parser.add_argument("--db", type=Path, help="The database those vectors' rowids point into.")
    parser.add_argument("--predictor", default="claude-sonnet-5-5")
    parser.add_argument("--judge", default="cli:claude-sonnet-5-5")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    config.load()  # the OpenAI key, for embedding the queries
    if args.index:
        corpus_index.COMMENT_VECTORS_PATH = args.index / "comment_vectors.npy"
        corpus_index.COMMENT_IDS_PATH = args.index / "comment_ids.json"
    if args.db:
        corpus_index.DB_PATH = args.db
    corpus_index._comment_index = None
    args.out.mkdir(parents=True, exist_ok=True)

    received = received_dates(args.testset)
    published = json.loads(args.dates.read_text())
    # The papers the precedent test judged, so the numbers line up with it.
    judged = sorted(p.stem for p in (args.run / "judge" / args.predictor).glob("*.json"))
    jobs = []
    for slug in judged:
        keyfile = args.answers / f"{slug}.json"
        if not keyfile.is_file() or slug not in received:
            continue
        key = [p for r in sorted(json.loads(keyfile.read_text())["reports"],
                                 key=lambda r: r["reviewer"]) for p in r["points"]]
        if key:
            jobs.append((slug, key, visible_before(received[slug], published)))
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"{len(jobs)} papers, index {corpus_index.COMMENT_VECTORS_PATH.parent}", file=sys.stderr)

    search_lock, lock = threading.Lock(), threading.Lock()
    failed = []

    def one(job):
        try:
            return run_one(*job, args.judge, args.out, search_lock, lock)
        except Exception as error:  # noqa: BLE001
            with lock:
                failed.append(job[0])
                print(f"  {job[0]} failed: {str(error)[:200]}", file=sys.stderr, flush=True)
            return None

    with ThreadPoolExecutor(args.workers) as pool:
        results = [r for r in pool.map(one, jobs) if r]
    if failed:
        print(f"{len(failed)} failed; run again to retry them.", file=sys.stderr)
    if results:
        counts = [len(r["covered"]) for r in results]
        shares = [len(r["covered"]) / r["key_points"] for r in results]
        summary = (f"{len(results)} papers, {statistics.mean(r['key_points'] for r in results):.1f} "
                   f"referee points a paper, {statistics.mean(r['items'] for r in results):.0f} comments "
                   f"found a paper ({PER_POINT} a point)\n"
                   f"  covered: {statistics.mean(counts):.2f} a paper ({statistics.mean(shares):.1%}); "
                   f"papers with none: {sum(c == 0 for c in counts)}")
        (args.out / "summary.txt").write_text(summary + "\n")
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
