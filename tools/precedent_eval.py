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

By default the manuscript is the published version, which already contains what
the referees asked for — "we added ICP-MS" gives the request away. That flatters
every arm, and the manuscript-alone arm most, since it is the arm that leans
hardest on what the text gives away. --submitted runs the honest version: the
manuscript as first submitted, from a Research Square "In Review" preprint, with
the paper's own reviews still the answer key.

    python -m tools.precedent_eval MANUSCRIPTS OUT [--slugs FILE] [--submitted]

MANUSCRIPTS holds <slug>.txt or <slug>.pdf for each test paper. Without --slugs
the test papers are those missing from the paper index; with it, the listed
ones, each kept out of its own search. Every stage is written to OUT as it
finishes, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import os
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
#: Another model family from the predictor, so it cannot favour its own habits.
#: Thinking is off: the answer is one list of ids, and on Sonnet 5 the thinking
#: alone ran past 16,000 output tokens a paper — most of the cost of judging.
JUDGE = "claude-sonnet-5-5"
#: Judge through Claude Code (`claude -p`) instead of the API; set by --judge-cli.
JUDGE_CLI = False
#: Predict through Claude Code too; set by --predictor-cli MODEL.
PREDICT_CLI = False

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

PREDICT_SUBMITTED = """\
This manuscript is shown as it was submitted, before peer review. Predict what
the referees will ask of it: the {n} specific requests or criticisms most likely
to appear in their reports — controls, measurements, analyses, comparisons,
clarifications, claims they will challenge. Be specific to this paper; a
prediction that would fit any paper in the field is wasted.
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
        # The answer key gets the same filter as the precedents: a reply the
        # parser left in ("We have added ICP-MS data") would let a prediction
        # match the authors rather than the referee.
        # First-round points: what the referees asked of the manuscript as
        # submitted, which is the text the predictor is given.
        first = " AND round = 1" if corpus_index.has_kinds(connection) else ""
        return [t for (t,) in connection.execute(
            "SELECT text FROM comments WHERE slug = ?" + corpus_index.points_only(connection)
            + first + " ORDER BY reviewer, position", (slug,))
            if corpus_index.is_referee_comment(t)]
    finally:
        connection.close()


def predict(slug: str, text: str, out: Path, lock: threading.Lock,
            question: str = PREDICT) -> dict:
    path = out / f"{slug}.predict.json"
    if path.is_file():
        return json.loads(path.read_text())

    spec = models.resolve(PREDICTOR)
    backend = ClaudeCodeBackend(spec) if PREDICT_CLI else backends.build(spec)
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
        prompt = question.format(n=PREDICTIONS)
        if material[arm]:
            prompt += MATERIAL.format(material=material[arm][1])
            result[f"{arm}_sources"] = material[arm][2]
        answer = backend.run_structured(reviewer.system, manuscript.parts, prompt, Predictions)
        result[arm] = answer.predictions[:PREDICTIONS]
    result["cost"] = 0.0 if PREDICT_CLI else backend.take_usage().cost(spec)
    result["predictor"] = spec.id
    path.write_text(json.dumps(result, indent=1))
    with lock:
        print(f"  predicted {slug}  ${result['cost']:.3f}", file=sys.stderr, flush=True)
    return result


def judge(result: dict, out: Path, lock: threading.Lock) -> dict:
    slug = result["slug"]
    path = out / f"{slug}.judge.json"
    if path.is_file():
        return json.loads(path.read_text())

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

    prompt = JUDGE_PROMPT.format(real="\n".join(real), predicted=predicted)
    raw, stop, tokens = (_judge_cli if JUDGE_CLI else _judge_api)(prompt)
    match = re.search(r"\{.*\}", raw, re.S)
    hits = set(json.loads(match.group(0))["hits"]) if match else set()

    scored = {arm: 0 for arm in ARMS}
    for i, (arm, _) in enumerate(pool, 1):
        if f"P{i}" in hits:
            scored[arm] += 1
    if not match:
        raise ValueError(f"judge gave no JSON (stop: {stop})")
    verdict = {"slug": slug, "hits": scored, "hit_ids": sorted(hits),
               "pool": [arm for arm, _ in pool],
               "judge_tokens": tokens}
    path.write_text(json.dumps(verdict, indent=1))
    with lock:
        print(f"  judged {slug}  {scored}", file=sys.stderr, flush=True)
    return verdict


def _judge_api(prompt: str) -> tuple[str, str, list[int]]:
    import anthropic

    with anthropic.Anthropic().messages.stream(
        model=JUDGE,
        max_tokens=4000,
        thinking={"type": "between_tools"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        reply = stream.get_final_message()
    # The reply may open with a thinking block; the answer is in the text.
    raw = "".join(b.text for b in reply.content if b.type == "text")
    return raw, reply.stop_reason, [reply.usage.input_tokens, reply.usage.output_tokens]


def claude_cli(system: str, prompt: str, model: str, schema: dict | None = None) -> dict:
    """One call through Claude Code (`claude -p`), on its subscription, not the API.

    The API key is kept out of the child's environment, or Claude Code would
    bill it. No tools, MCP servers, settings, skills or saved session, and an
    empty working directory: without these the call carries some 80,000 tokens
    of the operator's own setup, down to the language it answers in.
    """
    import subprocess
    import tempfile

    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    command = ["claude", "-p", "--model", model, "--effort", "low", "--tools", "",
               "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
               "--no-session-persistence", "--output-format", "json", "--system-prompt", system]
    if schema:
        command += ["--json-schema", json.dumps(schema)]
    with tempfile.TemporaryDirectory() as empty:
        done = subprocess.run(command, input=prompt, capture_output=True, text=True,
                              env=env, cwd=empty, timeout=1800)
    if done.returncode:
        raise RuntimeError(f"claude -p failed: {done.stderr.strip()[:300] or done.stdout[:300]}")
    return json.loads(done.stdout)


def _judge_cli(prompt: str) -> tuple[str, str, list[int]]:
    reply = claude_cli("You mark predictions against an answer key and reply with JSON only.",
                       prompt, JUDGE)
    usage = reply.get("usage") or {}
    return (reply.get("result") or "", reply.get("subtype", ""),
            [usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
             + usage.get("cache_read_input_tokens", 0), usage.get("output_tokens", 0)])


class ClaudeCodeBackend(backends.Backend):
    """The predictor through Claude Code, text only, for --cli runs."""

    def run_text(self, system, parts, instruction, web_search, effort="high", label="working"):
        return claude_cli(system, self._prompt(parts, instruction), self.spec.id).get("result") or ""

    def run_structured(self, system, parts, instruction, model_cls,
                       web_search: bool = False, effort: str = "high"):
        reply = claude_cli(system, self._prompt(parts, instruction), self.spec.id,
                           model_cls.model_json_schema())
        if reply.get("structured_output") is None:
            raise RuntimeError(f"claude -p gave no structured output ({reply.get('subtype')})")
        return model_cls.model_validate(reply["structured_output"])

    def count_input_tokens(self, system, parts) -> int:
        return 0

    @staticmethod
    def _prompt(parts, instruction: str) -> str:
        text = "\n\n".join(p.text for p in parts if isinstance(p, TextPart))
        return f"MANUSCRIPT\n\n{text}\n\n---\n\n{instruction}"


def manuscript_file(folder: Path, slug: str) -> Path | None:
    for suffix in (".txt", ".pdf"):
        if (folder / f"{slug}{suffix}").is_file():
            return folder / f"{slug}{suffix}"
    return None


def read_manuscript(path: Path) -> str:
    if path.suffix == ".txt":
        return path.read_text()
    import pymupdf

    with pymupdf.open(path) as document:
        return "\n\n".join(page.get_text() for page in document)


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
    parser.add_argument("--slugs", type=Path,
                        help="File of test slugs, one per line, instead of the papers "
                             "missing from the index.")
    parser.add_argument("--submitted", action="store_true",
                        help="The manuscripts are as submitted, not as published.")
    parser.add_argument("--db", type=Path,
                        help="Read this copy of the corpus database instead. A harvest "
                             "writing to the live one locks out readers.")
    parser.add_argument("--judge-cli", action="store_true",
                        help="Judge through Claude Code on its subscription, not the API.")
    parser.add_argument("--predictor-cli", metavar="MODEL",
                        help="Predict with this model through Claude Code, e.g. sonnet.")
    args = parser.parse_args(argv)
    global JUDGE_CLI, PREDICT_CLI, PREDICTOR
    JUDGE_CLI = args.judge_cli
    if args.predictor_cli:
        PREDICT_CLI, PREDICTOR = True, args.predictor_cli
    if args.db:
        corpus_index.DB_PATH = args.db

    config.load()
    args.out.mkdir(parents=True, exist_ok=True)
    wanted = args.slugs.read_text().split() if args.slugs else held_out()
    slugs = [s for s in wanted if manuscript_file(args.fulltext, s)]
    if args.limit:
        slugs = slugs[: args.limit]
    question = PREDICT_SUBMITTED if args.submitted else PREDICT
    print(f"{len(slugs)} test papers with a manuscript", file=sys.stderr)

    lock = threading.Lock()

    def one(slug: str) -> dict | None:
        try:
            text = read_manuscript(manuscript_file(args.fulltext, slug))
            return judge(predict(slug, text, args.out, lock, question), args.out, lock)
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
