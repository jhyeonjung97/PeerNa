"""Does the corpus help predict what referees will ask for, and does the model?

Each test manuscript is held out of the index; its own first-round referee
comments are the answer key. Every predictor makes twelve predictions in each of
three arms:

    prior   the manuscript alone
    paper   plus every comment on the six nearest papers (the old precedent pass)
    part    plus the comments found for each part of the manuscript (the new one)

and every judge then marks all of them for that manuscript at once, every
predictor's and every arm's, shuffled and unlabelled, against the real comments.

Built so that a run never has to be repeated for a fault found afterwards:

- Predicting is the expensive stage and is stored whole: the prompt, the
  material, its sources, the manuscript's hash, model, effort, index and commit.
  Judging and counting read only what is stored, so they can be redone or
  extended (another judge, another tally) without predicting again.
- With --dates, a manuscript is shown precedents only from papers published
  before it was received, as its referees and a user at the time would have been.
- Two judges from different companies, so a judge's taste for its own family's
  wording shows up as disagreement rather than as a result.

By default the manuscript is the published version, which already contains what
the referees asked for; --submitted runs the honest version, a Research Square
"In Review" preprint as first submitted.

    python -m tools.precedent_eval MANUSCRIPTS OUT --slugs FILE --submitted \\
        --testset testset.json --dates published-dates.json \\
        --predictors luna,cli:claude-sonnet-5-5 --judges cli:claude-sonnet-5-5,luna

MANUSCRIPTS holds <slug>.txt or <slug>.pdf. Every stage is written to OUT as it
finishes, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from math import comb, sqrt
from pathlib import Path

# Before numpy loads: OpenBLAS's own worker threads deadlock a fork() made
# while they are up (its atfork handler joins them), and every Claude Code
# call forks. The run hung twice that way, all threads parked, no child.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from pydantic import BaseModel, Field

from ai_peer_reviewer import backends, config, corpus_index, corpus_sync, models
from ai_peer_reviewer.loader import Manuscript
from ai_peer_reviewer.parts import TextPart
from ai_peer_reviewer.review import Reviewer

ARMS = ("prior", "paper", "part")
PREDICTIONS = 12
#: The same effort for every predictor. The site runs its passes at high, so
#: that is what is measured; the judges answer one list and run at low.
PREDICT_EFFORT = "high"
JUDGE_EFFORT = "low"

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

JUDGE_SYSTEM = "You mark predictions against an answer key and reply with JSON only."

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


class Hits(BaseModel):
    hits: list[str]


# ------------------------------------------------------------- models

def claude_cli(system: str, prompt: str, model: str, schema: dict | None = None,
               effort: str = "low") -> dict:
    """One call through Claude Code (`claude -p`), on its subscription, not the API.

    The API key is kept out of the child's environment, or Claude Code would
    bill it. No tools, MCP servers, settings, skills or saved session, and an
    empty working directory: without these the call carries some 80,000 tokens
    of the operator's own setup, down to the language it answers in.
    """
    import tempfile

    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    command = ["claude", "-p", "--model", model, "--effort", effort, "--tools", "",
               "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
               "--no-session-persistence", "--output-format", "json", "--system-prompt", system]
    if schema:
        command += ["--json-schema", json.dumps(schema)]
    with tempfile.TemporaryDirectory() as empty:
        done = subprocess.run(command, input=prompt, capture_output=True, text=True,
                              env=env, cwd=empty, timeout=1800)
    if done.returncode:
        # The reason ("You've hit your limit …") is in the JSON's result field,
        # past where a cut of the raw output would end.
        try:
            reason = json.loads(done.stdout).get("result") or done.stdout
        except ValueError:
            reason = done.stderr.strip() or done.stdout
        raise RuntimeError(f"claude -p failed: {str(reason)[:300]}")
    return json.loads(done.stdout)


class ClaudeCodeBackend(backends.Backend):
    """A Claude model through Claude Code, text only."""

    def run_text(self, system, parts, instruction, web_search, effort="high", label="working"):
        return claude_cli(system, self._prompt(parts, instruction), self.spec.id,
                          effort=effort).get("result") or ""

    def run_structured(self, system, parts, instruction, model_cls,
                       web_search: bool = False, effort: str = "high"):
        reply = claude_cli(system, self._prompt(parts, instruction), self.spec.id,
                           model_cls.model_json_schema(), effort=effort)
        if reply.get("structured_output") is None:
            raise RuntimeError(f"claude -p gave no structured output ({reply.get('subtype')})")
        return model_cls.model_validate(reply["structured_output"])

    def count_input_tokens(self, system, parts) -> int:
        return 0

    @staticmethod
    def _prompt(parts, instruction: str) -> str:
        text = "\n\n".join(p.text for p in parts if isinstance(p, TextPart))
        return f"MANUSCRIPT\n\n{text}\n\n---\n\n{instruction}" if text else instruction


def backend_for(name: str) -> tuple[backends.Backend, str]:
    """`cli:MODEL` runs through Claude Code; anything else through its API."""
    via_cli = name.startswith("cli:")
    spec = models.resolve(name.removeprefix("cli:"))
    backend = ClaudeCodeBackend(spec) if via_cli else backends.build(spec)
    return backend, spec.id


def label(name: str) -> str:
    """A name safe for a directory: the model id, without the route."""
    return models.resolve(name.removeprefix("cli:")).id


# ------------------------------------------------------------- data

#: A directory of answer keys read by a model (tools/answer_key.py), used in
#: place of the parser's comments when set by --answers.
ANSWERS: "Path | None" = None


def real_comments(slug: str) -> list[str]:
    if ANSWERS is not None:
        key = json.loads((ANSWERS / f"{slug}.json").read_text())
        return [p for r in sorted(key["reports"], key=lambda r: r["reviewer"]) for p in r["points"]]
    connection = sqlite3.connect(f"file:{corpus_index.DB_PATH}?mode=ro", uri=True, timeout=30)
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


def received_dates(testset: Path | None) -> dict[str, dt.date]:
    if not testset:
        return {}
    out = {}
    for row in json.loads(testset.read_text()):
        try:
            out[row["slug"]] = dt.datetime.strptime(row["received"], "%d %B %Y").date()
        except (KeyError, TypeError, ValueError):
            pass
    return out


def visible_before(received: dt.date, published: dict[str, str]) -> set[str]:
    """Papers out before this manuscript was received. Undated ones are left
    out: a precedent of unknown date might be from the future."""
    cut = received.isoformat()
    return {slug for slug, day in published.items() if day and day < cut}


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


def provenance() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, cwd=Path(__file__).parent).stdout.strip()
    except OSError:
        commit = ""
    return {"commit": commit, "index": corpus_sync.current() or str(corpus_index.INDEX_DIR),
            "db": str(corpus_index.DB_PATH), "manuscript_chars": MANUSCRIPT_CHARS,
            "predictions": PREDICTIONS}


# ------------------------------------------------------------- stages

def predict(slug: str, text: str, predictor: str, out: Path, question: str,
            visible: "set[str] | None", lock: threading.Lock) -> dict:
    path = out / "predict" / label(predictor) / f"{slug}.json"
    if path.is_file():
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)

    backend, model_id = backend_for(predictor)
    reviewer = Reviewer(backend, "referee", use_web_search=False)
    body = text[:MANUSCRIPT_CHARS]
    manuscript = Manuscript(Path(f"{slug}.txt"), [TextPart(body)])

    with corpus_index.restricted_to(visible):
        material = {
            "prior": None,
            "paper": reviewer.precedents_by_paper(manuscript, exclude=slug),
            "part": reviewer.precedents_by_part(manuscript, exclude=slug),
        }
    result: dict = {
        "slug": slug, "predictor": predictor, "model": model_id, "effort": PREDICT_EFFORT,
        "manuscript_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "visible_papers": None if visible is None else len(visible),
        "parts": reviewer.notes.get("parts", ""), **provenance(),
    }
    for arm in ARMS:
        prompt = question.format(n=PREDICTIONS)
        if material[arm]:
            prompt += MATERIAL.format(material=material[arm][1])
            result[f"{arm}_sources"] = material[arm][2]
            result[f"{arm}_material"] = material[arm][1]
            # Checked as it is made, not at the end: a run with a leak in it is
            # worth stopping, not finishing.
            if slug in material[arm][2]:
                raise RuntimeError(f"{slug} was shown its own reviews in the {arm} arm")
            if visible is not None and not set(material[arm][2]) <= visible:
                raise RuntimeError(f"{slug} was shown a paper from after its receipt")
        result[f"{arm}_prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        # A short list or a reply that runs away and breaks off (Luna once
        # repeated a tab until the output ran out) is retried, not fatal.
        for attempt in range(3):
            try:
                answer = backend.run_structured(reviewer.system, manuscript.parts, prompt,
                                                Predictions, effort=PREDICT_EFFORT)
                if len(answer.predictions) >= PREDICTIONS:
                    break
                problem = f"gave {len(answer.predictions)} predictions"
            except Exception as exc:  # malformed output from the model
                problem = f"{type(exc).__name__}: {str(exc)[:120]}"
        else:
            raise RuntimeError(f"{slug}: {arm} {problem} three times")
        result[arm] = answer.predictions[:PREDICTIONS]
    result["cost"] = 0.0 if isinstance(backend, ClaudeCodeBackend) \
        else backend.take_usage().cost(backend.spec)
    path.write_text(json.dumps(result, indent=1))
    with lock:
        print(f"  predicted {slug} with {model_id}  ${result['cost']:.3f}", file=sys.stderr, flush=True)
    return result


def judge(slug: str, predicted: list[dict], judge_name: str, out: Path,
          lock: threading.Lock) -> dict:
    """Mark every prediction for this manuscript, all predictors and arms in one
    shuffled list, so the judge cannot tell which came from where."""
    path = out / "judge" / label(judge_name) / f"{slug}.json"
    if path.is_file():
        saved = json.loads(path.read_text())
        if saved.get("predictors") == sorted(p["predictor"] for p in predicted):
            return saved
    path.parent.mkdir(parents=True, exist_ok=True)

    real, used = [], 0
    for n, c in enumerate(real_comments(slug), 1):
        line = f"R{n}. {c[:800]}"
        if used + len(line) > 40_000:
            break
        real.append(line)
        used += len(line)
    if not real:
        raise RuntimeError(f"{slug} has no first-round referee points to judge against")

    pool = [(p["predictor"], arm, text) for p in predicted for arm in ARMS for text in p[arm]]
    random.Random(slug).shuffle(pool)
    listed = "\n".join(f"P{i}. {text}" for i, (_, _, text) in enumerate(pool, 1))
    prompt = JUDGE_PROMPT.format(real="\n".join(real), predicted=listed)

    backend, model_id = backend_for(judge_name)
    answer = backend.run_structured(JUDGE_SYSTEM, [], prompt, Hits, effort=JUDGE_EFFORT)
    hit_ids = set(answer.hits)
    scored: dict[str, dict[str, int]] = {}
    for i, (who, arm, _) in enumerate(pool, 1):
        scored.setdefault(who, {a: 0 for a in ARMS})
        if f"P{i}" in hit_ids:
            scored[who][arm] += 1
    verdict = {"slug": slug, "judge": judge_name, "model": model_id,
               "predictors": sorted(p["predictor"] for p in predicted),
               "hits": scored, "hit_ids": sorted(hit_ids),
               "pool": [[who, arm] for who, arm, _ in pool], "real_comments": len(real)}
    path.write_text(json.dumps(verdict, indent=1))
    with lock:
        print(f"  judged {slug} by {model_id}  {scored}", file=sys.stderr, flush=True)
    return verdict


# ------------------------------------------------------------- summary

def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact sign test, ties dropped."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired(a: list[float], b: list[float]) -> str:
    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    mean = statistics.mean(diffs)
    half = 1.96 * statistics.stdev(diffs) / sqrt(n) if n > 1 else float("nan")
    wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    return (f"{mean:+.2f} a paper (95% CI {mean - half:+.2f} to {mean + half:+.2f}); "
            f"{wins} won, {losses} lost, {n - wins - losses} tied, sign test p={sign_test(wins, losses):.3f}")


def summarise(verdicts: list[dict], subsets: dict[str, set[str]]) -> str:
    lines = []
    for judge_name in sorted({v["judge"] for v in verdicts}):
        for name, members in subsets.items():
            mine = [v for v in verdicts if v["judge"] == judge_name and v["slug"] in members]
            if not mine:
                continue
            predictors = sorted({p for v in mine for p in v["hits"]})
            lines.append(f"\n== judge {judge_name} · {name}: {len(mine)} manuscripts, "
                         f"{PREDICTIONS} predictions per arm ==")
            for p in predictors:
                for arm in ARMS:
                    mean = statistics.mean(v["hits"][p][arm] for v in mine)
                    lines.append(f"  {p:28} {arm:6} mean hits {mean:.2f}")
            for p in predictors:
                for a, b in (("part", "prior"), ("paper", "prior"), ("part", "paper")):
                    lines.append(f"  {p}: {a} vs {b}: " + paired(
                        [v["hits"][p][a] for v in mine], [v["hits"][p][b] for v in mine]))
            for i, p in enumerate(predictors):
                for q in predictors[i + 1:]:
                    for arm in ARMS:
                        lines.append(f"  {arm}: {p} vs {q}: " + paired(
                            [v["hits"][p][arm] for v in mine], [v["hits"][q][arm] for v in mine]))
    return "\n".join(lines) if lines else "No manuscript finished."


# ------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fulltext", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--slugs", type=Path, required=True, help="Test slugs, one per line.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--submitted", action="store_true",
                        help="The manuscripts are as submitted, not as published.")
    parser.add_argument("--db", type=Path,
                        help="Read this copy of the corpus database instead. A harvest "
                             "writing to the live one locks out readers.")
    parser.add_argument("--testset", type=Path, help="testset.json, for each received date.")
    parser.add_argument("--dates", type=Path,
                        help="Publication date of every corpus paper (JSON slug → date). "
                             "With --testset, precedents come only from earlier papers.")
    parser.add_argument("--predictors", default="luna",
                        help="Comma-separated; cli:MODEL runs through Claude Code.")
    parser.add_argument("--judges", default="cli:claude-sonnet-5-5",
                        help="Comma-separated; every judge marks every predictor's predictions.")
    parser.add_argument("--answers", type=Path,
                        help="Answer keys from tools/answer_key.py instead of the parser's.")
    parser.add_argument("--subset", action="append", default=[], metavar="NAME=FILE",
                        help="Also report on the slugs in FILE, e.g. stamped=stamped.slugs.")
    args = parser.parse_args(argv)
    if args.db:
        corpus_index.DB_PATH = args.db
    global ANSWERS
    ANSWERS = args.answers
    if args.dates and not args.testset:
        parser.error("--dates needs --testset for the received dates")

    config.load()
    args.out.mkdir(parents=True, exist_ok=True)
    wanted = args.slugs.read_text().split()
    slugs = [s for s in wanted if manuscript_file(args.fulltext, s)]
    if args.limit:
        slugs = slugs[: args.limit]
    question = PREDICT_SUBMITTED if args.submitted else PREDICT
    predictors = [p for p in args.predictors.split(",") if p]
    judges = [j for j in args.judges.split(",") if j]
    received = received_dates(args.testset)
    published = json.loads(args.dates.read_text()) if args.dates else None
    print(f"{len(slugs)} manuscripts · predictors {predictors} · judges {judges} · "
          f"{'precedents before receipt' if published else 'no date cut'}", file=sys.stderr)
    (args.out / "run.json").write_text(json.dumps({**vars(args), **provenance()}, default=str, indent=1))

    lock = threading.Lock()

    def one(slug: str) -> list[dict]:
        try:
            visible = None
            if published is not None:
                if slug not in received:
                    raise RuntimeError("no received date")
                visible = visible_before(received[slug], published)
            text = read_manuscript(manuscript_file(args.fulltext, slug))
            made = [predict(slug, text, p, args.out, question, visible, lock) for p in predictors]
            return [judge(slug, made, j, args.out, lock) for j in judges]
        except Exception as exc:  # one bad manuscript should not sink the run
            with lock:
                print(f"  {slug} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verdicts = [v for vs in pool.map(one, slugs) for v in vs]

    subsets = {"all": set(slugs)}
    for spec in args.subset:
        name, file = spec.split("=", 1)
        subsets[name] = set(Path(file).read_text().split())
    summary = summarise(verdicts, subsets)
    (args.out / "summary.txt").write_text(summary + "\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
