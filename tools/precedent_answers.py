"""Were the answers in the precedents?

The precedent test found that precedents do not raise the hits. Two reasons
would look the same from there: the retrieved comments did not contain what the
referees asked (a search problem), or they did and the predictor did not use them
(a design problem). This marks each first-round referee point of a test paper as
covered or not by four references, one judge call each: the precedent comments
the predictor was given paper by paper, those given part by part, and the
predictor's own twelve predictions without precedents and with the part-by-part
ones.

    CLAUDE_CONFIG_DIR=… python -m tools.precedent_answers RUN ANSWERS OUT

RUN is a precedent_eval output directory; ANSWERS the answer keys it judged
against. Writes OUT/<slug>.json, so a stopped run resumes.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from math import sqrt
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from pydantic import BaseModel  # noqa: E402

from tools.precedent_eval import JUDGE_EFFORT, backend_for, sign_test  # noqa: E402

SYSTEM = "You mark an answer key against reference material and reply with JSON only."

PROMPT = """\
Below are the comments real referees wrote about a manuscript, then reference
material. For each referee comment, decide whether some item of the reference
material makes substantially the same request or criticism: the same object and
the same ask. Partial overlap in topic is not enough — "add stability data" does
not cover a comment about the stability mechanism. A generic item covers only a
comment that makes the same generic point. The material may be about other
papers; it covers a comment when it asks the same thing of the same kind of
object.

REFEREE COMMENTS
{real}

REFERENCE MATERIAL
{material}

Answer with JSON only: {{"covered": ["R2", "R9", ...]}} listing every referee
comment that the material covers.
"""

#: The key's points are cut as the precedent test cut them.
POINT_CHARS = 800
SOURCES = ("paper_material", "part_material", "prior", "part")


class Covered(BaseModel):
    covered: list[str]


def material_items(predict: dict, source: str) -> list[str]:
    if source.endswith("_material"):
        # The comments as the predictor saw them, one "- " line each.
        return [line[2:] for line in (predict.get(source) or "").splitlines() if line.startswith("- ")]
    return predict.get(source) or []


def mark(slug: str, predict: dict, key: list[str], judge_name: str, out: Path,
         lock: threading.Lock) -> dict:
    path = out / f"{slug}.json"
    saved = json.loads(path.read_text()) if path.is_file() else {"slug": slug, "covered": {}}
    real, used = [], 0
    for n, c in enumerate(key, 1):
        line = f"R{n}. {c[:POINT_CHARS]}"
        if used + len(line) > 40_000:
            break
        real.append(line)
        used += len(line)
    backend, model_id = backend_for(judge_name)
    for source in SOURCES:
        if source in saved["covered"]:
            continue
        items = material_items(predict, source)
        listed = "\n".join(f"M{i}. {t}" for i, t in enumerate(items, 1))
        answer = backend.run_structured(SYSTEM, [], PROMPT.format(
            real="\n".join(real), material=listed), Covered, effort=JUDGE_EFFORT)
        valid = {f"R{n}" for n in range(1, len(real) + 1)}
        saved["covered"][source] = sorted(set(answer.covered) & valid, key=lambda r: int(r[1:]))
        saved.update(judge=model_id, key_points=len(real),
                     items={**saved.get("items", {}), source: len(items)})
        path.write_text(json.dumps(saved, indent=1))
    with lock:
        print(f"  {slug}: key {len(real)}, " + ", ".join(
            f"{s} {len(saved['covered'][s])}" for s in SOURCES), file=sys.stderr, flush=True)
    return saved


def summarise(results: list[dict]) -> str:
    n = len(results)
    share = {s: [len(r["covered"][s]) / r["key_points"] for r in results] for s in SOURCES}
    count = {s: [len(r["covered"][s]) for r in results] for s in SOURCES}
    lines = [f"{n} papers, {statistics.mean(r['key_points'] for r in results):.1f} referee points a paper", ""]
    for s in SOURCES:
        items = statistics.mean(r["items"][s] for r in results)
        lines.append(f"  {s:15} covers {statistics.mean(count[s]):5.2f} points "
                     f"({statistics.mean(share[s]):5.1%}) from {items:.0f} items")
    # Points the precedents held that the prediction without them missed, and
    # how many of those the prediction with them picked up.
    held, used = [], []
    for r in results:
        c = {s: set(r["covered"][s]) for s in SOURCES}
        new = (c["paper_material"] | c["part_material"]) - c["prior"]
        held.append(len(new))
        used.append(len(new & c["part"]))
    lines += ["",
              f"  in the precedents but not in the prediction without them: {statistics.mean(held):.2f} a paper",
              f"  of those, in the prediction made with part-by-part precedents: {statistics.mean(used):.2f}"]
    diffs = [a - b for a, b in zip(count["part"], count["prior"])]
    mean = statistics.mean(diffs)
    half = 1.96 * statistics.stdev(diffs) / sqrt(n)
    wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    lines.append(f"  covered with part precedents − without: {mean:+.2f} (95% CI {mean - half:+.2f} to "
                 f"{mean + half:+.2f}); {wins} up, {losses} down, sign test p={sign_test(wins, losses):.3f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("answers", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--predictor", default="claude-sonnet-5-5")
    parser.add_argument("--judge", default="cli:claude-sonnet-5-5")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    # The papers the precedent test judged, so the numbers line up with it.
    judged = {p.stem for p in (args.run / "judge" / args.predictor).glob("*.json")}
    jobs = []
    for path in sorted((args.run / "predict" / args.predictor).glob("*.json")):
        if path.stem not in judged or not (args.answers / path.name).is_file():
            continue
        predict = json.loads(path.read_text())
        key = [p for r in sorted(json.loads((args.answers / path.name).read_text())["reports"],
                                 key=lambda r: r["reviewer"]) for p in r["points"]]
        if key and all(material_items(predict, s) for s in SOURCES):
            jobs.append((path.stem, predict, key))
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"{len(jobs)} papers", file=sys.stderr)

    lock = threading.Lock()
    failed = []

    def one(job):
        try:
            return mark(*job, args.judge, args.out, lock)
        except Exception as error:  # noqa: BLE001
            with lock:
                failed.append(job[0])
                print(f"  {job[0]} failed: {str(error)[:200]}", file=sys.stderr, flush=True)
            return None

    with ThreadPoolExecutor(args.workers) as pool:
        results = [r for r in pool.map(one, jobs) if r and len(r["covered"]) == len(SOURCES)]
    if failed:
        print(f"{len(failed)} failed; run again to retry them.", file=sys.stderr)
    if results:
        summary = summarise(results)
        (args.out / "summary.txt").write_text(summary + "\n")
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
