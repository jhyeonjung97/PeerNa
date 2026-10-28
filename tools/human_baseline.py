"""How many of one referee's points do the other referees also make?

A ceiling for the precedent test. The models hit about 4.4 of 12 predictions
against all referees' points; whether that is near what a person manages, or far
from it, decides how much room precedents had to help. Each referee of a paper
with two or more is held out in turn: their first-round points become the
"predictions" and the other referees' points the answer key. The models'
manuscript-only predictions (the prior arm of tools/precedent_eval.py) are
judged in the same shuffled list against the same reduced key, so the three are
marked by one judge under one standard.

    CLAUDE_CONFIG_DIR=… python -m tools.human_baseline RUN ANSWERS OUT

RUN is a precedent_eval output directory (its predict/ folder), ANSWERS the
answer keys it judged against. Writes OUT/<slug>-r<n>.json, so a stopped run
resumes; prints the summary at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from math import sqrt
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from tools.precedent_eval import (  # noqa: E402
    JUDGE_EFFORT, JUDGE_PROMPT, JUDGE_SYSTEM, Hits, backend_for, sign_test)

PREDICTORS = {"sonnet": "claude-sonnet-5-5", "luna": "gpt-5.6-luna"}
#: A referee's point is a paragraph, a prediction one sentence; cut to about the
#: length the key's points are cut to, so a long point is not judged on more text.
POINT_CHARS = 800


def judge_one(slug: str, held: int, reports: list[dict], predicted: dict[str, list[str]],
              judge_name: str, out: Path, lock: threading.Lock) -> dict | None:
    path = out / f"{slug}-r{held}.json"
    if path.is_file():
        return json.loads(path.read_text())
    key = [p for r in reports if r["reviewer"] != held for p in r["points"]]
    mine = next(r["points"] for r in reports if r["reviewer"] == held)
    if not key or not mine:
        return None
    real, used = [], 0
    for n, c in enumerate(key, 1):
        line = f"R{n}. {c[:POINT_CHARS]}"
        if used + len(line) > 40_000:
            break
        real.append(line)
        used += len(line)
    pool = [("referee", p[:POINT_CHARS]) for p in mine]
    pool += [(who, p) for who, preds in predicted.items() for p in preds]
    random.Random(f"{slug}-{held}").shuffle(pool)
    listed = "\n".join(f"P{i}. {text}" for i, (_, text) in enumerate(pool, 1))
    backend, model_id = backend_for(judge_name)
    answer = backend.run_structured(JUDGE_SYSTEM, [], JUDGE_PROMPT.format(
        real="\n".join(real), predicted=listed), Hits, effort=JUDGE_EFFORT)
    hit_ids = set(answer.hits)
    count = {who: 0 for who in ["referee", *predicted]}
    hits = dict(count)
    for i, (who, _) in enumerate(pool, 1):
        count[who] += 1
        hits[who] += f"P{i}" in hit_ids
    verdict = {"slug": slug, "held_out": held, "judge": model_id, "key_points": len(real),
               "count": count, "hits": hits, "hit_ids": sorted(hit_ids),
               "pool": [who for who, _ in pool]}
    path.write_text(json.dumps(verdict, indent=1))
    with lock:
        print(f"  {slug} r{held}: key {len(real)}, " + ", ".join(
            f"{w} {hits[w]}/{count[w]}" for w in count), file=sys.stderr, flush=True)
    return verdict


def summarise(verdicts: list[dict]) -> str:
    """Precision (hits over predictions) per source, averaged first within a
    paper over its held-out referees, then over papers, so a paper with four
    referees does not count twice as much as one with two."""
    by_paper: dict[str, list[dict]] = {}
    for v in verdicts:
        by_paper.setdefault(v["slug"], []).append(v)
    sources = list(verdicts[0]["count"])
    per_paper = {s: [] for s in sources}
    for vs in by_paper.values():
        for s in sources:
            per_paper[s].append(statistics.mean(v["hits"][s] / v["count"][s] for v in vs if v["count"][s]))
    lines = [f"{len(by_paper)} papers, {len(verdicts)} held-out referees "
             f"(key: the other referees' first-round points, mean "
             f"{statistics.mean(v['key_points'] for v in verdicts):.1f}; held-out referee's points, mean "
             f"{statistics.mean(v['count']['referee'] for v in verdicts):.1f})", ""]
    for s in sources:
        m = statistics.mean(per_paper[s])
        lines.append(f"  {s:8} {m:6.1%} of its points match   (= {12 * m:.1f} of 12)")
    lines.append("")
    for s in sources:
        if s == "referee":
            continue
        diffs = [a - b for a, b in zip(per_paper["referee"], per_paper[s])]
        mean = statistics.mean(diffs)
        half = 1.96 * statistics.stdev(diffs) / sqrt(len(diffs))
        wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
        lines.append(f"  referee − {s}: {mean:+.1%} (95% CI {mean - half:+.1%} to {mean + half:+.1%}); "
                     f"referee higher on {wins}, lower on {losses}, sign test p={sign_test(wins, losses):.3f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("answers", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--judge", default="cli:claude-sonnet-5-5")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="Papers, for a dry run.")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    # Papers with both predictors' predictions and two or more referees.
    jobs = []
    slugs = sorted(p.stem for p in (args.run / "predict" / PREDICTORS["sonnet"]).glob("*.json"))
    for slug in slugs:
        files = {who: args.run / "predict" / model / f"{slug}.json" for who, model in PREDICTORS.items()}
        key = args.answers / f"{slug}.json"
        if not key.is_file() or not all(f.is_file() for f in files.values()):
            continue
        predicted = {who: json.loads(f.read_text()).get("prior") or [] for who, f in files.items()}
        if not all(len(p) == 12 for p in predicted.values()):
            continue
        reports = [r for r in json.loads(key.read_text())["reports"] if r["points"]]
        if len(reports) < 2:
            continue
        jobs += [(slug, r["reviewer"], reports, predicted) for r in reports]
        if args.limit and len({j[0] for j in jobs}) >= args.limit:
            break
    print(f"{len({j[0] for j in jobs})} papers, {len(jobs)} held-out referees", file=sys.stderr)

    lock = threading.Lock()
    failed = []

    def one(job):
        try:
            return judge_one(*job, args.judge, args.out, lock)
        except Exception as error:  # noqa: BLE001 — one failure must not stop the rest
            with lock:
                failed.append(job[:2])
                print(f"  {job[0]} r{job[1]} failed: {str(error)[:200]}", file=sys.stderr, flush=True)
            return None

    with ThreadPoolExecutor(args.workers) as pool:
        verdicts = [v for v in pool.map(one, jobs) if v]
    if failed:
        print(f"{len(failed)} failed; run again to retry them.", file=sys.stderr)
    if verdicts:
        summary = summarise(verdicts)
        (args.out / "summary.txt").write_text(summary + "\n")
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
