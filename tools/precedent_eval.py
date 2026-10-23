"""Does the corpus help predict what referees will ask for? Part by part or whole?

The test in DECISIONS.md, re-run on papers the model cannot have seen, with a
third arm. Each paper is held out of the index; its own referee comments are
the answer key. Three arms predict twelve referee demands each:

    prior   the manuscript alone
    paper   plus every comment on the six nearest papers (the old precedent pass)
    part    plus the comments found for each part of the manuscript (the new one)

A judge from a different model family then marks each prediction against the
real comments, with all thirty-six shuffled together and unlabelled, so it can
neither favour an arm nor share the predictor's habits.

The manuscript is the published version, so it already contains what the
referees asked for. That flatters every arm equally and is why the absolute hit
rate here is not a measure of anything; only the differences are.

    python -m tools.precedent_eval FULLTEXT_DIR OUT_DIR [--limit N]

FULLTEXT_DIR holds <slug>.txt for each held-out paper. Every stage is written to
OUT_DIR as it finishes, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from math import comb
from pathlib import Path

from pydantic import BaseModel, Field

from ai_peer_reviewer import backends, config, corpus_index, models
from ai_peer_reviewer.loader import Manuscript
from ai_peer_reviewer.parts import TextPart
from ai_peer_reviewer.review import Reviewer

ARMS = ("prior", "paper", "part")
PREDICTIONS = 12
PREDICTOR = "luna"
JUDGE = "claude-sonnet-5"

#: The published text runs to 60–90k characters; the referees read the
#: submitted one, so nothing past the main text is needed to guess at them.
MANUSCRIPT_CHARS = 60_000

PREDICT = """\
This manuscript is shown as it was published, after peer review. Predict what
the referees asked of it when it was first submitted: the {n} specific requests
or criticisms most likely to appear in their reports — controls, measurements,
analyses, comparisons, clarifications, claims they challenged. Be specific to this
paper; a prediction that would fit any paper in the field is wasted.
"""

MATERIAL = """\

Below is reference material: comments real referees wrote about related work.
Use it where it applies to this manuscript, and ignore it where it does not.
Write every prediction in your own words.
{material}
"""

JUDGE_PROMPT = """\
Below are the comments the referees actually wrote about a manuscript, then a
list of predicted referee requests. For each prediction, decide whether at least
one real comment makes substantially the same request or criticism: the same
object and the same ask. Partial overlap in topic is not enough — "add stability
data" does not match a comment about the stability mechanism. A generic
prediction matches only a comment that makes the same generic point.

REAL COMMENTS
{real}

PREDICTIONS
{predicted}

