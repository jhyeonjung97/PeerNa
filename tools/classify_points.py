"""Sort each answer-key point into presentation, general substance, or field knowledge.

The precedent ceiling's matches were mostly presentation points (an unclear axis,
e-notation), which a checklist raises without a corpus, and the hit counts weigh
them the same as the points a field's referees make. This labels every
first-round point of the test papers so the earlier results can be counted again
on the substantive points alone; the coverage runs recorded which point each
reference covered, so nothing has to be judged again.

    CLAUDE_CONFIG_DIR=… python -m tools.classify_points ANSWERS OUT
    python -m tools.classify_points ANSWERS OUT --report RUN_DIR ...

Point numbers follow the answer key's order (reviewer, then point), cut at the
same 40,000 characters as the coverage runs, so R7 here is R7 there.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from pydantic import BaseModel  # noqa: E402

from tools.precedent_answers import POINT_CHARS  # noqa: E402
from tools.precedent_eval import backend_for  # noqa: E402

KINDS = ("presentation", "general", "field")

SYSTEM = "You classify referee comments and reply with JSON only."

PROMPT = """\
Classify each referee comment below into one kind.

presentation: about how the work is written or shown, not what was done or
  concluded. Figures, axes, captions, panels, notation and units as written,
  wording, undefined or inconsistent terms, typos, language, length, structure,
  moving text to the supplement, formatting of references.
general: about the substance, but a request any referee in any field could make
  without knowing this field: justify a choice, add a control or a baseline
  comparison, report statistics, error bars, replicates or sample size, give
  missing method details for reproducibility, state the novelty over prior work,
  tone down an overclaim, discuss limitations, share data or code.
field: about the substance, and it takes knowledge of this field to make: a
  specific alternative mechanism or explanation, a specific measurement,
  technique or analysis the field expects, a known pitfall of a specific method,
  a specific physical, chemical or biological concept the claim depends on.

When a comment mixes kinds, choose the most substantive one it contains
(field over general over presentation).

COMMENTS
{comments}
"""


class Label(BaseModel):
    id: str
    kind: Literal["presentation", "general", "field"]


class Labels(BaseModel):
    labels: list[Label]


def key_points(answers: Path, slug: str) -> list[str]:
    reports = json.loads((answers / f"{slug}.json").read_text())["reports"]
    key = [p for r in sorted(reports, key=lambda r: r["reviewer"]) for p in r["points"]]
    kept, used = [], 0
    for c in key:
        line = c[:POINT_CHARS]
        if used + len(line) > 40_000:
            break
        kept.append(line)
        used += len(line)
    return kept


def classify(slug: str, points: list[str], model: str, out: Path, lock: threading.Lock) -> dict:
    path = out / f"{slug}.json"
    if path.is_file():
        return json.loads(path.read_text())
    backend, model_id = backend_for(model)
    listed = "\n".join(f"R{n}. {p}" for n, p in enumerate(points, 1))
    answer = backend.run_structured(SYSTEM, [], PROMPT.format(comments=listed), Labels, effort="medium")
    labels = {l.id: l.kind for l in answer.labels if l.id in {f"R{n}" for n in range(1, len(points) + 1)}}
    missing = len(points) - len(labels)
    if missing:
        raise RuntimeError(f"{missing} of {len(points)} points unlabelled")
    record = {"slug": slug, "model": model_id, "labels": labels}
    path.write_text(json.dumps(record, indent=1))
    with lock:
        counts = collections.Counter(labels.values())
        print(f"  {slug}: " + ", ".join(f"{k} {counts[k]}" for k in KINDS), file=sys.stderr, flush=True)
    return record


def report(out: Path, runs: list[Path]) -> str:
    """Coverage by kind for each coverage run (tools/precedent_answers.py, which
    records several sources, or tools/precedent_ceiling.py, which records one)."""
    labels = {p.stem: json.loads(p.read_text())["labels"] for p in out.glob("s*.json")}
    lines = []
    totals = collections.Counter(k for l in labels.values() for k in l.values())
    lines.append(f"{len(labels)} papers; points: " + ", ".join(
        f"{k} {totals[k]} ({totals[k] / sum(totals.values()):.0%})" for k in KINDS))
    for run in runs:
        results = [json.loads(p.read_text()) for p in run.glob("s*.json")]
        results = [r for r in results if r["slug"] in labels]
        if not results:
            continue
        sources = results[0]["covered"] if isinstance(results[0]["covered"], dict) else {"covered": None}
        lines += ["", f"{run.name} ({len(results)} papers)"]
        for source in sources:
            row = []
            for kind in KINDS:
                got = []
                for r in results:
                    covered = r["covered"][source] if source != "covered" else r["covered"]
                    got.append(sum(1 for rid in covered if labels[r["slug"]].get(rid) == kind))
                total = statistics.mean(sum(1 for v in labels[r["slug"]].values() if v == kind) for r in results)
                row.append(f"{kind} {statistics.mean(got):.2f} of {total:.1f}")
            lines.append(f"  {source:15} " + " | ".join(row))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("answers", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--model", default="cli:claude-haiku-5-5")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", type=Path, nargs="*", help="Coverage run directories to count by kind.")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    if args.report is not None:
        text = report(args.out, args.report)
        (args.out / "report.txt").write_text(text + "\n")
        print(text)
        return 0

    jobs = [(p.stem, key_points(args.answers, p.stem)) for p in sorted(args.answers.glob("s*.json"))]
    lock = threading.Lock()
    failed = []

    def one(job):
        try:
            return classify(*job, args.model, args.out, lock)
        except Exception as error:  # noqa: BLE001
            with lock:
                failed.append(job[0])
                print(f"  {job[0]} failed: {str(error)[:200]}", file=sys.stderr, flush=True)
            return None

    with ThreadPoolExecutor(args.workers) as pool:
        done = [r for r in pool.map(one, jobs) if r]
    print(f"{len(done)} papers labelled, {len(failed)} failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
