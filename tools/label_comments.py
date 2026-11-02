"""Label referee comments on three separate axes, and check how well labels agree.

classify_points.py put each comment in one of presentation, general or field,
and the last two are not one axis: "why 30 principal components?" is a general
kind of request (justify a choice) about field content (single-cell analysis).
Its rule, take the most substantive, pushed such comments into "field" and left
"general" hard to tell apart, for a model and for a classifier on embeddings.

Here each comment gets three labels instead:

  target   presentation, substance, or other (praise, summary, a verdict)
  request  what is asked for, from a list drawn from the corpus's own clusters
  expert   whether making the comment takes knowledge particular to the field

    CLAUDE_CONFIG_DIR=… python -m tools.label_comments agree SAMPLE OUT [--n 200]
    CLAUDE_CONFIG_DIR=… python -m tools.label_comments label SAMPLE OUT

`agree` labels the first n comments of SAMPLE (a comment_types sample.json) three
times, Haiku twice and Sonnet once, and reports agreement on each axis. `label`
labels them all with Haiku, for training a classifier.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from pydantic import BaseModel  # noqa: E402

from tools.precedent_eval import backend_for  # noqa: E402

REQUESTS = {
    "justify_choice": "justify a choice of method, model, parameter, threshold, window, sample, variables or baseline, or compare it with an alternative",
    "more_evidence": "add a control, experiment, measurement, characterisation, analysis or comparison",
    "statistics": "report or fix statistics: replicates, sample size, error bars, uncertainty, tests, significance",
    "alternative_explanation": "consider another explanation, mechanism or interpretation of a result",
    "overclaim": "a claim, title or conclusion goes beyond what the data show; tone it down or support it",
    "limitations_scope": "discuss limitations, generality, applicability or the conditions where the findings hold",
    "definition": "define a term or concept the work relies on, or use it in its accepted sense",
    "novelty_prior_work": "state what is new, compare with or cite prior work, correct a citation",
    "method_detail": "give missing method or experimental detail for reproducibility, or share data or code",
    "error_inconsistency": "a number, statement or calculation is wrong or disagrees with another part of the paper",
    "figures_tables": "a figure or table: labels, axes, legends, panels, legibility, what it shows",
    "writing": "wording, typos, grammar, abbreviations, structure, length, moving text to the supplement",
    "none": "no request: praise, a summary of the paper, a recommendation or verdict",
}

SYSTEM = "You classify referee comments and reply with JSON only."

PROMPT = """\
Label each referee comment below on three separate axes.

target:
  presentation  about how the work is written or shown (wording, figures and
                tables as displayed, notation, typos, structure)
  substance     about what was done, measured, analysed or concluded
  other         not a request: praise, a summary, a recommendation or verdict

request: the main thing the comment asks for, one of
{requests}

expert: true if making this comment takes knowledge particular to the paper's
field (a specific technique, mechanism, pitfall or concept a non-specialist
scientist would not know to raise); false if any careful scientist from another
field could have made it. Judge the comment as written: "why 30 principal
components?" is justify_choice and expert true only if knowing why it matters
takes specialist knowledge.

COMMENTS
{comments}
"""


class Label(BaseModel):
    id: str
    target: Literal["presentation", "substance", "other"]
    request: Literal[tuple(REQUESTS)]  # type: ignore[valid-type]
    expert: bool


class Labels(BaseModel):
    labels: list[Label]


BATCH = 50


def label_batch(comments: list[dict], model: str, seed: int | None = None) -> dict[str, dict]:
    """Labels keyed by rowid. A seed shuffles the order, so a repeat run is not
    the same prompt twice."""
    order = list(comments)
    if seed is not None:
        random.Random(seed).shuffle(order)
    listed = "\n".join(f"C{n}. {c['text'][:800]}" for n, c in enumerate(order, 1))
    requests = "\n".join(f"  {k:24} {v}" for k, v in REQUESTS.items())
    backend, _ = backend_for(model)
    answer = backend.run_structured(SYSTEM, [], PROMPT.format(requests=requests, comments=listed),
                                    Labels, effort="medium")
    by_id = {l.id: l for l in answer.labels}
    out = {}
    for n, c in enumerate(order, 1):
        l = by_id.get(f"C{n}")
        if l:
            out[str(c["rowid"])] = {"target": l.target, "request": l.request, "expert": l.expert}
    return out


def run(comments: list[dict], model: str, path: Path, workers: int, seed: int | None) -> dict:
    labels = json.loads(path.read_text()) if path.is_file() else {}
    todo = [c for c in comments if str(c["rowid"]) not in labels]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    lock = threading.Lock()

    def one(i_batch):
        i, batch = i_batch
        try:
            got = label_batch(batch, model, None if seed is None else seed + i)
        except Exception as error:  # noqa: BLE001
            print(f"  batch {i} failed: {str(error)[:200]}", file=sys.stderr, flush=True)
            return
        with lock:
            labels.update(got)
            path.write_text(json.dumps(labels))
            print(f"  {path.stem}: {len(labels)}/{len(comments)}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, enumerate(batches)))
    return labels


def kappa(a: list, b: list) -> float:
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = collections.Counter(a), collections.Counter(b)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / n / n
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def agreement(runs: dict[str, dict], comments: list[dict]) -> str:
    names = list(runs)
    ids = [str(c["rowid"]) for c in comments if all(str(c["rowid"]) in runs[r] for r in names)]
    lines = [f"{len(ids)} comments labelled by all of {', '.join(names)}", ""]
    for axis in ("target", "request", "expert"):
        lines.append(f"{axis}:")
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                a = [runs[x][k][axis] for k in ids]
                b = [runs[y][k][axis] for k in ids]
                agree = sum(p == q for p, q in zip(a, b)) / len(ids)
                lines.append(f"  {x} vs {y}: {agree:.0%} agree, kappa {kappa(a, b):.2f}")
        dist = collections.Counter(runs[names[0]][k][axis] for k in ids)
        lines.append("  " + names[0] + ": " + ", ".join(f"{k} {v}" for k, v in dist.most_common()))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("agree", "label"))
    parser.add_argument("sample", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    comments = json.loads(args.sample.read_text())

    if args.mode == "agree":
        comments = comments[: args.n]
        runs = {
            "haiku_1": run(comments, "cli:claude-haiku-5-5", args.out / "haiku_1.json", args.workers, None),
            "haiku_2": run(comments, "cli:claude-haiku-5-5", args.out / "haiku_2.json", args.workers, 7),
            "sonnet": run(comments, "cli:claude-sonnet-5-5", args.out / "sonnet.json", args.workers, 11),
        }
        text = agreement(runs, comments)
        (args.out / "agreement.txt").write_text(text + "\n")
        print(text)
    else:
        run(comments, "cli:claude-haiku-5-5", args.out / "haiku.json", args.workers, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