Answer with JSON only: {{"hits": ["P3", "P17", ...]}} listing every prediction
that matches.
"""


class Predictions(BaseModel):
    predictions: list[str] = Field(description=f"Exactly {PREDICTIONS} predictions, one sentence each.")


def held_out() -> list[str]:
    indexed = set(json.loads(corpus_index.IDS_PATH.read_text()))
    connection = sqlite3.connect(corpus_index.DB_PATH)
    try:
        return sorted(s for (s,) in connection.execute("SELECT slug FROM papers") if s not in indexed)
    finally:
        connection.close()


def real_comments(slug: str) -> list[str]:
    connection = sqlite3.connect(corpus_index.DB_PATH)
    try:
        return [t for (t,) in connection.execute(
            "SELECT text FROM comments WHERE slug = ? ORDER BY reviewer, position", (slug,))]
    finally:
        connection.close()


def predict(slug: str, text: str, out: Path, lock: threading.Lock) -> dict:
    path = out / f"{slug}.predict.json"
    if path.is_file():
        return json.loads(path.read_text())

    spec = models.resolve(PREDICTOR)
    backend = backends.build(spec)
    reviewer = Reviewer(backend, "referee", use_web_search=False)
    manuscript = Manuscript(Path(f"{slug}.txt"), [TextPart(text[:MANUSCRIPT_CHARS])])

    result: dict = {"slug": slug}
    material = {
        "prior": None,
        "paper": reviewer.precedents_by_paper(manuscript, exclude=slug),
        "part": reviewer.precedents_by_part(manuscript, exclude=slug),
    }
    result["parts"] = reviewer.notes.get("parts", "")
    for arm in ARMS:
        prompt = PREDICT.format(n=PREDICTIONS)
        if material[arm]:
            prompt += MATERIAL.format(material=material[arm][1])
            result[f"{arm}_sources"] = material[arm][2]
        answer = backend.run_structured(reviewer.system, manuscript.parts, prompt, Predictions)
        result[arm] = answer.predictions[:PREDICTIONS]
    result["cost"] = backend.take_usage().cost(spec)
    path.write_text(json.dumps(result, indent=1))
    with lock:
        print(f"  predicted {slug}  ${result['cost']:.3f}", file=sys.stderr, flush=True)
    return result


def judge(result: dict, out: Path, lock: threading.Lock) -> dict:
    slug = result["slug"]
    path = out / f"{slug}.judge.json"
    if path.is_file():
        return json.loads(path.read_text())

    import anthropic

    real, used = [], 0
    for n, c in enumerate(real_comments(slug), 1):
        line = f"R{n}. {c[:800]}"
        if used + len(line) > 40_000:
            break
        real.append(line)
        used += len(line)

    pool = [(arm, p) for arm in ARMS for p in result[arm]]
    random.Random(slug).shuffle(pool)
    predicted = "\n".join(f"P{i}. {p}" for i, (_, p) in enumerate(pool, 1))

    # Streamed because the SDK refuses a request this large without it.
    with anthropic.Anthropic().messages.stream(
        model=JUDGE,
        # Room for the model to think first; at 2000 the thinking alone used it up.
        max_tokens=32000,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(
            real="\n".join(real), predicted=predicted)}],
    ) as stream:
        reply = stream.get_final_message()
    # The reply may open with a thinking block; the answer is in the text.
    raw = "".join(b.text for b in reply.content if b.type == "text")
    match = re.search(r"\{.*\}", raw, re.S)
    hits = set(json.loads(match.group(0))["hits"]) if match else set()

    scored = {arm: 0 for arm in ARMS}
    for i, (arm, _) in enumerate(pool, 1):
        if f"P{i}" in hits:
            scored[arm] += 1
    if not match:
        raise ValueError(f"judge gave no JSON (stop: {reply.stop_reason})")
    verdict = {"slug": slug, "hits": scored, "hit_ids": sorted(hits),
               "pool": [arm for arm, _ in pool],
               "judge_tokens": [reply.usage.input_tokens, reply.usage.output_tokens]}
    path.write_text(json.dumps(verdict, indent=1))
    with lock:
        print(f"  judged {slug}  {scored}", file=sys.stderr, flush=True)
    return verdict


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact sign test, ties dropped."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def summarise(verdicts: list[dict]) -> str:
    if not verdicts:
        return "No paper finished."
    lines = [f"{len(verdicts)} papers, {PREDICTIONS} predictions per arm\n"]
    for arm in ARMS:
        mean = sum(v["hits"][arm] for v in verdicts) / len(verdicts)
        lines.append(f"  {arm:6} mean hits {mean:.2f} / {PREDICTIONS}")
    for a, b in (("part", "paper"), ("part", "prior"), ("paper", "prior")):
        wins = sum(v["hits"][a] > v["hits"][b] for v in verdicts)
        losses = sum(v["hits"][a] < v["hits"][b] for v in verdicts)
        ties = len(verdicts) - wins - losses
        lines.append(f"  {a} vs {b}: {wins} won, {losses} lost, {ties} tied, "
                     f"sign test p={sign_test(wins, losses):.3f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fulltext", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--db", type=Path,
                        help="Read this copy of the corpus database instead. A harvest "
                             "writing to the live one locks out readers.")
    args = parser.parse_args(argv)
    if args.db:
        corpus_index.DB_PATH = args.db

    config.load()
    args.out.mkdir(parents=True, exist_ok=True)
    slugs = [s for s in held_out() if (args.fulltext / f"{s}.txt").is_file()]
    if args.limit:
        slugs = slugs[: args.limit]
    print(f"{len(slugs)} held-out papers with full text", file=sys.stderr)

    lock = threading.Lock()

    def one(slug: str) -> dict | None:
        try:
            text = (args.fulltext / f"{slug}.txt").read_text()
            return judge(predict(slug, text, args.out, lock), args.out, lock)
        except Exception as exc:  # one bad paper should not sink the run
            with lock:
                print(f"  {slug} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verdicts = [v for v in pool.map(one, slugs) if v]

    summary = summarise(verdicts)
    (args.out / "summary.txt").write_text(summary + "\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
