"""Review a few test manuscripts with the old passes and the new, and count by kind.

A pilot for the fifth lens (choices, scope and terms) and the itemised
housekeeping points (commit 94e794d). Each manuscript is reviewed twice through
Claude Code, once with the passes as they were before that commit and once as
they are now, and each report is marked against the referees' first-round points
as in tools/precedent_answers.py, then counted by kind with the labels from
tools/classify_points.py.

Both arms run the same way, so the comparison is fair, but neither is the site's
review: Claude Code sees the text and not the figures, has no web search, and
the precedent and consistency passes are left out of both (the first is the part
being retired, the second does not change).

    CLAUDE_CONFIG_DIR=… python -m tools.design_pilot PDFS ANSWERS KINDS OUT --slugs …
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from ai_peer_reviewer import loader, models, review  # noqa: E402
from tools.classify_points import KINDS, key_points  # noqa: E402
from tools.precedent_answers import PROMPT, SYSTEM, Covered  # noqa: E402
from tools.precedent_eval import JUDGE_EFFORT, ClaudeCodeBackend, backend_for  # noqa: E402

#: The passes as they were before the fifth lens.
BEFORE = "262ead3"

def old_passes() -> types.ModuleType:
    source = subprocess.run(["git", "show", f"{BEFORE}:ai_peer_reviewer/passes.py"],
                            capture_output=True, text=True, check=True,
                            cwd=Path(__file__).resolve().parent.parent).stdout
    module = types.ModuleType("ai_peer_reviewer.passes_before")
    module.__package__ = "ai_peer_reviewer"
    sys.modules[module.__name__] = module
    exec(compile(source, "passes_before.py", "exec"), module.__dict__)
    return module


def review_one(pdf: Path, arm: str, passes: types.ModuleType, model: str) -> dict:
    spec = models.resolve(model)
    manuscript = loader.load(pdf, spec)
    reviewer = review.Reviewer(ClaudeCodeBackend(spec), "referee", use_web_search=False)
    reviewer.precedent_pass = lambda manuscript: ""
    reviewer.check_consistency = lambda manuscript, announce=None: None
    result = reviewer.review(manuscript)
    report = result.report
    return {"general_comments": report.general_comments,
            "comments": [c.text if hasattr(c, "text") else str(c) for c in report.comments],
            "timings": result.timings, "arm": arm}


def cover(report: dict, key: list[str], judge: str) -> list[str]:
    items = [report["general_comments"], *report["comments"]]
    backend, _ = backend_for(judge)
    answer = backend.run_structured(SYSTEM, [], PROMPT.format(
        real="\n".join(f"R{n}. {c}" for n, c in enumerate(key, 1)),
        material="\n".join(f"M{i}. {t}" for i, t in enumerate(items, 1))), Covered, effort=JUDGE_EFFORT)
    valid = {f"R{n}" for n in range(1, len(key) + 1)}
    return sorted(set(answer.covered) & valid, key=lambda r: int(r[1:]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", type=Path)
    parser.add_argument("answers", type=Path)
    parser.add_argument("kinds", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--slugs", type=Path, required=True, help="Candidates, one per line.")
    parser.add_argument("--papers", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--judge", default="cli:claude-sonnet-5-5")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    candidates = sorted(s for s in args.slugs.read_text().split()
                        if (args.pdfs / f"{s}.pdf").is_file() and (args.kinds / f"{s}.json").is_file())
    chosen = random.Random(args.seed).sample(candidates, min(args.papers, len(candidates)))
    from ai_peer_reviewer import passes as current

    arms = {"before": old_passes(), "after": current}
    print(f"{len(chosen)} papers: {', '.join(chosen)}", file=sys.stderr)

    lock = threading.Lock()

    def one(job):
        slug, arm = job
        path = args.out / f"{slug}-{arm}.json"
        record = json.loads(path.read_text()) if path.is_file() else None
        try:
            if record is None:
                record = review_one(args.pdfs / f"{slug}.pdf", arm, arms[arm], args.model)
                path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
            if "covered" not in record:
                record["covered"] = cover(record, key_points(args.answers, slug), args.judge)
                path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
            with lock:
                print(f"  {slug} {arm}: {len(record['comments'])} comments, covered {len(record['covered'])}",
                      file=sys.stderr, flush=True)
            return slug, arm, record
        except Exception as error:  # noqa: BLE001
            with lock:
                print(f"  {slug} {arm} failed: {str(error)[:200]}", file=sys.stderr, flush=True)
            return None

    # review.py reads PASSES and SYNTHESIS_INSTRUCTION at call time, so the arms
    # run one after the other, each with its own set in place.
    done = []
    for arm in ("before", "after"):
        review.PASSES = arms[arm].PASSES
        review.SYNTHESIS_INSTRUCTION = arms[arm].SYNTHESIS_INSTRUCTION
        with ThreadPoolExecutor(2) as pool:
            done += [r for r in pool.map(one, [(s, arm) for s in chosen]) if r]

    results = collections.defaultdict(dict)
    for slug, arm, record in done:
        results[slug][arm] = record
    paired = [s for s in results if len(results[s]) == 2]
    if not paired:
        return 1
    lines = [f"{len(paired)} papers reviewed both ways", ""]
    for arm in ("before", "after"):
        row = [f"{statistics.mean(len(results[s][arm]['comments']) for s in paired):.1f} comments"]
        for kind in KINDS:
            got = [sum(1 for r in results[s][arm]["covered"]
                       if json.loads((args.kinds / f"{s}.json").read_text())["labels"].get(r) == kind)
                   for s in paired]
            total = statistics.mean(sum(1 for v in json.loads((args.kinds / f"{s}.json").read_text())["labels"].values()
                                        if v == kind) for s in paired)
            row.append(f"{kind} {statistics.mean(got):.2f} of {total:.1f}")
        row.append(f"all {statistics.mean(len(results[s][arm]['covered']) for s in paired):.2f}")
        lines.append(f"  {arm:7} " + " | ".join(row))
    summary = "\n".join(lines)
    (args.out / "summary.txt").write_text(summary + "\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
